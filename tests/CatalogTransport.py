"""Offline GitHub authentication, redirect and download boundary regressions."""

import io
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import catalog_core as core


class TransportTests(unittest.TestCase):
    def test_api_accepts_source_only_token_and_prefers_it_to_catalog_token(self):
        for environment, expected in (
            ({"SOURCE_GH_TOKEN": "fixture-source"}, "fixture-source"),
            (
                {"SOURCE_GH_TOKEN": "fixture-source", "GH_TOKEN": "fixture-catalog"},
                "fixture-source",
            ),
            ({"SOURCE_GH_TOKEN": "", "GH_TOKEN": "fixture-catalog"}, "fixture-catalog"),
            ({"GH_TOKEN": "fixture-catalog"}, "fixture-catalog"),
        ):
            with (
                self.subTest(environment=list(environment)),
                patch.dict(core.os.environ, environment, clear=True),
                patch.object(
                    core.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(b'{"id": 42}'),
                ) as opened,
            ):
                self.assertEqual(
                    core.GitHub().json("fixture/source", "/releases"), {"id": 42}
                )
                request = opened.call_args.args[0]
                self.assertEqual(
                    request.full_url,
                    "https://api.github.com/repos/fixture/source/releases",
                )
                self.assertEqual(
                    request.get_header("Authorization"), "Bearer " + expected
                )

    def test_missing_tokens_fail_before_opening_network_or_attestation_process(self):
        with (
            patch.dict(core.os.environ, {}, clear=True),
            patch.object(core.urllib.request, "urlopen") as opened,
            patch.object(core.subprocess, "check_output") as process,
        ):
            with self.assertRaisesRegex(core.VerificationError, "token is required"):
                core.GitHub().json("fixture/source", "/releases")
            with self.assertRaisesRegex(core.VerificationError, "token is required"):
                core.GitHub().attest(
                    "fixture/source",
                    {"attestation_workflow": "release.yml"},
                    Path("receipt.json"),
                )
        opened.assert_not_called()
        process.assert_not_called()

    def test_authenticated_download_uses_source_token_and_public_download_needs_none(
        self,
    ):
        for authenticated in (True, False):
            with (
                self.subTest(authenticated=authenticated),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(
                    core.os.environ,
                    {"SOURCE_GH_TOKEN": "fixture-source"} if authenticated else {},
                    clear=True,
                ),
                patch.object(core.urllib.request, "build_opener") as factory,
            ):
                opener = factory.return_value
                opener.open.return_value = io.BytesIO(b"fixture")
                destination = Path(temporary) / "asset.zip"
                core.GitHub().download(
                    "https://api.github.com/fixture",
                    destination,
                    maximum=7,
                    authenticated=authenticated,
                )
                self.assertEqual(destination.read_bytes(), b"fixture")
                header = opener.open.call_args.args[0].get_header("Authorization")
                self.assertEqual(
                    header, "Bearer fixture-source" if authenticated else None
                )

    def test_download_limit_accepts_exact_size_and_rejects_one_extra_byte(self):
        for data in (b"abcd", b"abcde"):
            with (
                self.subTest(size=len(data)),
                tempfile.TemporaryDirectory() as temporary,
                patch.object(core.urllib.request, "build_opener") as factory,
            ):
                factory.return_value.open.return_value = io.BytesIO(data)
                destination = Path(temporary) / "asset.zip"
                if len(data) == 4:
                    core.GitHub().download(
                        "https://github.com/fixture", destination, maximum=4
                    )
                    self.assertEqual(destination.read_bytes(), data)
                else:
                    with self.assertRaisesRegex(core.VerificationError, "safety limit"):
                        core.GitHub().download(
                            "https://github.com/fixture", destination, maximum=4
                        )
                    self.assertLessEqual(destination.stat().st_size, 4)

    def test_unapproved_download_origins_are_rejected_before_http(self):
        with patch.object(core.urllib.request, "build_opener") as factory:
            for url in (
                "http://github.com/fixture",
                "https://example.invalid/fixture",
                "https://github.com.example.invalid/fixture",
            ):
                with self.subTest(url=url), self.assertRaises(core.VerificationError):
                    core.GitHub().download(url, Path("unused.zip"))
        factory.assert_not_called()

    def test_redirect_rejects_http_and_strips_authorization_from_https(self):
        handler = core.PublicRedirect()
        request = urllib.request.Request(
            "https://api.github.com/fixture",
            headers={"Authorization": "Bearer fixture-source"},
        )
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://release-assets.githubusercontent.com/fixture",
        )
        self.assertEqual(
            redirected.full_url, "https://release-assets.githubusercontent.com/fixture"
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(core.VerificationError, "not HTTPS"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://example.invalid/fixture"
            )

    def test_pagination_collects_full_pages_and_stops_at_a_short_page(self):
        github = core.GitHub()
        first = [{"id": number} for number in range(100)]
        last = [{"id": 100}]
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                pages = [first, last]
                if wrapped:
                    pages = [{"workflow_runs": page} for page in pages]
                with patch.object(github, "json", side_effect=pages):
                    self.assertEqual(
                        list(github.pages("fixture/source", "/releases")), first + last
                    )

    def test_pagination_limit_refuses_an_incomplete_release_inventory(self):
        github = core.GitHub()
        with (
            patch.object(
                github, "json", return_value=[{"id": number} for number in range(100)]
            ),
            self.assertRaisesRegex(core.VerificationError, "refusing incomplete"),
        ):
            list(github.pages("fixture/source", "/releases", limit=1))

    def test_annotated_tags_resolve_to_the_source_commit_with_an_encoded_ref(self):
        github = core.GitHub()
        commit = "a" * 40
        with patch.object(
            github,
            "json",
            side_effect=[
                {"object": {"type": "tag", "sha": "b" * 40}},
                {"object": {"type": "commit", "sha": commit}},
            ],
        ) as api:
            self.assertEqual(github.tag("fixture/source", "release/tag #1"), commit)
        self.assertEqual(
            api.call_args_list[0].args[1], "/git/ref/tags/release%2Ftag%20%231"
        )
        self.assertEqual(api.call_args_list[1].args[1], "/git/tags/" + "b" * 40)

    def test_tags_that_cannot_resolve_to_commits_fail_with_bounded_lookup(self):
        github = core.GitHub()
        for object_type in ("tree", "tag"):
            with (
                self.subTest(object_type=object_type),
                patch.object(
                    github,
                    "json",
                    return_value={"object": {"type": object_type, "sha": "b" * 40}},
                ),
                self.assertRaisesRegex(core.VerificationError, "does not resolve"),
            ):
                github.tag("fixture/source", "v0.1.3")


if __name__ == "__main__":
    unittest.main()
