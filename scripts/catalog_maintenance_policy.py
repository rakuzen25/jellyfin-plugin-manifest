"""Maintenance requests and protected catalog delta validation."""

import copy
import json
import re
from pathlib import Path

from catalog_core import (
    CANONICAL_VERSION,
    REPOSITORY,
    GitHub,
    VerificationError,
    command,
    render,
    verify_history,
    withdrawal_keys,
)

FILES = {"withdrawals.json", "manifest.json"}


def mapping(rows: list[dict]) -> dict[str, dict]:
    return {row["guid"] + "/" + row["version"]: row for row in rows}


def load(root: Path, ref: str | None = None) -> dict:
    return {
        name: json.loads(
            command("git", "show", ref + ":" + name, cwd=root)
            if ref
            else (root / name).read_text()
        )
        for name in (
            "plugins.json",
            "history.json",
            "withdrawals.json",
            "manifest.json",
        )
    }


def scoped_validation(
    github: GitHub, base: dict, candidate: dict, changed: set[str]
) -> bool:
    """Only proven maintenance deltas get a narrow gate; additions remain full."""
    if not changed or not changed.issubset(FILES):
        return False
    if (
        base["plugins.json"] != candidate["plugins.json"]
        or base["history.json"] != candidate["history.json"]
    ):
        raise VerificationError("Maintenance cannot modify policy or history")
    plugins, history = base["plugins.json"], base["history.json"]
    before = withdrawal_keys(base["withdrawals.json"], plugins)
    after = withdrawal_keys(candidate["withdrawals.json"], plugins)
    if any(key not in history for key in before ^ after):
        raise VerificationError("Maintenance requires an existing version")
    if base["manifest.json"] != render(
        history, base["withdrawals.json"], plugins
    ) or candidate["manifest.json"] != render(
        history, candidate["withdrawals.json"], plugins
    ):
        raise VerificationError("Maintenance manifest differs from protected history")
    restored = before - after
    if restored:
        verify_history(github, plugins, {key: history[key] for key in restored}, [])
    return True


def rebase_pending(current: dict, branch_base: dict, pending: dict) -> list[dict]:
    if (
        branch_base["history.json"] != pending["history.json"]
        or branch_base["plugins.json"] != pending["plugins.json"]
    ):
        raise VerificationError("Pending maintenance changed protected files")
    if pending["manifest.json"] != render(
        pending["history.json"], pending["withdrawals.json"], pending["plugins.json"]
    ):
        raise VerificationError("Pending manifest is not a maintenance projection")
    withdrawal_keys(pending["withdrawals.json"], pending["plugins.json"])
    original = mapping(branch_base["withdrawals.json"])
    proposed = mapping(pending["withdrawals.json"])
    latest = mapping(current["withdrawals.json"])
    for key in original.keys() | proposed.keys():
        if original.get(key) == proposed.get(key):
            continue
        if key not in current["history.json"]:
            raise VerificationError("Pending request no longer identifies a version")
        if latest.get(key) != original.get(key) and latest.get(key) != proposed.get(
            key
        ):
            raise VerificationError(
                "Main has conflicting newer maintenance; resolve the pending PR"
            )
        if key in proposed:
            latest[key] = copy.deepcopy(proposed[key])
        else:
            latest.pop(key, None)
    return list(latest.values())


def request_change(
    github: GitHub,
    current: dict,
    pending_rows: list[dict],
    operation: str,
    repository: str,
    version: str,
    reason: str,
    actor: str,
    run_url: str,
) -> dict:
    if (
        operation not in {"Withdraw", "Restore"}
        or repository not in current["plugins.json"]
        or not CANONICAL_VERSION.fullmatch(version)
    ):
        raise VerificationError(
            "Choose an allowed plugin and exact four-component version without leading zeros"
        )
    if (
        not reason.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", actor)
        or not re.fullmatch(
            r"https://github.com/" + re.escape(REPOSITORY) + r"/actions/runs/[0-9]+",
            run_url,
        )
    ):
        raise VerificationError(
            "A reason, requesting actor and owning workflow URL are required"
        )
    key = current["plugins.json"][repository]["guid"] + "/" + version
    if key not in current["history.json"]:
        raise VerificationError("That version has not been catalogued")
    rows = mapping(pending_rows)
    if operation == "Withdraw":
        rows.setdefault(
            key,
            {
                "guid": key.split("/")[0],
                "version": version,
                "reason": reason.strip(),
                "requestedBy": actor,
                "requestUrl": run_url,
            },
        )
    else:
        rows.pop(key, None)
    candidate = copy.deepcopy(current)
    candidate["withdrawals.json"] = list(rows.values())
    candidate["manifest.json"] = render(
        current["history.json"], candidate["withdrawals.json"], current["plugins.json"]
    )
    scoped_validation(github, current, candidate, FILES)
    return candidate
