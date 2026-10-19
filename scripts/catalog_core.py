"""Shared catalog verification, reconciliation and infrastructure boundaries."""

import base64
import copy
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

from catalog import merge_catalog, validate_catalog

REPOSITORY = "rakuzen25/jellyfin-plugin-manifest"
CANONICAL_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}")
_IMMUTABLE_RELEASE_FIELDS = (
    "repository",
    "tag",
    "release_id",
    "commit",
    "artifacts",
    "entry",
)


class VerificationError(ValueError):
    pass


def app_author(author: dict) -> bool:
    slug = os.environ["PLUGIN_APP_SLUG"]
    # gh's GraphQL export uses app/<slug>; REST uses <slug>[bot].
    return (author.get("login") == "app/" + slug and author.get("is_bot") is True) or (
        author.get("login") == slug + "[bot]" and author.get("is_bot") is not False
    )


def configure_app_git_author(root: Path, run) -> None:
    login = os.environ["PLUGIN_APP_SLUG"] + "[bot]"
    user_id = run("gh", "api", "users/" + login, "--jq", ".id")
    if not re.fullmatch(r"[1-9][0-9]*", user_id):
        raise VerificationError("Expected a numeric GitHub App bot user ID")
    run("git", "config", "user.name", login, cwd=root)
    run(
        "git",
        "config",
        "user.email",
        f"{user_id}+{login}@users.noreply.github.com",
        cwd=root,
    )


