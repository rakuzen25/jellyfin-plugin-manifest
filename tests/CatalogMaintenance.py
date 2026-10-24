"""Explicit requests accumulate safely; removals do not depend on remote health."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_core as core
import catalog_maintenance as maintenance
import catalog_maintenance_policy as policy
import catalog_sync as sync
from CatalogPublication import GUID, POLICY, SOURCE, FakeGitHub

RUN = "https://github.com/" + core.REPOSITORY + "/actions/runs/123"
ENV: dict[str, Any] = {
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REPOSITORY": core.REPOSITORY,
    "GITHUB_ACTOR": "owner",
    "GITHUB_TRIGGERING_ACTOR": "owner",
    "GITHUB_RUN_ID": "123",
    "PLUGIN_APP_SLUG": "test-catalog-app",
}


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.plugins = {SOURCE: POLICY}
        history = core.reconcile(self.github, self.plugins, {}, [])
        self.current: dict[str, Any] = {
            "plugins.json": self.plugins,
            "history.json": history,
            "withdrawals.json": [],
            "manifest.json": core.render(history, [], self.plugins),
        }

    def request(
        self,
        state: dict[str, Any] | None = None,
        pending: list[dict[str, Any]] | None = None,
        operation: str = "Withdraw",
        version: str = "0.1.3.0",
        **overrides: Any,
    ) -> dict[str, Any]:
        state = self.current if state is None else state
        args: dict[str, Any] = {
            "repository": SOURCE,
            "reason": "Owner decision",
            "actor": "owner",
            "run_url": RUN,
        }
        args.update(overrides)
        return policy.request_change(
            self.github,
            state,
            state["withdrawals.json"] if pending is None else pending,
            operation,
            version=version,
            **args,
        )

    def test_withdrawal_and_duplicate_are_local_and_preserve_history(self) -> None:
        before = copy.deepcopy(self.current)
        with patch.object(
            policy,
            "verify_history",
            side_effect=OSError("unrelated provider unavailable"),
        ) as verify:
            withdrawn = self.request()
            self.assertEqual(withdrawn["manifest.json"], [])
            self.assertEqual(self.request(withdrawn), withdrawn)
            verify.assert_not_called()
        self.assertEqual(self.current, before)
        self.assertEqual(withdrawn["history.json"], before["history.json"])
        self.assertEqual(withdrawn["withdrawals.json"][0]["requestedBy"], "owner")
        self.assertEqual(withdrawn["withdrawals.json"][0]["requestUrl"], RUN)

    def test_restoration_verifies_only_target_and_failure_preserves_input(self) -> None:
        withdrawn = self.request()
        before = copy.deepcopy(withdrawn)
        restored = self.request(withdrawn, operation="Restore")
        self.assertEqual(restored["manifest.json"], self.current["manifest.json"])
        with (
            patch.object(
                policy,
                "verify_history",
                side_effect=core.VerificationError("changed archive"),
            ),
            self.assertRaises(core.VerificationError),
        ):
            self.request(withdrawn, operation="Restore")
        self.assertEqual(withdrawn, before)
        with patch.object(policy, "verify_history") as verify:
            self.request(withdrawn, operation="Restore")
            self.assertEqual(set(verify.call_args.args[2]), {GUID + "/0.1.3.0"})
        with patch.object(policy, "verify_history") as verify:
            self.assertEqual(
                self.request(self.current, operation="Restore"), self.current
            )
            verify.assert_not_called()

    def test_unknown_targets_invalid_versions_and_missing_audit_fail(self) -> None:
        invalid_overrides: tuple[dict[str, Any], ...] = (
            {"repository": "other/repo"},
            {"reason": " "},
            {"actor": "bad actor"},
            {"run_url": "https://example.invalid/123"},
        )
        for overrides in invalid_overrides:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(core.VerificationError),
            ):
                self.request(**overrides)
        for version in ("0.1.3", "01.1.3.0", "0.1.99.0"):
            with (
                self.subTest(version=version),
                self.assertRaises(core.VerificationError),
            ):
                self.request(version=version)

    def test_multiple_pending_requests_rebase_and_latest_successful_operation_wins(
        self,
    ) -> None:
        pending = self.request()
        self.github.add("0.1.4", "2026-10-15T00:00:00Z", 2)
        latest = copy.deepcopy(self.current)
        latest["history.json"] = core.reconcile(
            self.github, self.plugins, latest["history.json"], []
        )
        latest["manifest.json"] = core.render(latest["history.json"], [], self.plugins)
        pending_rows = policy.rebase_pending(latest, self.current, pending)
        both = self.request(latest, pending_rows, version="0.1.4.0")
        self.assertEqual(
            {row["version"] for row in both["withdrawals.json"]}, {"0.1.3.0", "0.1.4.0"}
        )
        last = self.request(latest, both["withdrawals.json"], operation="Restore")
        self.assertEqual(
            [row["version"] for row in last["withdrawals.json"]], ["0.1.4.0"]
        )
        with (
            patch.object(
                policy, "verify_history", side_effect=OSError("status unavailable")
            ),
            self.assertRaises(OSError),
        ):
            self.request(
                self.request(latest), both["withdrawals.json"], operation="Restore"
            )
        self.assertEqual(len(both["withdrawals.json"]), 2)

    def test_newer_main_conflict_and_pending_policy_changes_are_rejected(self) -> None:
        pending = self.request()
        latest = copy.deepcopy(pending)
        latest["withdrawals.json"][0]["reason"] = "Newer explicit owner decision"
        with self.assertRaisesRegex(core.VerificationError, "conflicting"):
            policy.rebase_pending(latest, self.current, pending)
        pending["plugins.json"][SOURCE]["guid"] = "changed"
        with self.assertRaisesRegex(core.VerificationError, "protected"):
            policy.rebase_pending(self.current, self.current, pending)

    def test_gate_rejects_extra_changes_and_unauthorized_manifest_edits(self) -> None:
        withdrawn = self.request()
        self.assertFalse(
            policy.scoped_validation(
                self.github, self.current, withdrawn, {"plugins.json"}
            )
        )
        for name in ("plugins.json", "history.json"):
            bad = copy.deepcopy(withdrawn)
            bad[name] = {}
            with self.subTest(name=name), self.assertRaises(core.VerificationError):
                policy.scoped_validation(self.github, self.current, bad, policy.FILES)
        bad = copy.deepcopy(self.current)
        bad["manifest.json"] = []
        with self.assertRaisesRegex(core.VerificationError, "manifest"):
            policy.scoped_validation(self.github, self.current, bad, policy.FILES)

    def run_mock(
        self,
        operation: str = "Withdraw",
        pending: dict[str, Any] | None = None,
        foreign: bool = False,
        advanced: bool = False,
        extra: bool = False,
        stale: bool = True,
        terminal: bool = False,
    ) -> tuple[list[Any], Exception | None]:
        base = "a" * 40
        head = "b" * 40
        ancestor = "c" * 40
        prs = (
            []
            if pending is None
            else [
                {
                    "number": 7,
                    "url": "https://github.com/" + core.REPOSITORY + "/pull/7",
                    "author": {
                        "login": "other-app[bot]"
                        if foreign
                        else "test-catalog-app[bot]"
                    },
                    "body": "Previous authorized request",
                    "baseRefName": "main",
                    "isCrossRepository": False,
                }
            ]
        )
        revs = 0
        self.audit_bodies: list[str] = []

        def command(*args: str, **kwargs: Any) -> str:
            nonlocal revs
            if "--body-file" in args:
                self.audit_bodies.append(
                    Path(args[args.index("--body-file") + 1]).read_text()
                )
            if args[:3] == ("gh", "api", "users/test-catalog-app[bot]"):
                return "123456"
            if args[:3] == ("gh", "pr", "list"):
                if terminal and "all" in args:
                    return json.dumps(
                        [
                            {
                                "author": {
                                    "login": "other-app[bot]"
                                    if foreign
                                    else "test-catalog-app[bot]"
                                },
                                "baseRefName": "main",
                                "isCrossRepository": False,
                                "state": "MERGED",
                                "headRefOid": head,
                            }
                        ]
                    )
                return json.dumps(
                    prs
                    or (
                        [
                            {
                                "number": 8,
                                "author": {
                                    "login": "app/test-catalog-app",
                                    "is_bot": True,
                                },
                                "baseRefName": "main",
                                "isCrossRepository": False,
                                "url": "https://github.com/"
                                + core.REPOSITORY
                                + "/pull/8",
                            }
                        ]
                        if revs > 1
                        else []
                    )
                )
            if args[:2] == ("git", "ls-remote"):
                return head if pending is not None or terminal else ""
            if args[:2] == ("git", "rev-parse"):
                if args[2] == "origin/main":
                    revs += 1
                    return "d" * 40 if advanced and revs > 1 else base
                return head
            if args[:2] == ("git", "merge-base"):
                return ancestor if stale else base
            if args[:2] == ("git", "diff"):
                return "plugins.json" if extra else "manifest.json\nwithdrawals.json"
            return ""

        def load(root: Path, ref: str | None = None) -> dict[str, Any] | None:
            return copy.deepcopy(pending if ref == head else self.current)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, ENV),
            patch.object(maintenance, "GitHub", return_value=self.github),
            patch.object(maintenance, "load", side_effect=load),
            patch.object(maintenance, "command", side_effect=command) as calls,
            patch.object(maintenance, "summary"),
        ):
            try:
                maintenance.maintain(
                    Path(directory), operation, SOURCE, "0.1.3.0", "Owner decision"
                )
            except core.VerificationError as error:
                return calls.call_args_list, error
            return calls.call_args_list, None

    def test_git_request_uses_lease_protected_branch_and_automatic_protected_merge(
        self,
    ) -> None:
        calls, error = self.run_mock()
        self.assertIsNone(error)
        arguments = [call.args for call in calls]
        self.assertIn(
            ("git", "config", "user.name", "test-catalog-app[bot]"), arguments
        )
        self.assertIn(
            (
                "git",
                "config",
                "user.email",
                "123456+test-catalog-app[bot]@users.noreply.github.com",
            ),
            arguments,
        )
        self.assertTrue(
            any(
                args[:2] == ("git", "push")
                and args[2]
                == "--force-with-lease=refs/heads/automation/catalog-maintenance:"
                for args in arguments
            )
        )
        self.assertTrue(
            any(
                args[:3] == ("gh", "pr", "merge") and "--auto" in args
                for args in arguments
            )
        )
        self.assertFalse(
            any(
                args[:2] == ("git", "push") and args[-1] == "main" for args in arguments
            )
        )

    def test_cancelled_out_pending_requests_close_without_a_git_commit(self) -> None:
        calls, error = self.run_mock(operation="Restore", pending=self.request())
        self.assertIsNone(error)
        self.assertTrue(any(call.args[:3] == ("gh", "pr", "close") for call in calls))
        self.assertFalse(
            any(call.args[:2] in {("git", "commit"), ("git", "push")} for call in calls)
        )

    def test_foreign_branch_protected_changes_and_advancing_main_never_push(
        self,
    ) -> None:
        cases_settings: tuple[dict[str, Any], ...] = (
            {"pending": self.request(), "foreign": True},
            {"pending": self.request(), "extra": True},
            {"advanced": True},
        )
        for settings in cases_settings:
            calls, error = self.run_mock(**settings)
            self.assertIsInstance(error, core.VerificationError)
            self.assertFalse(any(call.args[:2] == ("git", "push") for call in calls))

    def test_unchanged_pending_retry_resumes_auto_merge_without_another_commit(
        self,
    ) -> None:
        calls, error = self.run_mock(pending=self.request(), stale=False)
        self.assertIsNone(error)
        self.assertTrue(
            any(
                call.args[:3] == ("gh", "pr", "merge") and "--auto" in call.args
                for call in calls
            )
        )
        self.assertFalse(
            any(call.args[:2] in {("git", "commit"), ("git", "push")} for call in calls)
        )

        self.assertTrue(self.audit_bodies[0].startswith("Previous authorized request"))
        self.assertIn("Withdraw " + SOURCE + " 0.1.3.0 by @owner", self.audit_bodies[0])
        self.assertIn(RUN, self.audit_bodies[0])

    def test_failed_refresh_disables_auto_merge_before_writing_or_editing_audit(
        self,
    ) -> None:
        pr: dict[str, Any] = {
            "number": 7,
            "body": "Previous authorized request",
            "url": "fixture",
        }
        branch = maintenance.BranchState(pr, "b" * 40)
        request = maintenance.Request(
            "Withdraw", SOURCE, "0.1.3.0", "Owner decision", "owner", RUN
        )
        proposal = maintenance.Proposal(
            maintenance.Outcome.UPDATED, self.request(), "New request"
        )
        with (
            patch.object(maintenance, "command") as command,
            patch.object(
                maintenance, "write_proposal", side_effect=OSError("push failed")
            ),
            patch.object(maintenance, "update_body") as update,
            self.assertRaises(OSError),
        ):
            maintenance.publish_proposal(
                Path("/tmp"), "a" * 40, branch, request, proposal
            )
        self.assertEqual(len(command.call_args_list), 1)
        self.assertIn("--disable-auto", command.call_args.args)
        update.assert_not_called()

    def test_publication_and_maintenance_share_a_preserving_workflow_queue(
        self,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        workflows = root / ".github/workflows"
        for name in ("maintenance.yml", "sync.yml"):
            text = (workflows / name).read_text()
            concurrency = text.split("\nconcurrency:\n", 1)[1].split("\njobs:", 1)[0]
            values = dict(
                line.strip().split(": ", 1)
                for line in concurrency.splitlines()
                if line.strip()
            )
            self.assertEqual(
                values,
                {
                    "group": "catalog-update",
                    "cancel-in-progress": "false",
                    "queue": "max",
                },
            )

    def test_completed_app_branch_can_be_reused_but_other_apps_cannot(self) -> None:
        calls, error = self.run_mock(terminal=True)
        self.assertIsNone(error)
        self.assertTrue(
            any(
                call.args[:2] == ("git", "push")
                and call.args[2].endswith(":" + "b" * 40)
                for call in calls
            )
        )
        calls, error = self.run_mock(terminal=True, foreign=True)
        self.assertIsInstance(error, core.VerificationError)
        self.assertFalse(any(call.args[:2] == ("git", "push") for call in calls))

    def test_validate_entrypoint_uses_protected_delta_instead_of_unrelated_network(
        self,
    ) -> None:
        candidate = self.request()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in candidate.items():
                (root / name).write_text(json.dumps(value))

            def load(path: Path, ref: str | None = None) -> dict[str, Any] | None:
                return self.current if ref else candidate

            with (
                patch.dict(os.environ, {"CATALOG_BASE": "a" * 40}),
                patch.object(
                    sys, "argv", ["catalog_sync.py", "validate", "--root", directory]
                ),
                patch.object(
                    sync, "command", return_value="manifest.json\nwithdrawals.json"
                ),
                patch.object(sync, "load", side_effect=load),
                patch.object(
                    sync,
                    "verify_history",
                    side_effect=OSError("unrelated metadata unavailable"),
                ) as verify,
            ):
                sync.main()
                verify.assert_not_called()

    def test_non_main_dispatch_is_rejected_before_git_or_api(self) -> None:
        with (
            patch.dict(os.environ, dict(ENV, GITHUB_REF="refs/heads/other")),
            patch.object(maintenance, "command") as command,
            self.assertRaises(core.VerificationError),
        ):
            maintenance.maintain(Path("/tmp"), "Withdraw", SOURCE, "0.1.3.0", "reason")
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
