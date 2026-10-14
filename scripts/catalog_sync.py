"""Allowlisted release verification and idempotent shared-catalog reconciliation.

Remote inputs are data. Never execute plugin code, extract archives, or trust a
notification as authority. Publication uses GitHub's protected PR merge path.
"""

import argparse
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
from urllib.parse import quote, urlparse
import zipfile

from catalog import merge_catalog, validate_catalog

REPOSITORY = "rakuzen25/jellyfin-plugin-manifest"
BRANCH = "automation/catalog-update"
FILES = ("manifest.json", "history.json")


class VerificationError(ValueError):
    pass


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise VerificationError("Download redirect is not HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def json(self, repository: str, endpoint: str):
        request = urllib.request.Request(
            "https://api.github.com/repos/" + repository + endpoint,
            headers={
                "Authorization": "Bearer "
                + os.environ.get("SOURCE_GH_TOKEN", os.environ["GH_TOKEN"]),
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
            {
                "Authorization": "Bearer "
                + os.environ.get("SOURCE_GH_TOKEN", os.environ["GH_TOKEN"])
            }
            if authenticated
            else {}
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
        result = subprocess.check_output(
            [
                "gh",
                "attestation",
                "verify",
                str(receipt),
                "--repo",
                repository,
                "--signer-workflow",
                repository + "/" + policy["attestation_workflow"],
                "--source-ref",
                "refs/heads/main",
                "--deny-self-hosted-runners",
                "--format",
                "json",
            ],
            text=True,
            env={
                **os.environ,
                "GH_TOKEN": os.environ.get(
                    "SOURCE_GH_TOKEN", os.environ.get("GH_TOKEN", "")
                ),
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
            value = self.bootstrap_receipt(repository, bootstrap)
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
                if artifact["name"] != "lyricbridge-validation-receipt" or artifact.get(
                    "expired"
                ):
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
                value = self.bootstrap_receipt(repository, proof)
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

    def bootstrap_receipt(self, repository: str, proof: dict) -> dict:
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
        if (
            metadata.get("name") != "lyricbridge-validation-receipt"
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
        receipt.get("schema") != policy["receipt_schema"]
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
    expected = {
        f"{policy['prefix']}-{version}.zip",
        f"{policy['prefix']}-web-{version}.zip",
        f"{policy['prefix']}-source-{version}.zip",
        "manifest.json",
        "SHA256SUMS",
    }
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
        if (
            item.get("guid") not in guids
            or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", item.get("version", ""))
            or not item.get("reason", "").strip()
        ):
            raise VerificationError(
                "Withdrawal requires an allowed GUID, version and reason"
            )
        key = item["guid"] + "/" + item["version"]
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
            entry, record = verify_release(
                github, repository, policy, release, result.get(key)
            )
            record["published_at"] = release["published_at"]
            old = result.get(key)
            if old and any(
                old.get(field) != record[field]
                for field in (
                    "repository",
                    "tag",
                    "release_id",
                    "commit",
                    "artifacts",
                    "entry",
                )
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
                for field in (
                    "repository",
                    "tag",
                    "release_id",
                    "commit",
                    "artifacts",
                    "entry",
                )
            )
            or record["published_at"] != release["published_at"]
        ):
            raise VerificationError("History differs from verified release")
    return history


def command(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def problem_issue(message: str | None) -> None:
    prefix = "<!-- catalog-integrity:"
    marker = prefix + hashlib.sha256((message or "").encode()).hexdigest()[:16] + " -->"
    issues = json.loads(
        command(
            "gh",
            "issue",
            "list",
            "--repo",
            REPOSITORY,
            "--state",
            "open",
            "--json",
            "number,body,author",
            "--limit",
            "100",
        )
    )
    bot = os.environ["PLUGIN_APP_SLUG"] + "[bot]"
    matches = [
        issue
        for issue in issues
        if issue["body"].startswith(prefix) and issue["author"]["login"] == bot
    ]
    if message:
        body = (
            marker
            + "\nCatalog reconciliation failed. The served manifest was preserved.\n\n"
            + message
            + "\n\n"
            + f"https://github.com/{REPOSITORY}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
        )
        if not matches:
            command(
                "gh",
                "issue",
                "create",
                "--repo",
                REPOSITORY,
                "--title",
                "Catalog publication or integrity failure",
                "--body",
                body,
            )
        elif message not in matches[0]["body"]:
            command(
                "gh",
                "issue",
                "edit",
                str(matches[0]["number"]),
                "--repo",
                REPOSITORY,
                "--body",
                body,
            )
    else:
        for issue in matches:
            command("gh", "issue", "close", str(issue["number"]), "--repo", REPOSITORY)


def publish_pr(root: Path, before: dict, after: dict) -> None:
    if before == after:
        return
    prs = json.loads(
        command(
            "gh",
            "pr",
            "list",
            "--repo",
            REPOSITORY,
            "--head",
            BRANCH,
            "--state",
            "open",
            "--json",
            "number,author",
        )
    )
    if prs and prs[0]["author"]["login"] != os.environ["PLUGIN_APP_SLUG"] + "[bot]":
        raise VerificationError("Catalog branch belongs to a non-App PR")
    if command("git", "ls-remote", "--heads", "origin", BRANCH, cwd=root):
        if not prs:
            raise VerificationError("Catalog branch exists without its owning App PR")
        command(
            "git",
            "fetch",
            "origin",
            BRANCH + ":refs/remotes/origin/" + BRANCH,
            cwd=root,
        )
    command("git", "config", "user.name", "plugin-catalog[bot]", cwd=root)
    command(
        "git",
        "config",
        "user.email",
        "plugin-catalog[bot]@users.noreply.github.com",
        cwd=root,
    )
    command("git", "checkout", "-B", BRANCH, cwd=root)
    command("git", "add", *FILES, cwd=root)
    command("git", "commit", "-m", "Update verified plugin catalog", cwd=root)
    changed = command(
        "git", "diff", "--name-only", "origin/main...HEAD", cwd=root
    ).splitlines()
    if not set(changed).issubset(FILES):
        raise VerificationError("Generated PR includes policy or withdrawal changes")
    command("git", "push", "--force-with-lease", "origin", BRANCH, cwd=root)
    if not prs:
        command(
            "gh",
            "pr",
            "create",
            "--repo",
            REPOSITORY,
            "--head",
            BRANCH,
            "--base",
            "main",
            "--title",
            "Update verified plugin catalog",
            "--body",
            "Verified public stable releases. No policy or withdrawal changes.",
        )
        prs = json.loads(
            command(
                "gh",
                "pr",
                "list",
                "--repo",
                REPOSITORY,
                "--head",
                BRANCH,
                "--json",
                "number",
            )
        )
    command(
        "gh",
        "pr",
        "merge",
        str(prs[0]["number"]),
        "--repo",
        REPOSITORY,
        "--auto",
        "--squash",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "validate", "withdraw"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root
    plugins = json.loads((root / "plugins.json").read_text())
    history = json.loads((root / "history.json").read_text())
    withdrawals = json.loads((root / "withdrawals.json").read_text())
    if args.command == "withdraw":
        (root / "manifest.json").write_text(
            json.dumps(render(history, withdrawals, plugins), indent=4) + "\n"
        )
        return
    github = GitHub()
    try:
        verified = (
            verify_history(github, plugins, history, withdrawals)
            if args.command == "validate"
            else reconcile(github, plugins, history, withdrawals)
        )
        manifest = render(verified, withdrawals, plugins)
        if args.command == "validate":
            if verified != history or manifest != json.loads(
                (root / "manifest.json").read_text()
            ):
                raise VerificationError(
                    "Catalog is not the verified deterministic projection of history"
                )
        else:
            # All remote checks finish before either local candidate is written.
            (root / "history.json").write_text(json.dumps(verified, indent=4) + "\n")
            (root / "manifest.json").write_text(json.dumps(manifest, indent=4) + "\n")
            publish_pr(root, history, verified)
            problem_issue(None)
    except Exception as error:
        if args.command == "sync":
            problem_issue(
                str(error)
                if isinstance(error, VerificationError)
                else type(error).__name__
                + " during catalog reconciliation; inspect the workflow log."
            )
        raise


if __name__ == "__main__":
    main()
