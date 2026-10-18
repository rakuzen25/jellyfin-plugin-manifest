"""Explicit version maintenance and App-owned PR orchestration."""

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from catalog_core import (
    REPOSITORY,
    GitHub,
    VerificationError,
    app_author,
    command,
    configure_app_git_author,
)
from catalog_maintenance_policy import FILES, load, rebase_pending, request_change

BRANCH = "automation/catalog-maintenance"
TITLE = "chore(catalog): withdraw or restore catalog versions"


def summary(message: str) -> None:
    print(message)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(message + "\n")


class Outcome(Enum):
    COMPLETED = "completed"
    PENDING = "pending"
    CANCELLED = "cancelled"
    UPDATED = "updated"


@dataclass(frozen=True)
class BranchState:
    pr: dict | None
    lease: str


@dataclass(frozen=True)
class Request:
    operation: str
    repository: str
    version: str
    reason: str
    actor: str
    run_url: str


@dataclass(frozen=True)
class Proposal:
    outcome: Outcome
    candidate: dict
    audit: str


def maintenance_prs(state: str = "open") -> list[dict]:
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
            "number,author,body,url,baseRefName,isCrossRepository,state,headRefOid",
        )
    )


def owned_pr(pr: dict) -> bool:
    return (
        app_author(pr.get("author", {}))
        and pr.get("baseRefName") == "main"
        and pr.get("isCrossRepository") is False
    )


def inspect_branch(root: Path) -> BranchState:
    prs = maintenance_prs()
    remote = command("git", "ls-remote", "--heads", "origin", BRANCH, cwd=root)
    if len(prs) > 1 or prs and not owned_pr(prs[0]):
        raise VerificationError("Maintenance PR is not owned by this App")
    if (
        remote
        and not prs
        and not any(
            owned_pr(pr)
            and pr.get("state") in {"MERGED", "CLOSED"}
            and pr.get("headRefOid") == remote.split()[0]
            for pr in maintenance_prs("all")
        )
    ):
        raise VerificationError("Maintenance branch exists without its owning App PR")
    if prs and not remote:
        raise VerificationError("Pending maintenance branch is unavailable")
    lease = ""
    if remote:
        command(
            "git",
            "fetch",
            "origin",
            "+" + BRANCH + ":refs/remotes/origin/" + BRANCH,
            cwd=root,
        )
        lease = command("git", "rev-parse", "origin/" + BRANCH, cwd=root)
        if lease != remote.split()[0]:
            raise VerificationError(
                "Maintenance branch advanced during ownership checking; retry"
            )
    return BranchState(prs[0] if prs else None, lease)


def pending_state(
    root: Path, base_sha: str, current: dict, branch: BranchState
) -> tuple[dict, list[dict], bool]:
    if not branch.pr:
        return current, current["withdrawals.json"], False
    ancestor = command("git", "merge-base", base_sha, branch.lease, cwd=root)
    changed = set(
        command(
            "git", "diff", "--name-only", ancestor, branch.lease, cwd=root
        ).splitlines()
    )
    if not changed.issubset(FILES):
        raise VerificationError("Pending PR contains non-maintenance files")
    pending = load(root, branch.lease)
    rows = rebase_pending(current, load(root, ancestor), pending)
    return pending, rows, ancestor != base_sha


def plan_request(
    root: Path, base_sha: str, current: dict, branch: BranchState, request: Request
) -> Proposal:
    pending, rows, needs_rebase = pending_state(root, base_sha, current, branch)
    candidate = request_change(
        GitHub(),
        current,
        rows,
        request.operation,
        request.repository,
        request.version,
        request.reason,
        request.actor,
        request.run_url,
    )
    audit = (
        f"\n\n{request.operation} {request.repository} {request.version} by @{request.actor}"
        f"\n\nReason: {json.dumps(request.reason.strip())}\n\nRequest: {request.run_url}"
    )
    if candidate == pending and not needs_rebase and candidate != current:
        outcome = Outcome.PENDING
    elif candidate == current:
        outcome = Outcome.CANCELLED if branch.pr else Outcome.COMPLETED
    else:
        outcome = Outcome.UPDATED
    return Proposal(outcome, candidate, audit)


