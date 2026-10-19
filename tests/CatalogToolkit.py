"""Shared-toolkit policy regressions preserve existing release verification."""

import base64
import hashlib
import io
import json
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_core as core


class ToolkitPolicyTests(unittest.TestCase):
    def test_administrator_only_plugins_do_not_require_web_archive(self):
        names = core.required_assets({"prefix": "example", "web": False}, "1.0.0")
        self.assertEqual(
            {
                "example-1.0.0.zip",
                "example-source-1.0.0.zip",
                "manifest.json",
                "SHA256SUMS",
            },
            names,
        )
        self.assertIn(
            "example-web-1.0.0.zip",
            core.required_assets({"prefix": "example"}, "1.0.0"),
        )

    def test_toolkit_receipt_cannot_select_an_unapproved_repository(self):
        with self.assertRaisesRegex(core.VerificationError, "approved pinned toolkit"):
            core.GitHub().toolkit_revision(
                "fixture/plugin",
                {"toolkit_repository": "owner/toolkit"},
                {"toolkit": {"repository": "other/toolkit"}, "commit": "a" * 40},
            )

    def test_toolkit_lock_and_public_archive_must_match(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as package:
            data = json.dumps(
                {"schema": 1, "version": "1.0.0", "revision": "b" * 40}
            ).encode()
            member = tarfile.TarInfo("toolkit-release.json")
            member.size = len(data)
            package.addfile(member, io.BytesIO(data))
        archive = output.getvalue()
        lock = {
            "schema": 1,
            "repository": "owner/toolkit",
            "version": "v1.0.0",
            "sha256": hashlib.sha256(archive).hexdigest(),
        }

        class Fake(core.GitHub):
            def json(self, repository, endpoint):
                if endpoint.startswith("/contents/"):
                    return {
                        "content": base64.b64encode(json.dumps(lock).encode()).decode()
                    }
                return {
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "jellyfin-plugin-toolkit-1.0.0.tar.gz",
                            "browser_download_url": "https://github.com/owner/toolkit/releases/download/v1.0.0/jellyfin-plugin-toolkit-1.0.0.tar.gz",
                        }
                    ],
                }

            def download(self, url, destination, limit):
                destination.write_bytes(archive)

            def tag(self, repository, tag):
                return "b" * 40

        receipt = {"toolkit": dict(lock), "commit": "a" * 40}
        policy = {"toolkit_repository": "owner/toolkit"}
        self.assertEqual(
            "b" * 40, Fake().toolkit_revision("fixture/plugin", policy, receipt)
        )

        class MovedTag(Fake):
            def tag(self, repository, tag):
                return "c" * 40

        with self.assertRaisesRegex(core.VerificationError, "published tag"):
            MovedTag().toolkit_revision("fixture/plugin", policy, receipt)
        receipt["toolkit"]["sha256"] = "c" * 64
        with self.assertRaisesRegex(core.VerificationError, "tagged plugin source"):
            Fake().toolkit_revision("fixture/plugin", policy, receipt)
