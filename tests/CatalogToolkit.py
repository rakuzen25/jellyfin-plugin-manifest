"""Current native toolkit distributions remain bound to tagged consumer evidence."""

import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_core as core
from CatalogPublication import POLICY, SOURCE, FakeGitHub

TOOLKIT = "rakuzen25/jellyfin-plugin-toolkit"
VERSION = "0.4.2"
REVISION = "b" * 40
WHEEL = f"jellyfin_plugin_toolkit-{VERSION}-py3-none-any.whl"
SDIST = f"jellyfin_plugin_toolkit-{VERSION}.tar.gz"
BASE = f"https://github.com/{TOOLKIT}/releases/download/v{VERSION}/"


class ToolkitGitHub(core.GitHub):
    def __init__(self):
        self.files = {WHEEL: b"native wheel", SDIST: b"native source"}
        hashes = {
            name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()
        }
        self.files["SHA256SUMS"] = "".join(
            value + "  " + name + "\n" for name, value in sorted(hashes.items())
        ).encode()
        self.files["build-provenance.json"] = json.dumps(
            {
                "schema": 2,
                "version": VERSION,
                "revision": REVISION,
                "files": hashes,
            }
        ).encode()
        self.lock = (
            'version = 1\n[[package]]\nname = "jellyfin-plugin-toolkit"\n'
            f'version = "{VERSION}"\nsource = {{ url = "{BASE + WHEEL}" }}\n'
            f'wheels = [{{ hash = "sha256:{hashes[WHEEL]}" }}]\n'
        ).encode()
        self.receipt = {
            "schema": 7,
            "commit": "a" * 40,
            "toolkit": {
                "distribution": "jellyfin-plugin-toolkit",
                "version": VERSION,
                "source": BASE + WHEEL,
                "sha256": hashes[WHEEL],
                "python_lock_sha256": hashlib.sha256(self.lock).hexdigest(),
            },
        }
        self.policy = {"toolkit_repository": TOOLKIT}
        self.release = {
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": name, "browser_download_url": BASE + name}
                for name in self.files
            ],
        }
        self.revision = REVISION
        self.requests = []

    def json(self, repository, endpoint):
        self.requests.append((repository, endpoint))
        if repository == SOURCE:
            if endpoint != "/contents/uv.lock?ref=" + self.receipt["commit"]:
                raise AssertionError(endpoint)
            return {"content": base64.b64encode(self.lock).decode()}
        if (repository, endpoint) != (TOOLKIT, "/releases/tags/v" + VERSION):
            raise AssertionError((repository, endpoint))
        return self.release

    def download(self, url, destination, maximum=0, authenticated=False):
        if not url.startswith(BASE):
            raise AssertionError(url)
        destination.write_bytes(self.files[url.removeprefix(BASE)])

    def tag(self, repository, tag):
        if (repository, tag) != (TOOLKIT, "v" + VERSION):
            raise AssertionError((repository, tag))
        return self.revision

    def verify(self):
        return self.toolkit_revision(SOURCE, self.policy, self.receipt)


