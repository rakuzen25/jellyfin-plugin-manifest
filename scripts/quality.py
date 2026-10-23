"""Local and CI formatting and linting entrypoint for the toolkit itself."""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "format"])
    fix = parser.parse_args().command == "format"
    root = Path(__file__).resolve().parents[1]
    python = (
        str(root / ".venv/bin/python")
        if (root / ".venv/bin/python").exists()
        else sys.executable
    )
    commands = [
        [python, "-m", "ruff", "format", *([] if fix else ["--check"]), "."],
        [
            "pnpm",
            "exec",
            "prettier",
            "--write" if fix else "--check",
            "**/*.{md,json,mjs,ts,yml,yaml}",
        ],
    ]
    if not fix:
        commands += [[python, "-m", "ruff", "check", "scripts", "tests"]]
    for command in commands:
        subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
