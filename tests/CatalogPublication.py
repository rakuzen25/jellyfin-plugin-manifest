"""Catalog trust, integrity, recovery and withdrawal regressions; no remote writes."""

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_sync as sync

SOURCE = "rakuzen25/jellyfin-plugin-lyricbridge"
GUID = "70a9d327-7e98-4201-88a5-724d39366c40"
GROUPS = [
    "metadata",
    "frontend",
    "backend",
    "tooling",
    "workflow",
    "rendering",
    "performance",
    "artifacts",
]
POLICY = {
    "guid": GUID,
    "prefix": "lyricbridge",
    "receipt_schema": 3,
    "groups": GROUPS,
    "validation_workflows": [".github/workflows/release.yml"],
    "attestation_workflow": ".github/workflows/release.yml",
}


class FakeGitHub(sync.GitHub):
    def __init__(self):
        self.commit = "a" * 40
        self.releases = []
        self.files = {}
        self.receipts = {}
        self.run = {
            "id": 42,
            "head_repository": {"full_name": SOURCE},
            "head_sha": self.commit,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/release.yml",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
        self.jobs = [{"name": "validate / validate", "conclusion": "success"}]
        self.add("0.1.3", "2026-10-14T00:00:00Z", 1)

    def add(self, version, timestamp, release_id):
        tag = "v" + version
        base = f"https://github.com/{SOURCE}/releases/download/{tag}/"
        binary = f"lyricbridge-{version}.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("LyricBridge.dll", "fixture")
        files = {
            binary: buffer.getvalue(),
            f"lyricbridge-web-{version}.zip": b"web",
            f"lyricbridge-source-{version}.zip": b"source",
        }
        entry = {
            "guid": GUID,
            "name": "LyricBridge " + version,
            "description": "Current metadata",
            "versions": [
                {
                    "version": version + ".0",
                    "targetAbi": "12.2.0.0",
                    "timestamp": timestamp,
                    "sourceUrl": base + binary,
                    "checksum": hashlib.md5(files[binary]).hexdigest(),
                }
            ],
        }
        files["manifest.json"] = json.dumps([entry]).encode()
        files["SHA256SUMS"] = "".join(
            hashlib.sha256(data).hexdigest() + "  " + name + "\n"
            for name, data in sorted(files.items())
        ).encode()
        self.receipts[tag] = {
            "schema": 3,
            "repository": SOURCE,
            "run_id": "42",
            "run_attempt": 1,
            "commit": self.commit,
            "run_head_sha": self.commit,
            "release": True,
            "groups": dict.fromkeys(GROUPS, "success"),
            "artifacts": {
                name: hashlib.sha256(data).hexdigest() for name, data in files.items()
            },
        }
        self.files.update({base + name: data for name, data in files.items()})
        release = {
            "id": release_id,
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "published_at": timestamp,
            "assets": [
                {"name": name, "browser_download_url": base + name} for name in files
            ],
        }
        self.releases.append(release)
        return release

    def json(self, repository, endpoint):
        if endpoint.endswith("/jobs?per_page=100"):
            return {"jobs": self.jobs}
        if endpoint == "/actions/runs/42":
            return self.run
        if endpoint.startswith("/releases/"):
            return next(
                release
                for release in self.releases
                if release["id"] == int(endpoint.split("/")[-1])
            )
        raise AssertionError(endpoint)

    def pages(self, repository, endpoint, limit=10):
        return iter(self.releases)

    def tag(self, repository, tag):
        return self.commit

    def download(self, url, destination, maximum=0, authenticated=False):
        destination.write_bytes(self.files[url])

    def receipt(self, repository, policy, release, directory, commit, bootstrap=None):
        return self.receipts[release["tag_name"]]


class CatalogPublicationTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.plugins = {SOURCE: POLICY}

    def history(self):
        return sync.reconcile(self.github, self.plugins, {}, [])

    def test_initial_latest_only_then_missed_updates_and_idempotent_retries(self):
        self.github.add("0.1.2", "2026-10-13T00:00:00Z", 2)
        first = self.history()
        self.assertEqual(list(first), [GUID + "/0.1.3.0"])
        self.github.add("0.1.4", "2026-10-15T00:00:00Z", 3)
        self.github.add("0.1.5", "2026-10-16T00:00:00Z", 4)
        recovered = sync.reconcile(self.github, self.plugins, first, [])
        self.assertEqual(len(recovered), 3)
        self.assertEqual(
            sync.reconcile(self.github, self.plugins, recovered, []), recovered
        )
        manifest = sync.render(recovered, [], self.plugins)
        self.assertEqual(manifest[0]["name"], "LyricBridge 0.1.5")
        self.assertEqual(
            [item["version"] for item in manifest[0]["versions"]],
            ["0.1.5.0", "0.1.4.0", "0.1.3.0"],
        )

    def test_drafts_and_prereleases_never_enter_catalog(self):
        for flag in ("draft", "prerelease"):
            self.github.releases[0][flag] = True
            self.assertEqual(self.history(), {})
            with self.assertRaises(sync.VerificationError):
                sync.verify_release(
                    self.github, SOURCE, POLICY, self.github.releases[0]
                )
            self.github.releases[0][flag] = False

    def test_wrong_guid_and_repository_or_failed_evidence_are_rejected(self):
        receipt = self.github.receipts["v0.1.3"]
        for field, bad in (
            ("repository", "other/repo"),
            ("commit", "b" * 40),
            ("schema", 2),
            ("release", False),
            ("run_id", "../bad"),
        ):
            original = receipt[field]
            receipt[field] = bad
            with self.subTest(field=field), self.assertRaises(sync.VerificationError):
                self.history()
            receipt[field] = original
        for group in GROUPS:
            receipt["groups"][group] = "failure"
            with self.subTest(group=group), self.assertRaises(sync.VerificationError):
                self.history()
            receipt["groups"][group] = "success"
        policy = dict(POLICY, guid="b" * 36)
        with self.assertRaises(sync.VerificationError):
            sync.verify_release(self.github, SOURCE, policy, self.github.releases[0])

    def test_fork_pr_unapproved_workflow_and_missing_gate_fail(self):
        for field, bad in (
            ("event", "pull_request"),
            ("path", ".github/workflows/untrusted.yml"),
            ("head_repository", {"full_name": "fork/repo"}),
            ("head_sha", "b" * 40),
            ("status", "in_progress"),
        ):
            original = self.github.run[field]
            self.github.run[field] = bad
            with self.subTest(field=field), self.assertRaises(sync.VerificationError):
                self.history()
            self.github.run[field] = original
        self.github.jobs = []
        with self.assertRaises(sync.VerificationError):
            self.history()

    def test_corrupt_or_unavailable_assets_and_moved_tags_preserve_input(self):
        history = self.history()
        before = copy.deepcopy(history)
        url = self.github.releases[0]["assets"][0]["browser_download_url"]
        self.github.files[url] = b"changed"
        with self.assertRaises(sync.VerificationError):
            sync.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(history, before)
        self.github = FakeGitHub()
        self.github.commit = "b" * 40
        with self.assertRaises(sync.VerificationError):
            sync.reconcile(self.github, self.plugins, history, [])
        self.github = FakeGitHub()
        self.github.releases[0]["assets"].pop()
        with self.assertRaises(sync.VerificationError):
            sync.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(history, before)

    def test_explicit_withdrawal_survives_retries_and_release_removal(self):
        history = self.history()
        withdrawn = [{"guid": GUID, "version": "0.1.3.0", "reason": "Owner withdrawal"}]
        self.github.releases = []
        with self.assertRaises(sync.VerificationError):
            sync.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(
            sync.reconcile(self.github, self.plugins, history, withdrawn), history
        )
        self.assertEqual(sync.render(history, withdrawn, self.plugins), [])
        with self.assertRaises(sync.VerificationError):
            sync.withdrawal_keys([dict(withdrawn[0], reason="")], self.plugins)

    def test_network_failure_keeps_history_unchanged(self):
        history = self.history()
        with (
            patch.object(self.github, "pages", side_effect=OSError("rate limit")),
            self.assertRaises(OSError),
        ):
            sync.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(len(history), 1)

    def test_public_receipt_requires_attestation_and_bootstrap_cannot_use_expired_artifacts(
        self,
    ):
        github = sync.GitHub()
        release = copy.deepcopy(self.github.releases[0])
        release["assets"].append(
            {
                "name": "validation.json",
                "browser_download_url": f"https://github.com/{SOURCE}/releases/download/v0.1.3/validation.json",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def download(url, destination, maximum=0, authenticated=False):
                destination.write_text(json.dumps(self.github.receipts["v0.1.3"]))

            with (
                patch.object(github, "download", side_effect=download),
                patch.object(github, "attest") as attest,
            ):
                receipt = github.receipt(
                    SOURCE, POLICY, release, directory, self.github.commit
                )
                self.assertEqual(receipt["commit"], self.github.commit)
                attest.assert_called_once()
            with (
                patch.object(github, "download", side_effect=download),
                patch.object(
                    github,
                    "attest",
                    side_effect=sync.VerificationError("invalid signature"),
                ),
                self.assertRaises(sync.VerificationError),
            ):
                github.receipt(SOURCE, POLICY, release, directory, self.github.commit)
            release["assets"].pop()
            with (
                patch.object(github, "pages", return_value=iter([self.github.run])),
                patch.object(
                    github,
                    "json",
                    return_value={
                        "artifacts": [
                            {"name": "lyricbridge-validation-receipt", "expired": True}
                        ]
                    },
                ),
                patch.object(github, "download") as download_mock,
            ):
                with self.assertRaisesRegex(sync.VerificationError, "waiting"):
                    github.receipt(
                        SOURCE, POLICY, release, directory, self.github.commit
                    )
                download_mock.assert_not_called()

    def test_attestation_requires_matching_successful_attach_attempt(self):
        invocation = f"https://github.com/{SOURCE}/actions/runs/42/attempts/1"
        output = [
            {
                "verificationResult": {
                    "statement": {
                        "predicate": {
                            "runDetails": {"metadata": {"invocationId": invocation}}
                        }
                    }
                }
            }
        ]
        github = sync.GitHub()
        with (
            patch.object(
                sync.subprocess, "check_output", return_value=json.dumps(output)
            ),
            patch.object(
                github,
                "json",
                side_effect=[
                    self.github.run,
                    {"jobs": [{"name": "attach", "conclusion": "success"}]},
                ],
            ),
        ):
            github.attest(SOURCE, POLICY, Path("validation.json"))
        with (
            patch.object(
                sync.subprocess, "check_output", return_value=json.dumps(output)
            ),
            patch.object(
                github,
                "json",
                side_effect=[
                    self.github.run,
                    {"jobs": [{"name": "attach", "conclusion": "failure"}]},
                ],
            ),
            self.assertRaises(sync.VerificationError),
        ):
            github.attest(SOURCE, POLICY, Path("validation.json"))

    def test_bootstrap_archive_is_bound_to_github_digest_and_run_even_after_expiry(
        self,
    ):
        import base64

        github = sync.GitHub()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr(
                "validation.json", json.dumps(self.github.receipts["v0.1.3"])
            )
        data = buffer.getvalue()
        proof = {"artifact_id": 123, "archive": base64.b64encode(data).decode("ascii")}
        metadata = {
            "name": "lyricbridge-validation-receipt",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "workflow_run": {"id": 42},
            "expired": True,
        }
        with patch.object(github, "json", return_value=metadata):
            self.assertEqual(github.bootstrap_receipt(SOURCE, proof)["run_id"], "42")
        for broken in (
            dict(metadata, digest="sha256:" + "0" * 64),
            dict(metadata, workflow_run={"id": 43}),
            dict(metadata, name="unverified"),
        ):
            with (
                patch.object(github, "json", return_value=broken),
                self.assertRaises(sync.VerificationError),
            ):
                github.bootstrap_receipt(SOURCE, proof)

    def test_pr_updates_are_idempotent_app_owned_and_cannot_auto_merge_policy_changes(
        self,
    ):
        root = Path("/tmp/catalog-test")
        with patch.object(sync, "command") as command:
            sync.publish_pr(root, {}, {})
            command.assert_not_called()

        def fake(*args, **kwargs):
            if args[:3] == ("gh", "pr", "list"):
                return json.dumps([{"number": 7, "author": {"login": "test-app[bot]"}}])
            if args[:2] == ("git", "ls-remote"):
                return "existing"
            if args[:2] == ("git", "diff"):
                return "manifest.json\nhistory.json"
            return ""

        with (
            patch.dict(sync.os.environ, {"PLUGIN_APP_SLUG": "test-app"}),
            patch.object(sync, "command", side_effect=fake) as command,
        ):
            sync.publish_pr(root, {}, {"new": True})
            calls = [call.args for call in command.call_args_list]
            self.assertEqual(
                sum(args[:3] == ("gh", "pr", "merge") for args in calls), 1
            )
            self.assertFalse(any(args[:3] == ("gh", "pr", "create") for args in calls))
            self.assertTrue(
                any(args[:3] == ("git", "push", "--force-with-lease") for args in calls)
            )

        def changed_policy(*args, **kwargs):
            return (
                "plugins.json" if args[:2] == ("git", "diff") else fake(*args, **kwargs)
            )

        with (
            patch.dict(sync.os.environ, {"PLUGIN_APP_SLUG": "test-app"}),
            patch.object(sync, "command", side_effect=changed_policy) as command,
            self.assertRaises(sync.VerificationError),
        ):
            sync.publish_pr(root, {}, {"new": True})
        self.assertFalse(
            any(call.args[:2] == ("git", "push") for call in command.call_args_list)
        )
        with (
            patch.dict(sync.os.environ, {"PLUGIN_APP_SLUG": "test-app"}),
            patch.object(
                sync,
                "command",
                return_value=json.dumps(
                    [{"number": 7, "author": {"login": "other-bot[bot]"}}]
                ),
            ) as command,
            self.assertRaises(sync.VerificationError),
        ):
            sync.publish_pr(root, {}, {"new": True})
        self.assertEqual(command.call_count, 1)

    def test_issue_retry_does_not_create_duplicates_or_add_comments(self):
        import os

        marker = (
            "<!-- catalog-integrity:"
            + hashlib.sha256(b"missing asset").hexdigest()[:16]
            + " -->"
        )
        issue = {
            "number": 1,
            "author": {"login": "test-app[bot]"},
            "body": marker + "\nmissing asset",
        }
        with (
            patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-app"}),
            patch.object(sync, "command", return_value=json.dumps([issue])) as command,
        ):
            sync.problem_issue("missing asset")
            self.assertEqual(command.call_count, 1)
        with (
            patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-app"}),
            patch.object(
                sync, "command", side_effect=[json.dumps([issue]), ""]
            ) as command,
        ):
            sync.problem_issue(None)
            self.assertEqual(command.call_args.args[:3], ("gh", "issue", "close"))

    def test_empty_catalog_is_valid_while_waiting_for_first_verified_release(self):
        self.assertEqual(sync.verify_history(self.github, self.plugins, {}, []), {})
        self.assertEqual(sync.render({}, [], self.plugins), [])


if __name__ == "__main__":
    unittest.main()