class ToolkitPolicyTests(unittest.TestCase):
    def test_administrator_only_plugins_do_not_require_web_archive(self):
        self.assertEqual(
            {
                "example-1.0.0.zip",
                "example-source-1.0.0.zip",
                "manifest.json",
                "SHA256SUMS",
            },
            core.required_assets({"prefix": "example", "web": False}, "1.0.0"),
        )
        self.assertIn(
            "example-web-1.0.0.zip",
            core.required_assets({"prefix": "example"}, "1.0.0"),
        )

    def test_native_distribution_pair_matches_tagged_uv_lock(self):
        github = ToolkitGitHub()
        self.assertEqual(REVISION, github.verify())
        self.assertIn((SOURCE, "/contents/uv.lock?ref=" + "a" * 40), github.requests)

    def test_unapproved_and_local_wheel_sources_are_rejected(self):
        for source in (
            "https://attacker.invalid/toolkit.whl",
            ".build/toolkit-dist/" + WHEEL,
            "build-support/" + WHEEL,
        ):
            with self.subTest(source=source):
                github = ToolkitGitHub()
                github.receipt["toolkit"]["source"] = source
                with self.assertRaisesRegex(core.VerificationError, "approved release"):
                    github.verify()
                self.assertEqual([], github.requests)

    def test_missing_or_old_toolkit_identity_is_rejected(self):
        for toolkit in (
            None,
            {},
            {"repository": TOOLKIT, "schema": 1, "version": "v0.4.2"},
        ):
            with self.subTest(toolkit=toolkit):
                github = ToolkitGitHub()
                github.receipt["toolkit"] = toolkit
                with self.assertRaisesRegex(
                    core.VerificationError, "approved pinned toolkit"
                ):
                    github.verify()

    def test_receipt_must_match_exact_lock_bytes_and_dependency(self):
        for edit in (
            lambda data: data.replace(b"0.4.2", b"0.4.1"),
            lambda data: data.replace(b"[[package]]", b"[[different]]"),
            lambda data: data + b"\n[[package]]\n" + data.split(b"[[package]]", 1)[1],
            lambda data: b"not valid TOML!",
        ):
            with self.subTest(edit=edit):
                github = ToolkitGitHub()
                github.lock = edit(github.lock)
                # Exercise dependency validation independently of whole-file hash checks.
                github.receipt["toolkit"]["python_lock_sha256"] = hashlib.sha256(
                    github.lock
                ).hexdigest()
                with self.assertRaises(core.VerificationError):
                    github.verify()
        github = ToolkitGitHub()
        github.lock += b"\n"
        with self.assertRaisesRegex(core.VerificationError, "tagged plugin source"):
            github.verify()

    def test_lock_source_and_wheel_hash_cannot_differ_from_receipt(self):
        for old, new in (
            (BASE.encode(), b"https://attacker.invalid/"),
            (b"url = ", b"path = "),
            (ToolkitGitHub().receipt["toolkit"]["sha256"].encode(), b"0" * 64),
        ):
            with self.subTest(new=new):
                github = ToolkitGitHub()
                github.lock = github.lock.replace(old, new)
                github.receipt["toolkit"]["python_lock_sha256"] = hashlib.sha256(
                    github.lock
                ).hexdigest()
                with self.assertRaises(core.VerificationError):
                    github.verify()

    def test_release_must_be_stable_and_assets_canonical_and_unambiguous(self):
        for mutate in (
            lambda release: release.update(draft=True),
            lambda release: release.update(prerelease=True),
            lambda release: release["assets"].pop(),
            lambda release: release["assets"].append(release["assets"][0]),
            lambda release: release["assets"][0].update(
                browser_download_url="https://attacker.invalid/wheel"
            ),
        ):
            github = ToolkitGitHub()
            mutate(github.release)
            with self.assertRaises(core.VerificationError):
                github.verify()

    def test_tampered_distributions_checksums_and_provenance_are_rejected(self):
        for name in (WHEEL, SDIST, "SHA256SUMS", "build-provenance.json"):
            with self.subTest(name=name):
                github = ToolkitGitHub()
                github.files[name] += b"tampered"
                with self.assertRaises(core.VerificationError):
                    github.verify()
        for change in (
            {"revision": "c" * 40},
            {"version": "0.4.1"},
            {"files": {}},
            {"schema": 1},
        ):
            github = ToolkitGitHub()
            provenance = json.loads(github.files["build-provenance.json"])
            provenance.update(change)
            github.files["build-provenance.json"] = json.dumps(provenance).encode()
            with self.assertRaisesRegex(
                core.VerificationError, "published tag or distributions"
            ):
                github.verify()

    def test_moved_tag_and_duplicate_checksums_are_rejected(self):
        github = ToolkitGitHub()
        github.revision = "c" * 40
        with self.assertRaisesRegex(core.VerificationError, "published tag"):
            github.verify()
        github = ToolkitGitHub()
        github.files["SHA256SUMS"] *= 2
        with self.assertRaisesRegex(
            core.VerificationError, "duplicate toolkit checksum"
        ):
            github.verify()

    def test_schema_seven_attestation_pins_the_toolkit_signer_commit(self):
        github = ToolkitGitHub()
        policy = dict(
            POLICY,
            **github.policy,
            toolkit_attestation_workflow=".github/workflows/release.yml",
        )
        fixture = FakeGitHub()
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
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / "validation.json"
            receipt.write_text(json.dumps(github.receipt))
            with (
                patch.dict(core.os.environ, {"SOURCE_GH_TOKEN": "fixture"}),
                patch.object(
                    core.subprocess, "check_output", return_value=json.dumps(output)
                ) as verify,
                patch.object(
                    github, "toolkit_revision", return_value=REVISION
                ) as pinned,
                patch.object(
                    github,
                    "json",
                    side_effect=[
                        fixture.run,
                        {
                            "jobs": [
                                {"name": "release / attach", "conclusion": "success"}
                            ]
                        },
                    ],
                ),
            ):
                github.attest(SOURCE, policy, receipt)
                pinned.assert_called_once_with(SOURCE, policy, github.receipt)
                command = verify.call_args.args[0]
                self.assertEqual(
                    TOOLKIT + "/.github/workflows/release.yml",
                    command[command.index("--signer-workflow") + 1],
                )
                self.assertEqual(
                    REVISION, command[command.index("--signer-digest") + 1]
                )

    def test_schema_seven_bootstrap_verifies_toolkit_before_public_assets(self):
        github = FakeGitHub()
        release = github.releases[0]
        github.receipts[release["tag_name"]].update(ToolkitGitHub().receipt)
        policy = dict(POLICY, receipt_schemas=[3, 7], toolkit_repository=TOOLKIT)
        original = github.receipt

        def bootstrap(*args, **kwargs):
            github.bootstrap_proof = {"artifact_id": 1}
            return original(*args, **kwargs)

        with (
            patch.object(github, "receipt", side_effect=bootstrap),
            patch.object(
                github,
                "toolkit_revision",
                side_effect=core.VerificationError("invalid toolkit"),
            ) as verify,
        ):
            with self.assertRaisesRegex(core.VerificationError, "invalid toolkit"):
                core.verify_release(github, SOURCE, policy, release)
            verify.assert_called_once()

    def test_current_receipt_and_optional_web_profile_pass_public_asset_validation(
        self,
    ):
        for web in (False, True):
            with self.subTest(web=web):
                github = FakeGitHub()
                release = github.releases[0]
                receipt = github.receipts[release["tag_name"]]
                receipt.update(ToolkitGitHub().receipt)
                policy = dict(POLICY, receipt_schemas=[3, 7], web=web)
                if not web:
                    name = "lyricbridge-web-0.1.3.zip"
                    release["assets"] = [
                        a for a in release["assets"] if a["name"] != name
                    ]
                    receipt["artifacts"].pop(name)
                    base = f"https://github.com/{SOURCE}/releases/download/v0.1.3/"
                    sums = github.files[base + "SHA256SUMS"].decode()
                    github.files[base + "SHA256SUMS"] = "".join(
                        line + "\n"
                        for line in sums.splitlines()
                        if not line.endswith("  " + name)
                    ).encode()
                    receipt["artifacts"]["SHA256SUMS"] = hashlib.sha256(
                        github.files[base + "SHA256SUMS"]
                    ).hexdigest()
                core.verify_release(github, SOURCE, policy, release)
                receipt["schema"] = 4
                with self.assertRaisesRegex(core.VerificationError, "schema"):
                    core.verify_release(github, SOURCE, policy, release)

    def test_catalog_policy_accepts_only_historical_and_current_schemas(self):
        policies = json.loads(
            (Path(__file__).resolve().parents[1] / "plugins.json").read_text()
        )
        self.assertEqual([3, 7], policies[SOURCE]["receipt_schemas"])
        self.assertEqual(
            [7],
            policies["rakuzen25/jellyfin-plugin-music-metadata-manager"][
                "receipt_schemas"
            ],
        )
