"""Deterministic shared-catalog validation and preservation regressions."""

import copy
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from catalog import merge_catalog, validate_catalog

FIXTURE_GUID = "11111111-1111-4111-8111-111111111111"


def entry(guid: str, version: str = "0.1.0.0") -> dict[str, Any]:
    return {
        "guid": guid,
        "name": "Fixture",
        "custom": {"preserve": True},
        "versions": [
            {
                "version": version,
                "targetAbi": "12.2.0.0",
                "sourceUrl": "https://example.invalid/plugin.zip",
                "checksum": "a" * 32,
                "timestamp": "2026-10-11T00:00:00Z",
                "unknown": [1, 2],
            }
        ],
    }


class CatalogTests(unittest.TestCase):
    def test_independent_plugins_and_versions_are_preserved(self) -> None:
        plugin = entry(FIXTURE_GUID)
        other = entry("6d10e04d-ff20-41b6-ace7-1b408d180fde")
        existing = [plugin, other]
        before = copy.deepcopy(existing)
        incoming = entry(plugin["guid"], "0.1.1.0")
        merged = merge_catalog(existing, [incoming])
        self.assertEqual(existing, before)
        self.assertEqual(merged[1], other)
        self.assertEqual(merged[0]["versions"][0], plugin["versions"][0])
        self.assertEqual(merged[0]["custom"], plugin["custom"])
        self.assertEqual(len(merged[0]["versions"]), 2)
        self.assertEqual(merge_catalog(merged, [incoming]), merged)

    def test_existing_version_preserves_unknown_metadata(self) -> None:
        plugin = entry(FIXTURE_GUID)
        incoming = copy.deepcopy(plugin)
        incoming["versions"][0].pop("unknown")
        incoming["versions"][0]["newMetadata"] = {"future": True}
        merged = merge_catalog([plugin], [incoming])
        self.assertEqual(merged[0]["versions"][0]["unknown"], [1, 2])
        self.assertEqual(merged[0]["versions"][0]["newMetadata"], {"future": True})
        self.assertEqual(plugin["versions"][0]["unknown"], [1, 2])

    def test_conflicting_version_is_rejected(self) -> None:
        plugin = entry(FIXTURE_GUID)
        incoming = copy.deepcopy(plugin)
        incoming["versions"][0]["checksum"] = "b" * 32
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            merge_catalog([plugin], [incoming])

    def test_invalid_catalogs_are_rejected(self) -> None:
        plugin = entry(FIXTURE_GUID)
        invalid = [[plugin, copy.deepcopy(plugin)]]
        duplicate = copy.deepcopy(plugin)
        duplicate["versions"].append(copy.deepcopy(duplicate["versions"][0]))
        invalid.append([duplicate])
        for key, value in (
            ("targetAbi", "12.2"),
            ("checksum", "broken"),
            ("sourceUrl", "http://example.invalid"),
            ("sourceUrl", "https://user:secret@example.invalid"),
            ("timestamp", "2026-10-11"),
        ):
            candidate = copy.deepcopy(plugin)
            candidate["versions"][0][key] = value
            invalid.append([candidate])
        for catalog in invalid:
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                validate_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