def _source_token() -> str:
    token = os.environ.get("SOURCE_GH_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise VerificationError("A source verification token is required")
    return token


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise VerificationError("Download redirect is not HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


def required_assets(policy: dict, version: str) -> set[str]:
    names = {
        f"{policy['prefix']}-{version}.zip",
        f"{policy['prefix']}-source-{version}.zip",
        "manifest.json",
        "SHA256SUMS",
    }
    if policy.get("web", True):
        names.add(f"{policy['prefix']}-web-{version}.zip")
    return names


class GitHub:
    def json(self, repository: str, endpoint: str):
        request = urllib.request.Request(
            "https://api.github.com/repos/" + repository + endpoint,
            headers={
                "Authorization": "Bearer " + _source_token(),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def pages(self, repository: str, endpoint: str, limit: int = 10):
        for page in range(1, limit + 1):
            values = self.json(repository, f"{endpoint}?per_page=100&page={page}")
            values = values if isinstance(values, list) else values["workflow_runs"]
            yield from values
            if len(values) < 100:
                return
        raise VerificationError(
            "Release/run lookup exhausted; refusing incomplete reconciliation"
        )

    def download(
        self,
        url: str,
        destination: Path,
        maximum: int = 512 * 1024 * 1024,
        authenticated: bool = False,
    ):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "github.com",
            "api.github.com",
        }:
            raise VerificationError("Unapproved asset origin")
        headers = (
            {"Authorization": "Bearer " + _source_token()} if authenticated else {}
        )
        opener = urllib.request.build_opener(PublicRedirect())
        with (
            opener.open(
                urllib.request.Request(url, headers=headers), timeout=60
            ) as response,
            destination.open("wb") as output,
        ):
            remaining = maximum
            while chunk := response.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise VerificationError(
                        "Downloaded evidence exceeds the safety limit"
                    )
                output.write(chunk)

    def tag(self, repository: str, tag: str) -> str:
        obj = self.json(repository, "/git/ref/tags/" + quote(tag, safe=""))["object"]
        for _ in range(10):
            if obj["type"] == "commit":
                return obj["sha"]
            if obj["type"] != "tag":
                break
            obj = self.json(repository, "/git/tags/" + obj["sha"])["object"]
        raise VerificationError("Release tag does not resolve to a commit")

    def attest(self, repository: str, policy: dict, receipt: Path) -> None:
        signer = repository + "/" + policy["attestation_workflow"]
        signer_options = []
        if policy.get("toolkit_repository") and receipt.is_file():
            evidence = json.loads(receipt.read_text())
            if evidence.get("schema") == 4:
                revision = self.toolkit_revision(repository, policy, evidence)
                signer = (
                    policy["toolkit_repository"]
                    + "/"
                    + policy["toolkit_attestation_workflow"]
                )
                signer_options = ["--signer-digest", revision]
        result = subprocess.check_output(
            [
                "gh",
                "attestation",
                "verify",
                str(receipt),
                "--repo",
                repository,
                "--signer-workflow",
                signer,
                *signer_options,
                "--source-ref",
                "refs/heads/main",
                "--deny-self-hosted-runners",
                "--format",
                "json",
            ],
            text=True,
            env={
                **os.environ,
                "GH_TOKEN": _source_token(),
            },
        )
        # Bind the attesting workflow to an authenticated, successful attach job.
        verified = json.loads(result)
        for item in verified:
            statement = item.get("verificationResult", {}).get("statement", {})
            invocation = (
                statement.get("predicate", {})
                .get("runDetails", {})
                .get("metadata", {})
                .get("invocationId", "")
            )
            match = re.fullmatch(
                r"https://github.com/"
                + re.escape(repository)
                + r"/actions/runs/(\d+)(?:/attempts/(\d+))?",
                invocation,
            )
            if not match:
                continue
            run = self.json(repository, "/actions/runs/" + match[1])
            if (
                run.get("path") not in policy["validation_workflows"]
                or run.get("event") not in {"push", "workflow_dispatch"}
                or run.get("head_branch") != "main"
                or run.get("head_repository", {}).get("full_name") != repository
            ):
                continue
            attempt = match[2] or str(run["run_attempt"])
            jobs = self.json(
                repository,
                f"/actions/runs/{match[1]}/attempts/{attempt}/jobs?per_page=100",
            )["jobs"]
            if any(
                job["name"].split(" / ")[-1] == "attach"
                and job.get("conclusion") == "success"
                for job in jobs
            ):
                return
        raise VerificationError(
            "Receipt attestation lacks an approved successful artifact attachment"
        )

    def toolkit_revision(self, repository: str, policy: dict, receipt: dict) -> str:
        toolkit = receipt.get("toolkit", {})
        expected_repository = policy.get("toolkit_repository")
        if (
            toolkit.get("repository") != expected_repository
            or toolkit.get("schema") != 1
            or not re.fullmatch(r"v\d+\.\d+\.\d+", str(toolkit.get("version", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(toolkit.get("sha256", "")))
        ):
            raise VerificationError("Receipt lacks an approved pinned toolkit")
        source = self.json(
            repository,
            "/contents/toolkit.lock.json?ref=" + quote(receipt["commit"], safe=""),
        )
        if json.loads(base64.b64decode(source["content"])) != toolkit:
            raise VerificationError(
                "Receipt toolkit differs from the tagged plugin source"
            )
        release = self.json(expected_repository, "/releases/tags/" + toolkit["version"])
        if release.get("draft") or release.get("prerelease"):
            raise VerificationError("Toolkit must be a published stable release")
        name = "jellyfin-plugin-toolkit-" + toolkit["version"][1:] + ".tar.gz"
        assets = [a for a in release.get("assets", []) if a.get("name") == name]
        if len(assets) != 1:
            raise VerificationError("Toolkit release archive is missing or ambiguous")
        asset = assets[0]
        expected_url = f"https://github.com/{expected_repository}/releases/download/{toolkit['version']}/{name}"
        if asset.get("browser_download_url") != expected_url:
            raise VerificationError("Toolkit archive URL does not identify its release")
        with tempfile.TemporaryDirectory(prefix="catalog-toolkit-") as temporary:
            archive = Path(temporary) / name
            self.download(expected_url, archive, 100 * 1024 * 1024)
            data = archive.read_bytes()
            if hashlib.sha256(data).hexdigest() != toolkit["sha256"]:
                raise VerificationError("Toolkit archive differs from the plugin lock")
            revision = self.tag(expected_repository, toolkit["version"])
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    expanded = compressed.read(300 * 1024 * 1024 + 1)
                if len(expanded) > 300 * 1024 * 1024:
                    raise VerificationError("Expanded toolkit archive exceeds limit")
                with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as package:
                    entries = [
                        entry
                        for entry in package
                        if entry.name == "toolkit-release.json"
                    ]
                    if (
                        len(entries) != 1
                        or not entries[0].isfile()
                        or entries[0].size > 4096
                    ):
                        raise VerificationError(
                            "Toolkit source provenance is missing or ambiguous"
                        )
                    stream = package.extractfile(entries[0])
                    if stream is None:
                        raise VerificationError("Toolkit source provenance is missing")
                    metadata = json.load(stream)
                if metadata != {
                    "schema": 1,
                    "version": toolkit["version"][1:],
                    "revision": revision,
                }:
                    raise VerificationError(
                        "Toolkit archive revision differs from its published tag"
                    )
            except (OSError, tarfile.TarError, ValueError) as error:
                raise VerificationError(
                    "Invalid toolkit source provenance: " + str(error)
                ) from error
        return revision

    def receipt(
        self,
        repository: str,
        policy: dict,
        release: dict,
        directory: Path,
        commit: str,
        bootstrap: dict | None = None,
    ) -> dict:
        self.bootstrap_proof = None
        assets = {asset["name"]: asset for asset in release["assets"]}
        path = directory / "validation.json"
        if "validation.json" in assets:
            expected = f"https://github.com/{repository}/releases/download/{quote(release['tag_name'], safe='')}/validation.json"
            if assets["validation.json"]["browser_download_url"] != expected:
                raise VerificationError(
                    "Receipt URL does not identify the allowed release"
                )
            self.download(expected, path, 4 * 1024 * 1024)
            self.attest(repository, policy, path)
            return json.loads(path.read_text())
        if bootstrap is not None:
            value = self.bootstrap_receipt(repository, bootstrap, policy)
            self.bootstrap_proof = bootstrap
            return value
        for run in self.pages(repository, "/actions/runs", limit=3):
            if (
                run.get("path") not in policy["validation_workflows"]
                or run.get("event") not in {"push", "workflow_dispatch"}
                or run.get("status") != "completed"
                or run.get("head_branch") != "main"
                or run.get("head_repository", {}).get("full_name") != repository
            ):
                continue
            artifacts = self.json(
                repository, f"/actions/runs/{run['id']}/artifacts?per_page=100"
            )["artifacts"]
            for artifact in artifacts:
                if artifact["name"] not in policy.get(
                    "bootstrap_artifacts",
                    [
                        policy.get(
                            "bootstrap_artifact", "lyricbridge-validation-receipt"
                        )
                    ],
                ) or artifact.get("expired"):
                    continue
                archive = directory / "receipt.zip"
                self.download(
                    artifact["archive_download_url"],
                    archive,
                    maximum=4 * 1024 * 1024,
                    authenticated=True,
                )
                proof = {
                    "artifact_id": artifact["id"],
                    "archive": base64.b64encode(archive.read_bytes()).decode("ascii"),
                }
                value = self.bootstrap_receipt(repository, proof, policy)
                if (
                    value.get("commit") == commit
                    and value.get("repository") == repository
                    and value.get("run_id") == str(run["id"])
                ):
                    self.bootstrap_proof = proof
                    return value
        raise VerificationError(
            "No recoverable trusted receipt; waiting for a verified release"
        )

    def bootstrap_receipt(
        self, repository: str, proof: dict, policy: dict | None = None
    ) -> dict:
        artifact_id = proof.get("artifact_id")
        encoded = proof.get("archive", "")
        if (
            not isinstance(artifact_id, int)
            or artifact_id < 1
            or not isinstance(encoded, str)
            or len(encoded) > 6 * 1024 * 1024
        ):
            raise VerificationError("Invalid bootstrap evidence")
        data = base64.b64decode(encoded, validate=True)
        metadata = self.json(repository, f"/actions/artifacts/{artifact_id}")
        allowed = (policy or {}).get(
            "bootstrap_artifacts",
            [
                (policy or {}).get(
                    "bootstrap_artifact", "lyricbridge-validation-receipt"
                )
            ],
        )
        if (
            metadata.get("name") not in allowed
            or metadata.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest()
        ):
            raise VerificationError(
                "Bootstrap receipt differs from authenticated artifact digest"
            )
        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            if (
                zipped.namelist() != ["validation.json"]
                or zipped.getinfo("validation.json").file_size > 4 * 1024 * 1024
            ):
                raise VerificationError("Unexpected bootstrap receipt archive")
            value = json.loads(zipped.read("validation.json"))
        if value.get("run_id") != str(metadata.get("workflow_run", {}).get("id")):
            raise VerificationError(
                "Bootstrap receipt identifies a different artifact run"
            )
        return value


def check_receipt(
    receipt: dict,
    repository: str,
    commit: str,
    policy: dict,
    run: dict,
    jobs: list[dict],
) -> None:
    if (
        receipt.get("schema")
        not in policy.get("receipt_schemas", [policy["receipt_schema"]])
        or receipt.get("repository") != repository
        or receipt.get("commit") != commit
        or not receipt.get("release")
    ):
        raise VerificationError("Receipt schema, repository or release source mismatch")
    if (
        not re.fullmatch(r"[0-9]+", str(receipt.get("run_id", "")))
        or str(run.get("id")) != receipt["run_id"]
        or run.get("head_branch") != "main"
        or run.get("head_repository", {}).get("full_name") != repository
        or run.get("event") not in {"push", "workflow_dispatch"}
        or run.get("path") not in policy["validation_workflows"]
        or run.get("status") != "completed"
        or run.get("conclusion") not in {"success", "failure"}
        or run.get("head_sha")
        != receipt.get("run_head_sha", receipt.get("workflow_sha"))
    ):
        raise VerificationError("Untrusted validation execution")
    if any(
        receipt.get("groups", {}).get(group) != "success" for group in policy["groups"]
    ) or not any(
        job["name"].split(" / ")[-1] == "validate"
        and job.get("conclusion") == "success"
        for job in jobs
    ):
        raise VerificationError("Release lacks successful artifact and aggregate gates")


def verify_release(
    github: GitHub,
    repository: str,
    policy: dict,
    release: dict,
    known: dict | None = None,
) -> tuple[dict, dict]:
    if release.get("draft") or release.get("prerelease"):
        raise VerificationError("Only published stable releases are catalogued")
    tag = release["tag_name"]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise VerificationError("Unsupported release tag")
    version = tag[1:]
    names = [a["name"] for a in release["assets"]]
    if len(names) != len(set(names)):
        raise VerificationError("Duplicate release assets")
    expected = required_assets(policy, version)
    if not expected.issubset(names):
        raise VerificationError("Published release is missing required assets")
    commit = github.tag(repository, tag)
    with tempfile.TemporaryDirectory(prefix="catalog-verify-") as temporary:
        directory = Path(temporary)
        github.bootstrap_proof = None
        bootstrap = known.get("bootstrap") if known else None
        receipt = (
            github.receipt(repository, policy, release, directory, commit, bootstrap)
            if bootstrap
            else github.receipt(repository, policy, release, directory, commit)
        )
        run_id = receipt.get("run_id", "")
        if not re.fullmatch(r"\d+", str(run_id)):
            raise VerificationError("Invalid receipt run ID")
        run = github.json(repository, f"/actions/runs/{run_id}")
        attempt = receipt.get("run_attempt", 1)
        if not isinstance(attempt, int) or attempt < 1:
            raise VerificationError("Invalid receipt attempt")
        jobs = github.json(
            repository, f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
        )["jobs"]
        check_receipt(receipt, repository, commit, policy, run, jobs)
        hashes = {}
        for asset in release["assets"]:
            if asset["name"] not in expected:
                continue
            name = asset["name"]
            url = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{name}"
            if asset["browser_download_url"] != url:
                raise VerificationError(
                    "Asset URL does not identify the allowed release"
                )
            github.download(
                url,
                directory / name,
                4 * 1024 * 1024 if not name.endswith(".zip") else 512 * 1024 * 1024,
            )
            hashes[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if receipt.get("artifacts") != hashes:
            raise VerificationError("Public assets differ from validated artifacts")
        listed = {}
        for line in (directory / "SHA256SUMS").read_text().splitlines():
            match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
            if not match or match[2] in listed:
                raise VerificationError("Malformed or duplicate checksum entry")
            listed[match[2]] = match[1]
        if listed != {
            name: value for name, value in hashes.items() if name != "SHA256SUMS"
        }:
            raise VerificationError("Release checksum inventory mismatch")
        entries = validate_catalog(
            json.loads((directory / "manifest.json").read_text())
        )
        if (
            len(entries) != 1
            or entries[0]["guid"].lower() != policy["guid"]
            or len(entries[0]["versions"]) != 1
        ):
            raise VerificationError("Unexpected plugin GUID or version inventory")
        entry = entries[0]
        item = entry["versions"][0]
        binary = f"{policy['prefix']}-{version}.zip"
        if (
            item["version"] != version + ".0"
            or item["sourceUrl"]
            != f"https://github.com/{repository}/releases/download/{tag}/{binary}"
            or item["checksum"].lower()
            != hashlib.md5((directory / binary).read_bytes()).hexdigest()
        ):
            raise VerificationError("Plugin version, URL or MD5 mismatch")
        with zipfile.ZipFile(directory / binary) as archive:
            if archive.testzip() is not None or not any(
                name.endswith(".dll") for name in archive.namelist()
            ):
                raise VerificationError(
                    "Plugin ZIP is corrupt or contains no assemblies"
                )
        return entry, {
            "repository": repository,
            "tag": tag,
            "release_id": release["id"],
            "commit": commit,
            "artifacts": hashes,
            "run_id": run_id,
            "entry": entry,
            **({"bootstrap": github.bootstrap_proof} if github.bootstrap_proof else {}),
        }


def withdrawal_keys(withdrawals: list[dict], plugins: dict) -> set[str]:
    keys = set()
    guids = {value["guid"] for value in plugins.values()}
    for item in withdrawals:
        version = item.get("version")
        if not isinstance(version, str) or not CANONICAL_VERSION.fullmatch(version):
            raise VerificationError(
                "Withdrawal version requires four numeric components without leading zeros "
                "(for example 0.1.3.0)"
            )
        if item.get("guid") not in guids or not item.get("reason", "").strip():
            raise VerificationError(
                "Withdrawal requires an allowed GUID, version and reason"
            )
        key = item["guid"] + "/" + version
        if key in keys:
            raise VerificationError("Duplicate withdrawal")
        keys.add(key)
    return keys


def render(history: dict, withdrawals: list[dict], plugins: dict) -> list[dict]:
    excluded = withdrawal_keys(withdrawals, plugins)
    result: list[dict] = []
    for key, record in sorted(
        history.items(),
        key=lambda pair: tuple(map(int, pair[0].split("/")[-1].split("."))),
    ):
        if (
            record["repository"] not in plugins
            or key
            != plugins[record["repository"]]["guid"]
            + "/"
            + record["entry"]["versions"][0]["version"]
        ):
            raise VerificationError("History identity does not match the allowlist")
        if key in excluded:
            continue
        result = merge_catalog(result, [record["entry"]])
        target = next(
            plugin for plugin in result if plugin["guid"] == record["entry"]["guid"]
        )
        for name, value in record["entry"].items():
            if name != "versions":
                target[name] = copy.deepcopy(value)
    for plugin in result:
        plugin["versions"].sort(
            key=lambda version: tuple(map(int, version["version"].split("."))),
            reverse=True,
        )
    return sorted(result, key=lambda plugin: plugin["guid"])


def reconcile(
    github: GitHub, plugins: dict, history: dict, withdrawals: list[dict]
) -> dict:
    result = copy.deepcopy(history)
    excluded = withdrawal_keys(withdrawals, plugins)
    for repository, policy in plugins.items():
        releases = [
            release
            for release in github.pages(repository, "/releases")
            if not release.get("draft") and not release.get("prerelease")
        ]
        releases.sort(key=lambda release: release["published_at"], reverse=True)
        known = [
            record for record in history.values() if record["repository"] == repository
        ]
        # On first onboarding inspect only the newest stable release. Afterwards
        # backfill anything published since onboarding, including missed events.
        baseline = min((record["published_at"] for record in known), default=None)
        candidates = (
            releases[:1]
            if baseline is None
            else [
                release
                for release in releases
                if release["published_at"] >= baseline
                or release["id"] in {record["release_id"] for record in known}
            ]
        )
        public_ids = {release["id"] for release in releases}
        for key, record in history.items():
            if (
                record["repository"] == repository
                and key not in excluded
                and record["release_id"] not in public_ids
            ):
                raise VerificationError(
                    "Catalogued release is unavailable; explicit withdrawal is required"
                )
        for release in candidates:
            if not re.fullmatch(r"v\d+\.\d+\.\d+", release["tag_name"]):
                raise VerificationError("Stable release has an unsupported version tag")
            key = policy["guid"] + "/" + release["tag_name"][1:] + ".0"
            if key in excluded:
                continue
            _, record = verify_release(
                github, repository, policy, release, result.get(key)
            )
            record["published_at"] = release["published_at"]
            old = result.get(key)
            if old and any(
                old.get(field) != record[field] for field in _IMMUTABLE_RELEASE_FIELDS
            ):
                raise VerificationError(
                    "Existing published version changed; explicit withdrawal is required"
                )
            result.setdefault(key, record)
    return result


def verify_history(
    github: GitHub, plugins: dict, history: dict, withdrawals: list[dict]
) -> dict:
    excluded = withdrawal_keys(withdrawals, plugins)
    for key, record in history.items():
        if key in excluded:
            continue
        repository = record["repository"]
        if repository not in plugins:
            raise VerificationError("History contains an unapproved repository")
        release = github.json(repository, f"/releases/{record['release_id']}")
        _, actual = verify_release(
            github, repository, plugins[repository], release, record
        )
        if (
            any(
                record.get(field) != actual[field]
                for field in _IMMUTABLE_RELEASE_FIELDS
            )
            or record["published_at"] != release["published_at"]
        ):
            raise VerificationError("History differs from verified release")
    return history


def command(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()
