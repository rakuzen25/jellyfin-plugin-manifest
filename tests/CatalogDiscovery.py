"""Verify a local shared manifest in an owned, disposable official Jellyfin.

No caller-supplied server URL, host config/media mount or production credentials.
This manual integration check is separate from the catalog's fast PR gate.
"""

import argparse
import functools
import http.server
import json
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

IMAGE = "jellyfin/jellyfin@sha256:357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037"


class QuietFiles(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _install_path(name: str, guid: str, version: str) -> str:
    query = urlencode({"assemblyGuid": guid, "version": version})
    return f"/Packages/Installed/{quote(name, safe='')}?{query}"


def discovery(manifest: Path) -> None:
    entries = json.loads(manifest.read_text())
    if not entries:
        raise ValueError("No catalog entries available to test")
    name = "catalog-discovery-" + uuid.uuid4().hex[:12]

    def docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], text=True).strip()

    gateway = docker(
        "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"
    )
    with tempfile.TemporaryDirectory(prefix="catalog-discovery-") as directory:
        Path(directory, "manifest.json").write_text(json.dumps(entries))
        server = http.server.ThreadingHTTPServer(
            (gateway, 0), functools.partial(QuietFiles, directory=directory)
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        token = ""
        password = ""

        def call(path: str, method: str = "GET", body: Any = None) -> Any:
            authorization = 'MediaBrowser Client="CatalogTests", Device="Disposable", DeviceId="catalog", Version="1"'
            if token:
                authorization += f', Token="{token}"'
            request = urllib.request.Request(
                base + path,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                },
                method=method,
                data=None if body is None else json.dumps(body).encode(),
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
                return json.loads(data) if data else None

        def ready() -> None:
            for _ in range(120):
                try:
                    if (
                        call("/System/Info/Public")
                        .get("Version", "")
                        .startswith("12.2")
                    ):
                        return
                except urllib.error.URLError, TimeoutError, ConnectionError:
                    pass
                time.sleep(0.5)
            raise RuntimeError("Owned Jellyfin did not become ready")

        try:
            docker(
                "run",
                "-d",
                "--platform",
                "linux/amd64",
                "--name",
                name,
                "--label",
                "lyricbridge.disposable-test=true",
                "-p",
                f"127.0.0.1:{port}:8096",
                "--tmpfs",
                "/config:rw,size=3g",
                "--tmpfs",
                "/cache:rw,size=3g",
                "--entrypoint",
                "/bin/sh",
                IMAGE,
                "-c",
                'while true; do /jellyfin/jellyfin & child=$!; echo "$child" > /tmp/catalog-server.pid; wait "$child"; sleep 1; done',
            )
            ready()
            call(
                "/Startup/Configuration",
                "POST",
                {
                    "UICulture": "en-US",
                    "MetadataCountryCode": "US",
                    "PreferredMetadataLanguage": "en",
                },
            )
            password = secrets.token_urlsafe(24)
            call("/Startup/User")
            call(
                "/Startup/User", "POST", {"Name": "catalog-admin", "Password": password}
            )
            call("/Startup/Complete", "POST", {})
            token = call(
                "/Users/AuthenticateByName",
                "POST",
                {"Username": "catalog-admin", "Pw": password},
            )["AccessToken"]
            call(
                "/Repositories",
                "POST",
                [
                    {
                        "Name": "Local shared catalog candidate",
                        "Url": f"http://{gateway}:{server.server_port}/manifest.json",
                        "Enabled": True,
                    }
                ],
            )
            packages = call("/Packages")
            for entry in entries:
                package = next(
                    p
                    for p in packages
                    if p.get("guid", p.get("Guid", "")).lower().replace("-", "")
                    == entry["guid"].replace("-", "")
                )
                if package["name"] != entry["name"]:
                    raise AssertionError("Catalog name changed during discovery")
                version = entry["versions"][0]["version"]
                call(
                    _install_path(entry["name"], entry["guid"], version),
                    "POST",
                    {},
                )
            previous = docker("exec", name, "cat", "/tmp/catalog-server.pid")
            docker("exec", name, "sh", "-c", 'kill "$(cat /tmp/catalog-server.pid)"')
            for _ in range(120):
                if docker("exec", name, "cat", "/tmp/catalog-server.pid") != previous:
                    break
                time.sleep(0.5)
            ready()
            plugins = call("/Plugins")
            for entry in entries:
                plugin = next(
                    p
                    for p in plugins
                    if p["Id"].lower().replace("-", "")
                    == entry["guid"].replace("-", "")
                )
                if plugin["Status"] != "Active":
                    raise AssertionError("Catalogued plugin did not become active")
            print(
                "PASS: shared manifest discovered and exact public plugins installed/active in disposable Jellyfin 12.2"
            )
        except Exception:
            try:
                logs = docker("logs", "--tail", "40", name)
                for secret in (token, password):
                    if secret:
                        logs = logs.replace(secret, "[redacted]")
                print(logs, flush=True)
            except subprocess.CalledProcessError:
                pass
            raise
        finally:
            subprocess.run(
                ["docker", "rm", "-f", "-v", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    discovery(parser.parse_args().manifest)
