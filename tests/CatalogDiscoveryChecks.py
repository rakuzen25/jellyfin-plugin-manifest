"""Pure URL checks for the optional disposable Jellyfin discovery test."""

import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from CatalogDiscovery import install_path


class DiscoveryUrlTests(unittest.TestCase):
    def test_plugin_names_stay_in_one_encoded_path_component(self) -> None:
        guid = "70a9d327-7e98-4201-88a5-724d39366c40"
        for name in ("Fixture Plugin", "Lyric/Bridge & More?#", "歌詞 Plugin"):
            with self.subTest(name=name):
                parsed = urlsplit(install_path(name, guid, "0.1.3.0"))
                component = parsed.path.removeprefix("/Packages/Installed/")
                self.assertNotIn("/", component)
                self.assertNotIn(" ", component)
                self.assertEqual(unquote(component), name)
                self.assertEqual(parsed.fragment, "")
                self.assertEqual(
                    parse_qs(parsed.query),
                    {
                        "assemblyGuid": [guid],
                        "version": ["0.1.3.0"],
                    },
                )

    def test_simple_plugin_names_keep_the_existing_install_url(self) -> None:
        self.assertEqual(
            install_path("LyricBridge", "fixture", "0.1.3.0"),
            "/Packages/Installed/LyricBridge?assemblyGuid=fixture&version=0.1.3.0",
        )


if __name__ == "__main__":
    unittest.main()
