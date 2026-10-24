"""Validate and merge independent plugin entries into a shared local Jellyfin catalog."""

import argparse
import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from json_shapes import is_json_array, is_json_object


def validate_catalog(value: object) -> list[dict[str, Any]]:
    # Schema failures are invalid JSON values, including fields of the wrong type.
    if not is_json_array(value):
        raise ValueError("Catalog must be an array")
    guids: set[str] = set()
    for plugin in value:
        if not is_json_object(plugin) or not isinstance(plugin.get("guid"), str):
            raise ValueError("Invalid plugin entry")
        try:
            guid = str(uuid.UUID(plugin["guid"]))
        except ValueError as error:
            raise ValueError("Invalid plugin GUID") from error
        if guid in guids:
            raise ValueError("Duplicate plugin GUID")
        guids.add(guid)
        if not isinstance(plugin.get("name"), str) or not plugin["name"].strip():
            raise ValueError("Plugin name is required")
        versions = plugin.get("versions")
        if not is_json_array(versions) or not versions:
            raise ValueError("Each plugin needs at least one version")
        seen: set[str] = set()
        for release in versions:
            if not is_json_object(release):
                raise ValueError("Invalid version entry")
            for key in ("version", "targetAbi"):
                if not isinstance(release.get(key), str) or not re.fullmatch(
                    r"\d+\.\d+\.\d+\.\d+", release[key]
                ):
                    raise ValueError(
                        "Versions and ABIs must have four numeric components"
                    )
            # Normalized version identity avoids 01.0.0.0 and 1.0.0.0 duplicates.
            version = ".".join(
                str(int(component)) for component in release["version"].split(".")
            )
            if version in seen:
                raise ValueError("Duplicate plugin version")
            seen.add(version)
            url = release.get("sourceUrl")
            if not isinstance(url, str):
                raise ValueError("Release download URL is required")  # noqa: TRY004
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Release download URLs require HTTPS without credentials, query strings or fragments"
                )
            checksum = release.get("checksum")
            if not isinstance(checksum, str) or not re.fullmatch(
                r"[0-9a-fA-F]{32}", checksum
            ):
                raise ValueError("Jellyfin requires a 32-digit MD5 package checksum")
            timestamp = release.get("timestamp")
            if not isinstance(timestamp, str):
                raise ValueError("Release timestamp is required")  # noqa: TRY004
            try:
                date = datetime.fromisoformat(timestamp)
                if date.tzinfo is None:
                    raise ValueError("Missing timezone")
            except ValueError as error:
                raise ValueError("Invalid release timestamp") from error
    return value


def merge_catalog(existing: object, incoming: object) -> list[dict[str, Any]]:
    result = copy.deepcopy(validate_catalog(existing))
    entries = validate_catalog(incoming)
    for entry in entries:
        guid = uuid.UUID(entry["guid"])
        target = next(
            (plugin for plugin in result if uuid.UUID(plugin["guid"]) == guid), None
        )
        if target is None:
            result.append(copy.deepcopy(entry))
            continue
        for version in entry["versions"]:
            identity = tuple(int(part) for part in version["version"].split("."))
            old = next(
                (
                    item
                    for item in target["versions"]
                    if tuple(int(part) for part in item["version"].split("."))
                    == identity
                ),
                None,
            )
            if old is not None:
                same_artifact = (
                    old["sourceUrl"] == version["sourceUrl"]
                    and old["checksum"].lower() == version["checksum"].lower()
                    and tuple(int(part) for part in old["targetAbi"].split("."))
                    == tuple(int(part) for part in version["targetAbi"].split("."))
                )
                if not same_artifact:
                    raise ValueError("Conflicting entry for an existing plugin version")
                for key, value in version.items():
                    if key not in old:
                        old[key] = copy.deepcopy(value)
            else:
                target["versions"].append(copy.deepcopy(version))
        # Preserve existing metadata, including unknown fields. New fields may be added.
        for key, value in entry.items():
            if key != "versions" and key not in target:
                target[key] = copy.deepcopy(value)
    return validate_catalog(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-input", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--catalog-output", type=Path, required=True)
    args = parser.parse_args()
    if args.catalog_output.resolve() in (
        args.catalog_input.resolve(),
        args.entry.resolve(),
    ):
        parser.error("Write to a separate candidate file; inputs must remain unchanged")
    try:
        merged = merge_catalog(
            json.loads(args.catalog_input.read_text()),
            json.loads(args.entry.read_text()),
        )
    except (ValueError, OSError) as error:
        # Do not expose raw URLs or malformed input bodies in diagnostics.
        if isinstance(error, json.JSONDecodeError):
            parser.error("Invalid catalog JSON")
        if isinstance(error, OSError):
            parser.error("Could not read catalog inputs")
        parser.error(str(error))
    args.catalog_output.write_text(json.dumps(merged, indent=2) + "\n")
    print("Validated shared catalog candidate; no remote changes made.")


if __name__ == "__main__":
    main()