def merge_request(pr: dict, option: str) -> None:
    args = ["gh", "pr", "merge", str(pr["number"]), "--repo", REPOSITORY, option]
    if option == "--auto":
        args.extend(["--squash", "--subject", TITLE])
    command(*args)


def update_body(pr: dict | None, audit: str) -> dict:
    body = (
        pr["body"]
        if pr
        else "Explicit version maintenance. Only exclusions and the generated manifest may change."
    ) + audit
    with tempfile.TemporaryDirectory(prefix="catalog-maintenance-body-") as directory:
        body_file = Path(directory) / "body.md"
        body_file.write_text(body)
        if pr:
            command(
                "gh",
                "pr",
                "edit",
                str(pr["number"]),
                "--title",
                TITLE,
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
                TITLE,
                "--body-file",
                str(body_file),
            )
    if pr:
        return pr
    prs = maintenance_prs()
    if len(prs) != 1 or not owned_pr(prs[0]):
        raise VerificationError("Expected one App-owned maintenance PR")
    return prs[0]


def write_proposal(
    root: Path, base_sha: str, branch: BranchState, request: Request, candidate: dict
) -> None:
    command("git", "checkout", "-B", BRANCH, base_sha, cwd=root)
    for name in FILES:
        (root / name).write_text(json.dumps(candidate[name], indent=4) + "\n")
    configure_app_git_author(root, command)
    command("git", "add", *sorted(FILES), cwd=root)
    command(
        "git",
        "commit",
        "-m",
        "chore(catalog): "
        + request.operation.lower()
        + " "
        + request.repository
        + " "
        + request.version,
        cwd=root,
    )
    changed = set(
        command("git", "diff", "--name-only", base_sha, "HEAD", cwd=root).splitlines()
    )
    if not changed.issubset(FILES):
        raise VerificationError("Generated maintenance contains protected files")
    command("git", "fetch", "origin", "main", cwd=root)
    if command("git", "rev-parse", "origin/main", cwd=root) != base_sha:
        raise VerificationError(
            "Main advanced during maintenance; retry to refresh the proposal"
        )
    command(
        "git",
        "push",
        "--force-with-lease=refs/heads/" + BRANCH + ":" + branch.lease,
        "origin",
        "HEAD:refs/heads/" + BRANCH,
        cwd=root,
    )


def publish_proposal(
    root: Path, base_sha: str, branch: BranchState, request: Request, proposal: Proposal
) -> None:
    if proposal.outcome == Outcome.COMPLETED:
        summary("No change: the requested state is already completed.")
        return
    if proposal.outcome == Outcome.PENDING:
        pr = update_body(branch.pr, proposal.audit)
        merge_request(pr, "--auto")
        summary(
            "No change: the requested state is already completed or pending. PR: "
            + pr["url"]
        )
        return
    # Failures after this point leave any pending PR's auto-merge disabled.
    if branch.pr:
        merge_request(branch.pr, "--disable-auto")
    if proposal.outcome == Outcome.CANCELLED:
        pr = update_body(branch.pr, proposal.audit)
        command("gh", "pr", "close", str(pr["number"]), "--repo", REPOSITORY)
        summary("Pending requests cancel out; no main changes. PR: " + pr["url"])
        return
    write_proposal(root, base_sha, branch, request, proposal.candidate)
    pr = update_body(branch.pr, proposal.audit)
    merge_request(pr, "--auto")
    summary(
        "Maintenance proposed; auto-merge waits for catalog-validate and an up-to-date branch. PR: "
        + pr["url"]
    )


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
    branch = inspect_branch(root)
    request = Request(
        operation,
        repository,
        version,
        reason,
        os.environ.get("GITHUB_TRIGGERING_ACTOR") or os.environ["GITHUB_ACTOR"],
        "https://github.com/"
        + REPOSITORY
        + "/actions/runs/"
        + os.environ["GITHUB_RUN_ID"],
    )
    proposal = plan_request(root, base_sha, current, branch, request)
    publish_proposal(root, base_sha, branch, request, proposal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("operation", "repository", "version", "reason"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    maintain(Path.cwd(), args.operation, args.repository, args.version, args.reason)
