"""Shared catalog verification, reconciliation and infrastructure boundaries."""

import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
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
    if policy["web"]:
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
        _source_token()
        evidence = json.loads(receipt.read_text())
        if evidence.get("schema") != 7:
            raise VerificationError("Only schema 7 public receipts are supported")
        revision = self.toolkit_revision(repository, policy, evidence)
        signer = (
            policy["toolkit_repository"] + "/" + policy["toolkit_attestation_workflow"]
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
            not expected_repository
            or not isinstance(toolkit, dict)
            or toolkit.get("distribution") != "jellyfin-plugin-toolkit"
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(toolkit.get("version", "")))
            or any(
                not re.fullmatch(r"[a-f0-9]{64}", str(toolkit.get(key, "")))
                for key in ("sha256", "python_lock_sha256")
            )
        ):
            raise VerificationError("Receipt lacks an approved pinned toolkit")
        version = toolkit["version"]
        tag = "v" + version
        wheel = f"jellyfin_plugin_toolkit-{version}-py3-none-any.whl"
        sdist = f"jellyfin_plugin_toolkit-{version}.tar.gz"
        base = f"https://github.com/{expected_repository}/releases/download/{tag}/"
        if toolkit.get("source") != base + wheel:
            raise VerificationError(
                "Toolkit wheel URL does not identify the approved release"
            )
        source = self.json(
            repository, "/contents/uv.lock?ref=" + quote(receipt["commit"], safe="")
        )
        try:
            payload = base64.b64decode(source["content"])
            lock = tomllib.loads(payload.decode())
            entries = [
                entry
                for entry in lock.get("package", [])
                if entry.get("name", "").replace("_", "-") == "jellyfin-plugin-toolkit"
            ]
            if (
                hashlib.sha256(payload).hexdigest() != toolkit["python_lock_sha256"]
                or len(entries) != 1
                or entries[0].get("version") != version
                or entries[0].get("source") != {"url": base + wheel}
                or [item.get("hash") for item in entries[0].get("wheels", [])]
                != ["sha256:" + toolkit["sha256"]]
                or set(toolkit)
                != {"distribution", "version", "source", "sha256", "python_lock_sha256"}
            ):
                raise VerificationError(
                    "Receipt toolkit differs from the tagged plugin source"
                )
        except (KeyError, TypeError, ValueError) as error:
            raise VerificationError(
                "Invalid tagged uv.lock toolkit provenance: " + str(error)
            ) from error
        release = self.json(expected_repository, "/releases/tags/" + tag)
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise VerificationError("Toolkit must be a published stable release")
        names = (wheel, sdist, "SHA256SUMS", "build-provenance.json")
        for name in names:
            assets = [
                asset
                for asset in release.get("assets", [])
                if asset.get("name") == name
            ]
            if len(assets) != 1 or assets[0].get("browser_download_url") != base + name:
                raise VerificationError(
                    "Toolkit release asset is missing, ambiguous or has an invalid URL"
                )
        revision = self.tag(expected_repository, tag)
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise VerificationError("Toolkit tag must resolve to an immutable revision")
        with tempfile.TemporaryDirectory(prefix="catalog-toolkit-") as temporary:
            directory = Path(temporary)
            for name in names:
                self.download(
                    base + name,
                    directory / name,
                    100 * 1024 * 1024 if name in (wheel, sdist) else 64 * 1024,
                )
            hashes = {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in (wheel, sdist)
            }
            if hashes[wheel] != toolkit["sha256"]:
                raise VerificationError("Toolkit wheel differs from the plugin lock")
            try:
                listed = {}
                for line in (directory / "SHA256SUMS").read_text().splitlines():
                    match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
                    if not match or match[2] in listed:
                        raise VerificationError(
                            "Malformed or duplicate toolkit checksum entry"
                        )
                    listed[match[2]] = match[1]
                if listed != hashes:
                    raise VerificationError(
                        "Toolkit distribution checksum inventory mismatch"
                    )
                if json.loads((directory / "build-provenance.json").read_text()) != {
                    "schema": 2,
                    "version": version,
                    "revision": revision,
                    "files": hashes,
                }:
                    raise VerificationError(
                        "Toolkit build provenance differs from its published tag or distributions"
                    )
            except (OSError, ValueError) as error:
                raise VerificationError(
                    "Invalid toolkit release provenance: " + str(error)
                ) from error
        return revision

    def receipt(
        self, repository: str, policy: dict, release: dict, directory: Path, commit: str
    ) -> dict:
        assets = [
            asset for asset in release["assets"] if asset["name"] == "validation.json"
        ]
        expected = f"https://github.com/{repository}/releases/download/{quote(release['tag_name'], safe='')}/validation.json"
        if len(assets) != 1 or assets[0]["browser_download_url"] != expected:
            raise VerificationError(
                "Release requires one public validation.json receipt at its canonical URL"
            )
        path = directory / "validation.json"
        self.download(expected, path, 4 * 1024 * 1024)
        evidence = json.loads(path.read_text())
        if evidence.get("schema") != 7:
            raise VerificationError("Only schema 7 public receipts are supported")
        self.attest(repository, policy, path)
        return evidence


def check_receipt(
    receipt: dict,
    repository: str,
    commit: str,
    policy: dict,
    run: dict,
    jobs: list[dict],
) -> None:
    if (
        receipt.get("schema") != 7
        or receipt.get("repository") != repository
        or receipt.get("commit") != commit
        or receipt.get("release") is not True
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
        or run.get("head_sha") != receipt.get("run_head_sha")
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
        receipt = github.receipt(repository, policy, release, directory, commit)
        run_id = receipt.get("run_id", "")
        if not re.fullmatch(r"\d+", str(run_id)):
            raise VerificationError("Invalid receipt run ID")
        run = github.json(repository, f"/actions/runs/{run_id}")
        attempt = receipt.get("run_attempt")
        if type(attempt) is not int or attempt < 1:
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
            _, record = verify_release(github, repository, policy, release)
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
        _, actual = verify_release(github, repository, plugins[repository], release)
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
