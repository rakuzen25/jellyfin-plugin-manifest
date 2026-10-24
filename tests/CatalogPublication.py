"""Catalog trust, integrity, recovery and withdrawal regressions; no remote writes."""

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_core as core
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
POLICY: dict[str, Any] = {
    "guid": GUID,
    "prefix": "lyricbridge",
    "web": True,
    "toolkit_repository": "rakuzen25/jellyfin-plugin-toolkit",
    "toolkit_attestation_workflow": ".github/workflows/release.yml",
    "groups": GROUPS,
    "validation_workflows": [".github/workflows/release.yml"],
}


class FakeGitHub(core.GitHub):
    def __init__(self) -> None:
        self.commit = "a" * 40
        self.releases: list[dict[str, Any]] = []
        self.files: dict[str, Any] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.run: dict[str, Any] = {
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

    def add(self, version: str, timestamp: str, release_id: int) -> dict[str, Any]:
        tag = "v" + version
        base = f"https://github.com/{SOURCE}/releases/download/{tag}/"
        binary = f"lyricbridge-{version}.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            assembly = zipfile.ZipInfo(
                "LyricBridge.dll", date_time=(2026, 10, 14, 0, 0, 0)
            )
            zipped.writestr(assembly, "fixture")
        files = {
            binary: buffer.getvalue(),
            f"lyricbridge-web-{version}.zip": b"web",
            f"lyricbridge-source-{version}.zip": b"source",
        }
        entry: dict[str, Any] = {
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
            "schema": 7,
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
        release: dict[str, Any] = {
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

    def json(self, repository: str, endpoint: str) -> Any:
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

    def pages(
        self, repository: str, endpoint: str, limit: int = 10
    ) -> Iterator[dict[str, Any]]:
        return iter(self.releases)

    def tag(self, repository: str, tag: str) -> str:
        return self.commit

    def download(
        self, url: str, destination: Path, maximum: int = 0, authenticated: bool = False
    ) -> None:
        destination.write_bytes(self.files[url])

    def receipt(
        self,
        repository: str,
        policy: dict[str, Any],
        release: dict[str, Any],
        directory: Path,
        commit: str,
    ) -> dict[str, Any]:
        return self.receipts[release["tag_name"]]


class CatalogPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.plugins = {SOURCE: POLICY}

    def history(self) -> dict[str, Any]:
        return core.reconcile(self.github, self.plugins, {}, [])

    def test_release_fixture_bytes_do_not_depend_on_the_clock(self) -> None:
        with patch(
            "zipfile.time.localtime", return_value=(2026, 1, 1, 0, 0, 0, 3, 1, 0)
        ):
            first = FakeGitHub()
        with patch(
            "zipfile.time.localtime", return_value=(2026, 2, 1, 0, 0, 0, 6, 32, 0)
        ):
            second = FakeGitHub()
        self.assertEqual(first.files, second.files)
        self.assertEqual(first.receipts, second.receipts)

    def test_initial_latest_only_then_missed_updates_and_idempotent_retries(
        self,
    ) -> None:
        self.github.add("0.1.2", "2026-10-13T00:00:00Z", 2)
        first = self.history()
        self.assertEqual(list(first), [GUID + "/0.1.3.0"])
        self.github.add("0.1.4", "2026-10-15T00:00:00Z", 3)
        self.github.add("0.1.5", "2026-10-16T00:00:00Z", 4)
        recovered = core.reconcile(self.github, self.plugins, first, [])
        self.assertEqual(len(recovered), 3)
        self.assertEqual(
            core.reconcile(self.github, self.plugins, recovered, []), recovered
        )
        manifest = core.render(recovered, [], self.plugins)
        self.assertEqual(manifest[0]["name"], "LyricBridge 0.1.5")
        self.assertEqual(
            [item["version"] for item in manifest[0]["versions"]],
            ["0.1.5.0", "0.1.4.0", "0.1.3.0"],
        )

    def test_drafts_and_prereleases_never_enter_catalog(self) -> None:
        for flag in ("draft", "prerelease"):
            self.github.releases[0][flag] = True
            self.assertEqual(self.history(), {})
            with self.assertRaises(core.VerificationError):
                core.verify_release(
                    self.github, SOURCE, POLICY, self.github.releases[0]
                )
            self.github.releases[0][flag] = False

    def test_wrong_guid_and_repository_or_failed_evidence_are_rejected(self) -> None:
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
            with self.subTest(field=field), self.assertRaises(core.VerificationError):
                self.history()
            receipt[field] = original
        for group in GROUPS:
            receipt["groups"][group] = "failure"
            with self.subTest(group=group), self.assertRaises(core.VerificationError):
                self.history()
            receipt["groups"][group] = "success"
        policy = dict(POLICY, guid="b" * 36)
        with self.assertRaises(core.VerificationError):
            core.verify_release(self.github, SOURCE, policy, self.github.releases[0])

    def test_fork_pr_unapproved_workflow_and_missing_gate_fail(self) -> None:
        for field, bad in (
            ("event", "pull_request"),
            ("path", ".github/workflows/untrusted.yml"),
            ("head_repository", {"full_name": "fork/repo"}),
            ("head_sha", "b" * 40),
            ("status", "in_progress"),
        ):
            original = self.github.run[field]
            self.github.run[field] = bad
            with self.subTest(field=field), self.assertRaises(core.VerificationError):
                self.history()
            self.github.run[field] = original
        self.github.jobs = []
        with self.assertRaises(core.VerificationError):
            self.history()

    def test_corrupt_or_unavailable_assets_and_moved_tags_preserve_input(self) -> None:
        history = self.history()
        before = copy.deepcopy(history)
        url = self.github.releases[0]["assets"][0]["browser_download_url"]
        self.github.files[url] = b"changed"
        with self.assertRaises(core.VerificationError):
            core.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(history, before)
        self.github = FakeGitHub()
        self.github.commit = "b" * 40
        with self.assertRaises(core.VerificationError):
            core.reconcile(self.github, self.plugins, history, [])
        self.github = FakeGitHub()
        self.github.releases[0]["assets"].pop()
        with self.assertRaises(core.VerificationError):
            core.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(history, before)

    def test_explicit_withdrawal_survives_retries_and_release_removal(self) -> None:
        history = self.history()
        withdrawn = [{"guid": GUID, "version": "0.1.3.0", "reason": "Owner withdrawal"}]
        self.github.releases = []
        with self.assertRaises(core.VerificationError):
            core.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(
            core.reconcile(self.github, self.plugins, history, withdrawn), history
        )
        self.assertEqual(core.render(history, withdrawn, self.plugins), [])
        with self.assertRaises(core.VerificationError):
            core.withdrawal_keys([dict(withdrawn[0], reason="")], self.plugins)

    def test_noncanonical_withdrawal_versions_fail_with_a_clear_error(self) -> None:
        for version in (
            "00.1.3.0",
            "0.01.3.0",
            "0.1.03.0",
            "0.1.3.00",
            "0.1.3",
            "0.1.3.0.0",
            "\u0660.1.3.0",
            "+0.1.3.0",
            None,
            13,
        ):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    core.VerificationError,
                    "four numeric components without leading zeros",
                ),
            ):
                core.withdrawal_keys(
                    [{"guid": GUID, "version": version, "reason": "Owner withdrawal"}],
                    self.plugins,
                )

    def test_canonical_withdrawal_versions_keep_their_exact_identity(self) -> None:
        for version in ("0.0.0.0", "0.1.3.0", "10.20.30.40"):
            with self.subTest(version=version):
                self.assertEqual(
                    core.withdrawal_keys(
                        [
                            {
                                "guid": GUID,
                                "version": version,
                                "reason": "Owner withdrawal",
                            }
                        ],
                        self.plugins,
                    ),
                    {GUID + "/" + version},
                )

    def test_network_failure_keeps_history_unchanged(self) -> None:
        history = self.history()
        with (
            patch.object(self.github, "pages", side_effect=OSError("rate limit")),
            self.assertRaises(OSError),
        ):
            core.reconcile(self.github, self.plugins, history, [])
        self.assertEqual(len(history), 1)

    def test_public_receipt_requires_attestation_and_no_artifact_fallback(
        self,
    ) -> None:
        github = core.GitHub()
        release = copy.deepcopy(self.github.releases[0])
        release["assets"].append(
            {
                "name": "validation.json",
                "browser_download_url": f"https://github.com/{SOURCE}/releases/download/v0.1.3/validation.json",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def download(
                url: str,
                destination: Path,
                maximum: int = 0,
                authenticated: bool = False,
            ) -> None:
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
                    side_effect=core.VerificationError("invalid signature"),
                ),
                self.assertRaises(core.VerificationError),
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
                with self.assertRaisesRegex(
                    core.VerificationError, "public validation.json"
                ):
                    github.receipt(
                        SOURCE, POLICY, release, directory, self.github.commit
                    )
                download_mock.assert_not_called()

    def test_attestation_requires_matching_successful_attach_attempt(self) -> None:
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
        github = core.GitHub()
        receipt_file = self.enterContext(
            tempfile.NamedTemporaryFile(mode="w", suffix=".json")
        )
        receipt_file.write(json.dumps(self.github.receipts["v0.1.3"]))
        receipt_file.flush()
        with (
            patch.object(github, "toolkit_revision", return_value="b" * 40),
            patch.dict(os.environ, {"SOURCE_GH_TOKEN": "fixture-source-token"}),
            patch.object(subprocess, "check_output", return_value=json.dumps(output)),
            patch.object(
                github,
                "json",
                side_effect=[
                    self.github.run,
                    {"jobs": [{"name": "attach", "conclusion": "success"}]},
                ],
            ),
        ):
            github.attest(SOURCE, POLICY, Path(receipt_file.name))
        with (
            patch.object(github, "toolkit_revision", return_value="b" * 40),
            patch.dict(os.environ, {"SOURCE_GH_TOKEN": "fixture-source-token"}),
            patch.object(subprocess, "check_output", return_value=json.dumps(output)),
            patch.object(
                github,
                "json",
                side_effect=[
                    self.github.run,
                    {"jobs": [{"name": "attach", "conclusion": "failure"}]},
                ],
            ),
            self.assertRaises(core.VerificationError),
        ):
            github.attest(SOURCE, POLICY, Path(receipt_file.name))

    @patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-catalog-app"})
    def test_commit_identity_rejects_missing_bot_user_id(self) -> None:
        for user_id in ("", "null", "0", "someone-else"):
            with self.subTest(user_id=user_id):
                with (
                    patch.object(core, "command", return_value=user_id) as run,
                    self.assertRaises(core.VerificationError),
                ):
                    core.configure_app_git_author(Path("/tmp"), run)
                self.assertEqual(run.call_count, 1)

    @patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-catalog-app"})
    def test_app_author_formats_preserve_ownership_checks(self) -> None:
        for author in (
            {"login": "app/test-catalog-app", "is_bot": True},
            {"login": "test-catalog-app[bot]"},
        ):
            pr: dict[str, Any] = {
                "author": author,
                "baseRefName": "main",
                "isCrossRepository": False,
            }
            with self.subTest(author=author):
                self.assertTrue(sync.owned_pr(pr))
                self.assertFalse(sync.owned_pr(dict(pr, baseRefName="other")))
                self.assertFalse(sync.owned_pr(dict(pr, isCrossRepository=True)))
        for author in (
            {},
            {"login": "test-catalog-app"},
            {"login": "app/test-catalog-app", "is_bot": False},
            {"login": "app/test-catalog-app"},
            {"login": "test-catalog-app[bot]", "is_bot": False},
            {"login": "app/other-app", "is_bot": True},
        ):
            with self.subTest(author=author):
                self.assertFalse(core.app_author(author))

    def test_noop_and_foreign_publication_pr_do_not_run_git_commands(
        self,
    ) -> None:
        root = Path("/tmp/catalog-test")
        with patch.object(sync, "command") as command:
            sync.publish_pr(root, "a" * 40, {}, {})
            command.assert_not_called()
        with (
            patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-catalog-app"}),
            patch.object(
                sync,
                "publication_prs",
                return_value=[
                    {
                        "number": 7,
                        "author": {"login": "other-bot[bot]"},
                        "baseRefName": "main",
                        "isCrossRepository": False,
                    }
                ],
            ),
            patch.object(sync, "command") as command,
            self.assertRaises(core.VerificationError),
        ):
            sync.publish_pr(root, "a" * 40, {}, {"new": True})
        command.assert_not_called()

    def test_issue_retry_does_not_create_duplicates_or_add_comments(self) -> None:
        import os

        marker = (
            "<!-- catalog-integrity:"
            + hashlib.sha256(b"missing asset").hexdigest()[:16]
            + " -->"
        )
        issue: dict[str, Any] = {
            "number": 1,
            "author": {"login": "app/test-catalog-app", "is_bot": True},
            "body": marker + "\nmissing asset",
        }
        with (
            patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-catalog-app"}),
            patch.object(sync, "command", return_value=json.dumps([issue])) as command,
        ):
            sync.problem_issue("missing asset")
            self.assertEqual(command.call_count, 1)
        with (
            patch.dict(os.environ, {"PLUGIN_APP_SLUG": "test-catalog-app"}),
            patch.object(
                sync, "command", side_effect=[json.dumps([issue]), ""]
            ) as command,
        ):
            sync.problem_issue(None)
            self.assertEqual(command.call_args.args[:3], ("gh", "issue", "close"))

    def test_empty_catalog_is_valid_while_waiting_for_first_verified_release(
        self,
    ) -> None:
        self.assertEqual(core.verify_history(self.github, self.plugins, {}, []), {})
        self.assertEqual(core.render({}, [], self.plugins), [])


if __name__ == "__main__":
    unittest.main()
