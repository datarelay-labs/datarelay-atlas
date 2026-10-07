import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atlas.provenance import ValidationError
from atlas.write_probe import MAX_VALUE_BYTES, TTL_SECONDS, WriteProbeStore


class WriteProbeStoreTests(unittest.TestCase):
    def test_create_get_delete_round_trip_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas.write_probe.tempfile.gettempdir", return_value=tmp):
                store = WriteProbeStore("https://mcp.example/mcp")
                created = store.create("probe-value")
                self.assertTrue(created["isolated"])
                self.assertFalse(created["canonical"])
                self.assertFalse(created["derived_knowledge"])
                self.assertEqual(created["expires_at"] - created["created_at"], TTL_SECONDS)
                self.assertEqual(store.root.parent, Path(tmp))
                self.assertEqual(store.get(created["probe_id"])["value"], "probe-value")
                self.assertTrue(store.delete(created["probe_id"])["deleted"])
                with self.assertRaisesRegex(ValidationError, "not found"):
                    store.get(created["probe_id"])

    def test_value_and_id_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas.write_probe.tempfile.gettempdir", return_value=tmp):
                store = WriteProbeStore("https://mcp.example/mcp")
                with self.assertRaisesRegex(ValidationError, "required"):
                    store.create(" ")
                with self.assertRaisesRegex(ValidationError, "512 bytes"):
                    store.create("x" * (MAX_VALUE_BYTES + 1))
                with self.assertRaisesRegex(ValidationError, "invalid"):
                    store.get("../escape")

    def test_expired_record_is_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas.write_probe.tempfile.gettempdir", return_value=tmp):
                with patch("atlas.write_probe.time.time", return_value=1000):
                    store = WriteProbeStore("https://mcp.example/mcp")
                    created = store.create("short-lived")
                with patch("atlas.write_probe.time.time", return_value=1000 + TTL_SECONDS + 1):
                    with self.assertRaisesRegex(ValidationError, "not found"):
                        store.get(created["probe_id"])


if __name__ == "__main__":
    unittest.main()
