"""Pinned verifier installation and automation validation boundaries."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tools


class ToolTests(unittest.TestCase):
    def archive(self) -> tuple[str, bytes]:
        version = json.loads((tools.ROOT / "toolchain.json").read_text())["gh"]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            item = tarfile.TarInfo(f"gh_{version}_linux_amd64/bin/gh")
            data = b"fixture executable"
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        return version, buffer.getvalue()

    def test_installer_verifies_archive_and_installs_only_expected_binary(self) -> None:
        version, archive = self.archive()
        checksum = f"{hashlib.sha256(archive).hexdigest()}  gh_{version}_linux_amd64.tar.gz\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bin"
            with (
                patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(checksum), io.BytesIO(archive)],
                ),
                patch.object(subprocess, "run") as run,
                patch.dict(os.environ, {}, clear=True),
            ):
                tools.install_gh(destination)
            self.assertEqual((destination / "gh").read_bytes(), b"fixture executable")
            self.assertEqual((destination / "gh").stat().st_mode & 0o777, 0o755)
            run.assert_called_once_with(
                [str(destination / "gh"), "--version"], check=True
            )

    def test_bad_checksum_writes_no_executable(self) -> None:
        version, archive = self.archive()
        checksum = f"{'0' * 64}  gh_{version}_linux_amd64.tar.gz\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bin"
            with (
                patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(checksum), io.BytesIO(archive)],
                ),
                self.assertRaisesRegex(ValueError, "checksum mismatch"),
            ):
                tools.install_gh(destination)
            self.assertFalse(destination.exists())

    def test_runtime_and_workflow_pins_agree(self) -> None:
        package = json.loads((tools.ROOT / "package.json").read_text())
        self.assertEqual(
            package["engines"]["node"],
            (tools.ROOT / ".node-version").read_text().strip(),
        )
        self.assertEqual(
            package["packageManager"], "pnpm@" + package["engines"]["pnpm"]
        )
        python = (tools.ROOT / ".python-version").read_text().strip()
        project = tomllib.loads((tools.ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            "py" + "".join(python.split(".")[:2]),
            project["tool"]["ruff"]["target-version"],
        )
        shared_workflows = {
            "toolkit-sync.yml": "sync-bindings.yml",
            "pr-title.yml": "pr-title.yml",
            "app-setup.yml": "setup-automation.yml",
        }
        for workflow in (tools.ROOT / ".github/workflows").glob("*.yml"):
            text = workflow.read_text()
            if "uses: rakuzen25/jellyfin-plugin-toolkit/.github/workflows/" in text:
                self.assertIn(workflow.name, shared_workflows)
                self.assertRegex(
                    text,
                    r"uses: rakuzen25/jellyfin-plugin-toolkit/\.github/workflows/"
                    + shared_workflows[workflow.name].replace(".", r"\.")
                    + r"@[0-9a-f]{40} # v[0-9]+\.[0-9]+\.[0-9]+",
                )
                continue
            self.assertIn("/.github/actions/setup-python@", text)
            self.assertNotIn("astral-sh/setup-uv@", text)
            self.assertNotIn("uses: actions/setup-python@", text)
            self.assertIn(
                "mode: "
                + (
                    "runtime"
                    if workflow.name in {"sync.yml", "maintenance.yml"}
                    else "development"
                ),
                text,
            )
            if workflow.name not in {"advisories.yml"}:
                self.assertIn("scripts/tools.py install-gh", text)
            self.assertNotIn("pip install", text)

    def test_catalog_does_not_own_shared_validator_pins(self) -> None:
        pins = json.loads((tools.ROOT / "toolchain.json").read_text())
        self.assertEqual({"gh"}, set(pins))
        project = tomllib.loads((tools.ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            "tests/renovate.test.mjs",
            project["tool"]["jpt"]["quality"]["renovate_tests"],
        )
