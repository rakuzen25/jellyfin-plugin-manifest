"""Publication lifecycle checks with real Git and a disposable bare remote."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_core as core
import catalog_maintenance_policy as policy
import catalog_sync as sync
from CatalogPublication import POLICY, SOURCE, FakeGitHub


class PublicationGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="catalog-git-")
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.remote = directory / "remote.git"
        self.root = directory / "checkout"
        self.root.mkdir()
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Catalog fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(directory / "no-hooks"))
        self.git("remote", "add", "origin", str(self.remote))
        initial = {
            "plugins.json": {SOURCE: POLICY},
            "history.json": {},
            "manifest.json": [],
            "withdrawals.json": [],
        }
        self.write(initial)
        (self.root / "README.md").write_text("Fixture policy\n")
        self.commit("Initial protected catalog")
        self.git("push", "-u", "origin", "main")
        self.github = FakeGitHub()
        self.prs = []
        self.terminal = []
        self.calls = []
        self.fail_create = False
        self.fail_auto = False
        self.created_override = {}
        self.before_command = None
        self.environment = patch.dict(
            sync.os.environ, {"PLUGIN_APP_SLUG": "test-catalog-app"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.commands = patch.object(sync, "command", side_effect=self.command)
        self.commands.start()
        self.addCleanup(self.commands.stop)
        sleeps = patch.object(sync.time, "sleep")
        sleeps.start()
        self.addCleanup(sleeps.stop)

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            check=True,
            capture_output=True,
        ).stdout.strip()

    def write(self, values):
        for name, value in values.items():
            (self.root / name).write_text(json.dumps(value, indent=4) + "\n")

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def head(self):
        remote = self.git("ls-remote", "--heads", "origin", sync.BRANCH)
        return remote.split()[0] if remote else ""

    def pr(self, head=None, **overrides):
        result = {
            "number": 7,
            "author": {"login": "app/test-catalog-app", "is_bot": True},
            "baseRefName": "main",
            "isCrossRepository": False,
            "state": "OPEN",
            "headRefOid": self.head() if head is None else head,
        }
        result.update(overrides)
        return result

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if self.before_command:
            self.before_command(args)
        if args[0] == "git":
            result = self.git(*args[1:])
            if args[1] == "push":
                for pr in self.prs:
                    pr["headRefOid"] = self.head()
            return result
        if args[:3] == ("gh", "api", "users/test-catalog-app[bot]"):
            return "123456"
        if args[:3] == ("gh", "pr", "list"):
            rows = self.prs + self.terminal if "all" in args else self.prs
            return json.dumps(rows)
        if args[:3] == ("gh", "pr", "create"):
            if self.fail_create:
                raise subprocess.CalledProcessError(1, args)
            self.prs = [self.pr(**self.created_override)]
            return "https://example.invalid/pull/7"
        if args[:3] == ("gh", "pr", "merge"):
            if "--auto" in args:
                expected = args[args.index("--match-head-commit") + 1]
                if self.head() != expected:
                    raise subprocess.CalledProcessError(1, args)
            if self.fail_auto and "--auto" in args:
                raise subprocess.CalledProcessError(1, args)
            return ""
        raise AssertionError(args)

    def snapshot(self):
        self.git("fetch", "origin", "main")
        base = self.git("rev-parse", "origin/main")
        state = policy.load(self.root, base)
        history = core.reconcile(
            self.github,
            state["plugins.json"],
            state["history.json"],
            state["withdrawals.json"],
        )
        candidate = {
            "history.json": history,
            "manifest.json": core.render(
                history, state["withdrawals.json"], state["plugins.json"]
            ),
        }
        return base, {name: state[name] for name in sync.FILES}, candidate

    def publish(self, snapshot=None):
        sync.publish_pr(self.root, *(snapshot or self.snapshot()))

    def push_orphan(self, base, candidate, alter=None):
        self.git("checkout", "-B", sync.BRANCH, base)
        self.write(candidate)
        if alter:
            alter()
        head = self.commit("Interrupted publication")
        self.git("push", "--force", "origin", sync.BRANCH)
        return head

    def advance_main(self, base, candidate=None):
        self.git("checkout", "-B", "main", base)
        if candidate:
            self.write(candidate)
        else:
            (self.root / "README.md").write_text("Newer main policy\n")
        head = self.commit("Advance protected main")
        self.git("push", "origin", "main")
        return head

    def pushes(self):
        return [args for args in self.calls if args[:2] == ("git", "push")]

    def test_initial_publication_is_a_protected_pr_with_an_empty_lease(self):
        base, before, candidate = self.snapshot()
        self.publish((base, before, candidate))
        self.assertEqual(
            self.git("show", "-s", "--format=%an <%ae>%n%cn <%ce>", self.head()),
            "test-catalog-app[bot] <123456+test-catalog-app[bot]@users.noreply.github.com>\n"
            "test-catalog-app[bot] <123456+test-catalog-app[bot]@users.noreply.github.com>",
        )
        self.assertEqual(self.git("rev-parse", "origin/main"), base)
        self.assertEqual(
            policy.load(self.root, self.head())["history.json"],
            candidate["history.json"],
        )
        self.assertEqual(len(self.prs), 1)
        self.assertEqual(
            self.pushes()[0][2], "--force-with-lease=refs/heads/" + sync.BRANCH + ":"
        )
        self.assertIn("--auto", self.calls[-1])
        self.assertFalse(any("--disable-auto" in args for args in self.calls))

    def test_retained_merged_and_closed_app_branches_are_reused_with_exact_leases(self):
        self.publish()
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state=state):
                previous = self.head()
                old_pr = self.prs.pop()
                old_pr["state"] = state
                self.terminal.append(old_pr)
                if state == "MERGED":
                    base, _, candidate = self.snapshot()
                    self.advance_main(base, candidate)
                self.github.add(
                    "0.1.4" if state == "MERGED" else "0.1.5",
                    "2026-10-15T00:00:00Z"
                    if state == "MERGED"
                    else "2026-10-16T00:00:00Z",
                    2 if state == "MERGED" else 3,
                )
                self.publish()
                self.assertNotEqual(self.head(), previous)
                self.assertEqual(
                    self.pushes()[-1][2],
                    "--force-with-lease=refs/heads/" + sync.BRANCH + ":" + previous,
                )
                self.assertEqual(len(self.prs), 1)

    def test_failed_pr_creation_recovers_the_exact_branch_without_another_push(self):
        self.fail_create = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        orphan = self.head()
        self.assertTrue(orphan)
        self.assertEqual(self.prs, [])
        self.fail_create = False
        self.publish()
        self.assertEqual(self.head(), orphan)
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.prs[0]["headRefOid"], orphan)

    def test_orphan_mismatches_and_non_catalog_changes_are_never_adopted(self):
        snapshot = self.snapshot()
        base, _, candidate = snapshot
        for kind in (
            "content",
            "extra",
            "symlink",
            "executable",
            "missing",
            "malformed",
        ):
            with self.subTest(kind=kind):

                def alter(kind=kind):
                    manifest = self.root / "manifest.json"
                    if kind == "content":
                        self.write({"manifest.json": []})
                    elif kind == "extra":
                        (self.root / "README.md").write_text("Unapproved change\n")
                    elif kind == "symlink":
                        manifest.unlink()
                        manifest.symlink_to("history.json")
                    elif kind == "executable":
                        manifest.chmod(0o755)
                    elif kind == "missing":
                        manifest.unlink()
                    else:
                        manifest.write_text("not JSON\n")

                orphan = self.push_orphan(base, candidate, alter)
                with self.assertRaisesRegex(
                    core.VerificationError, "Unmatched publication orphan"
                ):
                    self.publish(snapshot)
                self.assertEqual(self.head(), orphan)
                self.assertEqual(self.prs, [])
                self.assertEqual(self.pushes(), [])
                self.git("checkout", "main")

    def test_stale_orphan_ancestry_is_rejected_even_when_catalog_content_matches(self):
        base, _, candidate = self.snapshot()
        orphan = self.push_orphan(base, candidate)
        self.advance_main(base)
        with self.assertRaisesRegex(
            core.VerificationError, "Unmatched publication orphan"
        ):
            self.publish()
        self.assertEqual(self.head(), orphan)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.prs, [])

    def test_invalid_open_pr_metadata_and_multiple_prs_stop_before_pushing(self):
        snapshot = self.snapshot()
        base, _, candidate = snapshot
        head = self.push_orphan(base, candidate)
        invalid = [
            [self.pr(author={"login": "other-app[bot]"})],
            [self.pr(isCrossRepository=True)],
            [self.pr(baseRefName="other")],
            [self.pr(), self.pr(number=8)],
            [self.pr(head="0" * 40)],
        ]
        for prs in invalid:
            with self.subTest(prs=prs):
                self.prs = prs
                with self.assertRaises(core.VerificationError):
                    self.publish(snapshot)
                self.assertEqual(self.head(), head)
                self.assertEqual(self.pushes(), [])
                self.assertFalse(any("--auto" in args for args in self.calls))

    def test_publication_waits_for_pr_head_after_push(self):
        self.publish()
        previous = self.head()
        self.github.add("0.1.4", "2026-10-15T00:00:00Z", 2)
        reads = 0
        original = sync.publication_prs

        def delayed_prs(state="open"):
            nonlocal reads
            prs = original(state)
            if self.head() != previous:
                reads += 1
                if reads <= 2:
                    return [dict(pr, headRefOid=previous) for pr in prs]
            return prs

        with patch.object(sync, "publication_prs", side_effect=delayed_prs):
            self.publish()
        self.assertEqual(reads, 3)
        self.assertIn("--auto", self.calls[-1])
        self.assertEqual(self.calls[-1][-1], self.head())
        self.assertEqual(len(self.pushes()), 2)

    def test_created_pr_is_rechecked_before_auto_merge(self):
        for overrides in (
            {"author": {"login": "other-app[bot]"}},
            {"headRefOid": "0" * 40},
        ):
            with self.subTest(overrides=overrides):
                self.created_override = overrides
                self.prs = []
                self.calls.clear()
                with self.assertRaises(core.VerificationError):
                    self.publish()
                self.assertFalse(any("--auto" in args for args in self.calls))

    def test_terminal_foreign_pr_cannot_be_adopted_even_with_matching_content(self):
        snapshot = self.snapshot()
        base, _, candidate = snapshot
        head = self.push_orphan(base, candidate)
        for overrides in (
            {"author": {"login": "other-app[bot]"}},
            {"baseRefName": "other"},
            {"isCrossRepository": True},
        ):
            with self.subTest(overrides=overrides):
                self.terminal = [self.pr(state="CLOSED", **overrides)]
                with self.assertRaisesRegex(
                    core.VerificationError, "non-App or wrong-base"
                ):
                    self.publish(snapshot)
                self.assertEqual(self.head(), head)
                self.assertEqual(self.pushes(), [])
                self.assertEqual(self.prs, [])

    def test_remote_branch_movement_is_rejected_by_the_real_git_lease(self):
        self.publish()
        original = self.head()
        self.github.add("0.1.4", "2026-10-15T00:00:00Z", 2)
        raced = None

        def race(args):
            nonlocal raced
            if args[:2] == ("git", "push"):
                self.before_command = None
                tree = self.git("rev-parse", original + "^{tree}")
                raced = self.git(
                    "commit-tree",
                    tree,
                    "-p",
                    original,
                    "-m",
                    "Concurrent branch update",
                )
                self.git(
                    "push", "--force", "origin", raced + ":refs/heads/" + sync.BRANCH
                )

        self.before_command = race
        self.calls.clear()
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual(self.head(), raced)
        self.assertTrue(any("--disable-auto" in args for args in self.calls))
        self.assertFalse(any("--auto" in args for args in self.calls))

    def test_main_advancement_stops_both_push_and_orphan_adoption(self):
        for orphan in (False, True):
            with self.subTest(orphan=orphan):
                snapshot = self.snapshot()
                base, _, candidate = snapshot
                head = self.push_orphan(base, candidate) if orphan else ""
                advanced = False

                def race(args, base=base):
                    nonlocal advanced
                    if args[:4] == ("git", "fetch", "origin", "main") and not advanced:
                        advanced = True
                        self.before_command = None
                        self.advance_main(base)

                self.before_command = race
                with self.assertRaisesRegex(core.VerificationError, "Main advanced"):
                    self.publish(snapshot)
                self.assertEqual(self.head(), head)
                self.assertEqual(self.pushes(), [])
                self.assertEqual(self.prs, [])
                # Give the next scenario a distinct main change.
                self.git("checkout", "main")
                (self.root / "README.md").write_text("Fixture policy\n")
                self.commit("Reset fixture description")
                self.git("push", "origin", "main")

    def test_branch_movement_during_fetch_and_orphan_adoption_stops_recovery(self):
        snapshot = self.snapshot()
        base, _, candidate = snapshot
        orphan = self.push_orphan(base, candidate)
        for moment in ("fetch", "adoption"):
            with self.subTest(moment=moment):
                self.git(
                    "--git-dir=" + str(self.remote),
                    "update-ref",
                    "refs/heads/" + sync.BRANCH,
                    orphan,
                )
                observed = 0
                raced = None

                def race(args, moment=moment):
                    nonlocal observed, raced
                    if args[:2] == ("git", "ls-remote"):
                        observed += 1
                    if (
                        moment == "fetch" and args[:3] == ("git", "fetch", "origin")
                    ) or (
                        moment == "adoption"
                        and args[:2] == ("git", "ls-remote")
                        and observed == 2
                    ):
                        self.before_command = None
                        tree = self.git("rev-parse", orphan + "^{tree}")
                        raced = self.git(
                            "commit-tree", tree, "-p", orphan, "-m", "Concurrent change"
                        )
                        self.git(
                            "push",
                            "--force",
                            "origin",
                            raced + ":refs/heads/" + sync.BRANCH,
                        )

                self.before_command = race
                with self.assertRaisesRegex(core.VerificationError, "advanced"):
                    self.publish(snapshot)
                self.assertEqual(self.head(), raced)
                self.assertEqual(self.prs, [])
                self.assertEqual(self.pushes(), [])

    def test_failed_refresh_stays_disabled_and_matching_retry_resumes_without_push(
        self,
    ):
        self.publish()
        self.github.add("0.1.4", "2026-10-15T00:00:00Z", 2)
        self.fail_auto = True
        self.calls.clear()
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        refreshed = self.head()
        self.assertTrue(any("--disable-auto" in args for args in self.calls))
        self.assertEqual(len(self.pushes()), 1)
        self.fail_auto = False
        self.calls.clear()
        self.publish()
        self.assertEqual(self.head(), refreshed)
        self.assertEqual(self.pushes(), [])
        self.assertIn("--auto", self.calls[-1])

    def test_head_guard_rejects_a_change_after_final_pr_inspection(self):
        self.publish()
        original = self.head()
        raced = None

        def race(args):
            nonlocal raced
            if args[:3] == ("gh", "pr", "merge") and "--auto" in args:
                self.before_command = None
                tree = self.git("rev-parse", original + "^{tree}")
                raced = self.git(
                    "commit-tree", tree, "-p", original, "-m", "Concurrent update"
                )
                self.git(
                    "push", "--force", "origin", raced + ":refs/heads/" + sync.BRANCH
                )

        self.before_command = race
        self.calls.clear()
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual(self.head(), raced)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.calls[-1][-2:], ("--match-head-commit", original))

    def test_sync_reads_protected_snapshot_instead_of_local_catalog_edits(self):
        snapshot = self.snapshot()
        self.write({"plugins.json": {}, "history.json": {"untrusted": True}})
        with (
            patch.object(sync, "GitHub", return_value=self.github),
            patch.object(sync, "publish_pr") as publish,
            patch.object(sync, "problem_issue"),
            patch.object(
                sys, "argv", ["catalog_sync.py", "sync", "--root", str(self.root)]
            ),
        ):
            sync.main()
        self.assertEqual(publish.call_args.args, (self.root, *snapshot))

    def test_offline_withdraw_can_regenerate_a_missing_manifest(self):
        (self.root / "manifest.json").unlink()
        with patch.object(
            sys, "argv", ["catalog_sync.py", "withdraw", "--root", str(self.root)]
        ):
            sync.main()
        self.assertEqual(json.loads((self.root / "manifest.json").read_text()), [])
        self.assertEqual(self.calls, [])

    def test_offline_withdraw_rejects_aliases_without_overwriting_the_manifest(self):
        _, _, candidate = self.snapshot()
        self.write(candidate)
        self.write(
            {
                "withdrawals.json": [
                    {
                        "guid": POLICY["guid"],
                        "version": "00.1.3.0",
                        "reason": "Owner withdrawal",
                    }
                ]
            }
        )
        before = (self.root / "manifest.json").read_bytes()
        with (
            patch.object(
                sys, "argv", ["catalog_sync.py", "withdraw", "--root", str(self.root)]
            ),
            self.assertRaisesRegex(core.VerificationError, "without leading zeros"),
        ):
            sync.main()
        self.assertEqual((self.root / "manifest.json").read_bytes(), before)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
