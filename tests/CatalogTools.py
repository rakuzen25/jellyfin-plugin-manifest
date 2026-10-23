"""Pinned verifier installation and automation validation boundaries."""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tools


class ToolTests(unittest.TestCase):
    def archive(self):
        version = json.loads((tools.ROOT / "toolchain.json").read_text())["gh"]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            item = tarfile.TarInfo(f"gh_{version}_linux_amd64/bin/gh")
            data = b"fixture executable"
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        return version, buffer.getvalue()

    def test_installer_verifies_archive_and_installs_only_expected_binary(self):
        version, archive = self.archive()
        checksum = f"{hashlib.sha256(archive).hexdigest()}  gh_{version}_linux_amd64.tar.gz\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bin"
            with (
                patch.object(
                    tools.urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(checksum), io.BytesIO(archive)],
                ),
                patch.object(tools.subprocess, "run") as run,
                patch.dict(tools.os.environ, {}, clear=True),
            ):
                tools.install_gh(destination)
            self.assertEqual((destination / "gh").read_bytes(), b"fixture executable")
            self.assertEqual((destination / "gh").stat().st_mode & 0o777, 0o755)
            run.assert_called_once_with(
                [str(destination / "gh"), "--version"], check=True
            )

    def test_bad_checksum_writes_no_executable(self):
        version, archive = self.archive()
        checksum = f"{'0' * 64}  gh_{version}_linux_amd64.tar.gz\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bin"
            with (
                patch.object(
                    tools.urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(checksum), io.BytesIO(archive)],
                ),
                self.assertRaisesRegex(ValueError, "checksum mismatch"),
            ):
                tools.install_gh(destination)
            self.assertFalse(destination.exists())

    def test_runtime_and_workflow_pins_agree(self):
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
        version = project["tool"]["uv"]["required-version"].removeprefix("==")
        for workflow in (tools.ROOT / ".github/workflows").glob("*.yml"):
            text = workflow.read_text()
            self.assertIn('version: "' + version + '"', text)
            self.assertIn("python-version-file: .python-version", text)
            self.assertIn("uv sync --locked", text)
            self.assertIn("scripts/tools.py install-gh", text)
            self.assertNotIn("pip install", text)

    def test_actionlint_exception_is_specific_and_tools_have_no_network(self):
        with patch.object(tools.subprocess, "run") as run:
            tools.check_config()
        self.assertEqual(run.call_count, 2)
        actionlint, renovate = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            actionlint[actionlint.index("-ignore") + 1],
            '^unexpected key "queue" for "concurrency" section\\.',
        )
        for command in (actionlint, renovate):
            self.assertIn("none", command)
            self.assertTrue(any("sha256:" in item for item in command))
        self.assertIn("--strict", renovate)
