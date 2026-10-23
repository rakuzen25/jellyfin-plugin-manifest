"""Install the pinned verifier and validate maintained automation configuration."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_gh(destination):
    version = json.loads((ROOT / "toolchain.json").read_text())["gh"]
    name = f"gh_{version}_linux_amd64.tar.gz"
    base = f"https://github.com/cli/cli/releases/download/v{version}/"
    with urllib.request.urlopen(
        base + f"gh_{version}_checksums.txt", timeout=60
    ) as response:
        checksums = response.read().decode().splitlines()
    expected = [line.split()[0] for line in checksums if line.split()[-1] == name]
    with urllib.request.urlopen(base + name, timeout=60) as response:
        archive = response.read()
    if len(expected) != 1 or hashlib.sha256(archive).hexdigest() != expected[0]:
        raise ValueError("GitHub CLI archive checksum mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        binary = package.extractfile(f"gh_{version}_linux_amd64/bin/gh")
        if binary is None:
            raise ValueError("GitHub CLI archive has no expected executable")
        (destination / "gh").write_bytes(binary.read())
    (destination / "gh").chmod(0o755)
    subprocess.run([str(destination / "gh"), "--version"], check=True)
    if os.environ.get("GITHUB_PATH"):
        with Path(os.environ["GITHUB_PATH"]).open("a") as output:
            output.write(str(destination.resolve()) + "\n")


def check_config():
    pins = json.loads((ROOT / "toolchain.json").read_text())
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{ROOT}:/work:ro",
            "-w",
            "/work",
            pins["actionlint"],
            "-ignore",
            '^unexpected key "queue" for "concurrency" section\\.',
        ],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "renovate-config-validator",
            "-v",
            f"{ROOT}:/work:ro",
            pins["renovate"],
            "--strict",
            "/work/renovate.json",
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install-gh", "check-config"])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.command == "install-gh":
        if args.directory is None:
            parser.error("install-gh requires --directory")
        install_gh(args.directory)
    else:
        check_config()


if __name__ == "__main__":
    main()
