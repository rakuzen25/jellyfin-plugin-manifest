"""Explicit version maintenance, protected-base validation and App-owned PRs."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile

from catalog_sync import (
    GitHub,
    REPOSITORY,
    VerificationError,
    command,
    render,
    verify_history,
    withdrawal_keys,
)

BRANCH = "automation/catalog-maintenance"
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
        or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            version,
        )
    ):
        raise VerificationError(
            "Choose an allowed plugin and exact four-component version"
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


def summary(message: str) -> None:
    print(message)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(message + "\n")


def maintain(
    root: Path, operation: str, repository: str, version: str, reason: str
) -> None:
    if (
        os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
    ):
        raise VerificationError(
            "Maintenance must be dispatched from main in the catalog repository"
        )
    command("git", "fetch", "origin", "main", cwd=root)
    base_sha = command("git", "rev-parse", "origin/main", cwd=root)
    current = load(root, base_sha)
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
            "number,author,body,url,baseRefName,isCrossRepository",
        )
    )
    remote = command("git", "ls-remote", "--heads", "origin", BRANCH, cwd=root)
    if (
        len(prs) > 1
        or prs
        and (
            prs[0]["author"]["login"] != os.environ["PLUGIN_APP_SLUG"] + "[bot]"
            or prs[0]["baseRefName"] != "main"
            or prs[0]["isCrossRepository"]
        )
    ):
        raise VerificationError("Maintenance PR is not owned by this App")
    if remote and not prs:
        terminal = json.loads(
            command(
                "gh",
                "pr",
                "list",
                "--repo",
                REPOSITORY,
                "--head",
                BRANCH,
                "--state",
                "all",
                "--json",
                "author,baseRefName,isCrossRepository,state,headRefOid",
            )
        )
        if not any(
            pr["author"]["login"] == os.environ["PLUGIN_APP_SLUG"] + "[bot]"
            and pr["baseRefName"] == "main"
            and not pr["isCrossRepository"]
            and pr["state"] in {"MERGED", "CLOSED"}
            and pr["headRefOid"] == remote.split()[0]
            for pr in terminal
        ):
            raise VerificationError(
                "Maintenance branch exists without its owning App PR"
            )
    if prs and not remote:
        raise VerificationError("Pending maintenance branch is unavailable")
    pending_rows = current["withdrawals.json"]
    pending = current
    lease = ""
    needs_rebase = False
    if remote:
        command(
            "git",
            "fetch",
            "origin",
            BRANCH + ":refs/remotes/origin/" + BRANCH,
            cwd=root,
        )
        lease = command("git", "rev-parse", "origin/" + BRANCH, cwd=root)
        if lease != remote.split()[0]:
            raise VerificationError(
                "Maintenance branch advanced during ownership checking; retry"
            )
    if remote and prs:
        ancestor = command("git", "merge-base", base_sha, lease, cwd=root)
        needs_rebase = ancestor != base_sha
        changed = set(
            command(
                "git", "diff", "--name-only", ancestor, lease, cwd=root
            ).splitlines()
        )
        if not changed.issubset(FILES):
            raise VerificationError("Pending PR contains non-maintenance files")
        pending = load(root, lease)
        pending_rows = rebase_pending(current, load(root, ancestor), pending)
    actor = os.environ.get("GITHUB_TRIGGERING_ACTOR") or os.environ["GITHUB_ACTOR"]
    run_url = (
        "https://github.com/"
        + REPOSITORY
        + "/actions/runs/"
        + os.environ["GITHUB_RUN_ID"]
    )
    candidate = request_change(
        GitHub(),
        current,
        pending_rows,
        operation,
        repository,
        version,
        reason,
        actor,
        run_url,
    )
    audit = (
        "\n\n"
        + operation
        + " "
        + repository
        + " "
        + version
        + " by @"
        + actor
        + "\n\nReason: "
        + json.dumps(reason.strip())
        + "\n\nRequest: "
        + run_url
    )
    if candidate == pending and not needs_rebase and candidate != current:
        with tempfile.TemporaryDirectory(
            prefix="catalog-maintenance-body-"
        ) as directory:
            body_file = Path(directory) / "body.md"
            body_file.write_text(prs[0]["body"] + audit)
            command(
                "gh",
                "pr",
                "edit",
                str(prs[0]["number"]),
                "--repo",
                REPOSITORY,
                "--body-file",
                str(body_file),
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
        summary(
            "No change: the requested state is already completed or pending."
            + (" PR: " + prs[0]["url"] if prs else "")
        )
        return
    if candidate == current and not prs:
        summary("No change: the requested state is already completed.")
        return
    # Stop auto-merge before refreshing either branch or audit trail. A failed
    # refresh stays disabled; the next explicit request can safely resume it.
    if prs:
        command(
            "gh",
            "pr",
            "merge",
            str(prs[0]["number"]),
            "--repo",
            REPOSITORY,
            "--disable-auto",
        )
    if candidate == current and prs:
        with tempfile.TemporaryDirectory(
            prefix="catalog-maintenance-body-"
        ) as directory:
            body_file = Path(directory) / "body.md"
            body_file.write_text(prs[0]["body"] + audit)
            command(
                "gh",
                "pr",
                "edit",
                str(prs[0]["number"]),
                "--repo",
                REPOSITORY,
                "--body-file",
                str(body_file),
            )
        command("gh", "pr", "close", str(prs[0]["number"]), "--repo", REPOSITORY)
        summary("Pending requests cancel out; no main changes. PR: " + prs[0]["url"])
        return
    command("git", "checkout", "-B", BRANCH, base_sha, cwd=root)
    for name in FILES:
        (root / name).write_text(json.dumps(candidate[name], indent=4) + "\n")
    command("git", "config", "user.name", "plugin-catalog[bot]", cwd=root)
    command(
        "git",
        "config",
        "user.email",
        "plugin-catalog[bot]@users.noreply.github.com",
        cwd=root,
    )
    command("git", "add", *sorted(FILES), cwd=root)
    command(
        "git", "commit", "-m", operation + " " + repository + " " + version, cwd=root
    )
    if not set(
        command("git", "diff", "--name-only", base_sha, "HEAD", cwd=root).splitlines()
    ).issubset(FILES):
        raise VerificationError("Generated maintenance contains protected files")
    command("git", "fetch", "origin", "main", cwd=root)
    if command("git", "rev-parse", "origin/main", cwd=root) != base_sha:
        raise VerificationError(
            "Main advanced during maintenance; retry to refresh the proposal"
        )
    command(
        "git",
        "push",
        "--force-with-lease=refs/heads/" + BRANCH + ":" + lease,
        "origin",
        "HEAD:refs/heads/" + BRANCH,
        cwd=root,
    )
    body = (
        prs[0]["body"]
        if prs
        else "Explicit version maintenance. Only exclusions and the generated manifest may change."
    ) + audit
    with tempfile.TemporaryDirectory(prefix="catalog-maintenance-body-") as directory:
        body_file = Path(directory) / "body.md"
        body_file.write_text(body)
        if prs:
            command(
                "gh",
                "pr",
                "edit",
                str(prs[0]["number"]),
                "--repo",
                REPOSITORY,
                "--body-file",
                str(body_file),
            )
        else:
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
                "Withdraw or restore catalog versions",
                "--body-file",
                str(body_file),
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
                    "--state",
                    "open",
                    "--json",
                    "number,url",
                )
            )
    if len(prs) != 1:
        raise VerificationError("Expected one maintenance PR")
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
    summary(
        "Maintenance proposed; auto-merge waits for catalog-validate and an up-to-date branch. PR: "
        + prs[0]["url"]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("operation", "repository", "version", "reason"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    maintain(Path.cwd(), args.operation, args.repository, args.version, args.reason)
