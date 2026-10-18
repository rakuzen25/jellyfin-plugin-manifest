"""Allowlisted catalog reconciliation and protected App-owned publication."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from catalog_core import (
    REPOSITORY,
    GitHub,
    VerificationError,
    app_author,
    command,
    configure_app_git_author,
    reconcile,
    render,
    verify_history,
)
from catalog_maintenance_policy import load, scoped_validation

BRANCH = "automation/catalog-update"
FILES = ("manifest.json", "history.json")
TITLE = "chore(catalog): update verified plugin catalog"


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
    matches = [
        issue
        for issue in issues
        if issue["body"].startswith(prefix) and app_author(issue["author"])
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


def owned_pr(pr: dict) -> bool:
    return (
        app_author(pr.get("author", {}))
        and pr.get("baseRefName") == "main"
        and pr.get("isCrossRepository") is False
    )


def publication_prs(state: str = "open") -> list[dict]:
    return json.loads(
        command(
            "gh",
            "pr",
            "list",
            "--repo",
            REPOSITORY,
            "--head",
            BRANCH,
            "--state",
            state,
            "--json",
            "number,author,baseRefName,isCrossRepository,state,headRefOid",
        )
    )


def open_publication_pr() -> dict | None:
    prs = publication_prs()
    if len(prs) > 1 or prs and not owned_pr(prs[0]):
        raise VerificationError(
            "Expected at most one same-repository App PR targeting main"
        )
    return prs[0] if prs else None


def publication_head(root: Path) -> str:
    remote = command("git", "ls-remote", "--heads", "origin", BRANCH, cwd=root)
    if not remote:
        return ""
    observed = remote.split()[0]
    command(
        "git",
        "fetch",
        "origin",
        "+" + BRANCH + ":refs/remotes/origin/" + BRANCH,
        cwd=root,
    )
    head = command("git", "rev-parse", "origin/" + BRANCH, cwd=root)
    if head != observed:
        raise VerificationError("Publication branch advanced during inspection; retry")
    return head


def matches_candidate(root: Path, base_sha: str, head: str, candidate: dict) -> bool:
    try:
        ancestor = command("git", "merge-base", base_sha, head, cwd=root)
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise
    if ancestor != base_sha:
        return False
    changed = set(
        command("git", "diff", "--name-only", base_sha, head, cwd=root).splitlines()
    )
    if not changed or not changed.issubset(FILES):
        return False
    for name in FILES:
        tree = command("git", "ls-tree", head, "--", name, cwd=root)
        if not tree or tree.split()[0] != "100644":
            return False
        try:
            actual = json.loads(command("git", "show", head + ":" + name, cwd=root))
        except json.JSONDecodeError:
            return False
        if json.dumps(actual, sort_keys=True) != json.dumps(
            candidate[name], sort_keys=True
        ):
            return False
    return True


def require_current_main(root: Path, base_sha: str) -> None:
    command("git", "fetch", "origin", "main", cwd=root)
    if command("git", "rev-parse", "origin/main", cwd=root) != base_sha:
        raise VerificationError(
            "Main advanced during publication; retry with fresh verification"
        )


def merge_publication(pr: dict, option: str) -> None:
    args = ["gh", "pr", "merge", str(pr["number"]), "--repo", REPOSITORY, option]
    if option == "--auto":
        args.extend(
            ["--squash", "--subject", TITLE, "--match-head-commit", pr["headRefOid"]]
        )
    command(*args)


def write_publication(root: Path, base_sha: str, candidate: dict, lease: str) -> str:
    if command("git", "status", "--porcelain", "--untracked-files=no", cwd=root):
        raise VerificationError("Publication requires a clean tracked working tree")
    command("git", "checkout", "-B", BRANCH, base_sha, cwd=root)
    for name in FILES:
        (root / name).write_text(json.dumps(candidate[name], indent=4) + "\n")
    configure_app_git_author(root, command)
    command("git", "add", *FILES, cwd=root)
    command("git", "commit", "-m", TITLE, cwd=root)
    head = command("git", "rev-parse", "HEAD", cwd=root)
    if not matches_candidate(root, base_sha, head, candidate):
        raise VerificationError(
            "Generated publication differs from the verified candidate"
        )
    require_current_main(root, base_sha)
    command(
        "git",
        "push",
        "--force-with-lease=refs/heads/" + BRANCH + ":" + lease,
        "origin",
        "HEAD:refs/heads/" + BRANCH,
        cwd=root,
    )
    return head


def publish_pr(root: Path, base_sha: str, before: dict, candidate: dict) -> None:
    if before == candidate:
        return
    pr = open_publication_pr()
    head = publication_head(root)
    if pr and (not head or pr.get("headRefOid") != head):
        raise VerificationError(
            "Pending publication branch is unavailable or advanced; retry"
        )
    matching = bool(head) and matches_candidate(root, base_sha, head, candidate)
    if head and not pr:
        terminal = publication_prs("all")
        if any(old.get("headRefOid") == head and not owned_pr(old) for old in terminal):
            raise VerificationError(
                "Publication branch belongs to a non-App or wrong-base PR"
            )
        terminal_owned = any(
            owned_pr(old)
            and old.get("state") in {"MERGED", "CLOSED"}
            and old.get("headRefOid") == head
            for old in terminal
        )
        if not terminal_owned and not matching:
            raise VerificationError(
                "Unmatched publication orphan; inspect its diff and resolve or remove "
                "the branch as an owner before retrying. It was not changed."
            )
    if pr and not matching:
        merge_publication(pr, "--disable-auto")
    if not matching:
        head = write_publication(root, base_sha, candidate, head)
    else:
        require_current_main(root, base_sha)
        remote = command("git", "ls-remote", "--heads", "origin", BRANCH, cwd=root)
        if not remote or remote.split()[0] != head:
            raise VerificationError(
                "Publication branch advanced before PR recovery; retry"
            )
    if not pr:
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
            TITLE,
            "--body",
            "Verified public stable releases. No policy or withdrawal changes.",
        )
    # GitHub's PR read model can briefly lag behind a successful branch push.
    for attempt in range(4):
        pr = open_publication_pr()
        if pr and pr.get("headRefOid") == head:
            break
        if attempt < 3:
            time.sleep(1)
    else:
        raise VerificationError("Expected one App publication PR at the verified head")
    command(
        "gh", "pr", "edit", str(pr["number"]), "--repo", REPOSITORY, "--title", TITLE
    )
    merge_publication(pr, "--auto")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "validate", "withdraw"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root
    if args.command == "withdraw":
        plugins = json.loads((root / "plugins.json").read_text())
        history = json.loads((root / "history.json").read_text())
        withdrawals = json.loads((root / "withdrawals.json").read_text())
        (root / "manifest.json").write_text(
            json.dumps(render(history, withdrawals, plugins), indent=4) + "\n"
        )
        return
    base_sha = None
    if args.command == "sync":
        command("git", "fetch", "origin", "main", cwd=root)
        base_sha = command("git", "rev-parse", "origin/main", cwd=root)
    state = load(root, base_sha)
    plugins = state["plugins.json"]
    history = state["history.json"]
    withdrawals = state["withdrawals.json"]
    github = GitHub()
    if args.command == "validate" and (base := os.environ.get("CATALOG_BASE", "")):
        if not re.fullmatch(r"[a-f0-9]{40}", base):
            raise VerificationError("Invalid protected comparison source")
        if base != "0" * 40:
            changed = set(
                command(
                    "git", "diff", "--name-only", base, "HEAD", cwd=root
                ).splitlines()
            )
            if scoped_validation(github, load(root, base), load(root), changed):
                print(
                    "PASS: exact maintenance delta; restored versions verified. Unrelated integrity checks remain diagnostic and scheduled."
                )
                return
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
            assert base_sha is not None
            # Publication writes only after verification and branch inspection.
            candidate = {"history.json": verified, "manifest.json": manifest}
            before = {name: state[name] for name in FILES}
            publish_pr(root, base_sha, before, candidate)
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
