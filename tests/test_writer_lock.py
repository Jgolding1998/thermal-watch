"""Coordinate CSV producers without modifying a peer's locked byte range."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thermal_watch.inputs import _writer_lock, append_reading


class WriterLockTests(unittest.TestCase):
    def test_new_lock_needs_no_marker_write_and_legacy_marker_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.csv"
            lock = path.with_name(path.name + ".lock")
            with _writer_lock(path):
                self.assertTrue(lock.is_file())
                self.assertEqual(lock.stat().st_size, 0)
            lock.write_bytes(b"\0")  # An existing installation's marker remains valid.
            with _writer_lock(path):
                self.assertEqual(lock.stat().st_size, 1)
            self.assertEqual(lock.read_bytes(), b"\0")
            append_reading(path, "synthetic", -18, 1000)
            self.assertIn("synthetic", path.read_text("utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows enforces byte-range write exclusion")
    def test_peer_lock_beyond_empty_eof_returns_busy_without_writing_or_leaking(self):
        import msvcrt
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.csv"
            path.write_bytes(b"SYNTHETIC-UNCHANGED")
            lock = path.with_name(path.name + ".lock")
            original_open = Path.open
            opened = []

            def track_open(candidate, *args, **kwargs):
                handle = original_open(candidate, *args, **kwargs)
                if candidate == lock:
                    opened.append(handle)
                return handle

            # Windows permits byte-range locks past EOF. This represents the peer
            # winning initialization while this writer still sees an empty file.
            with lock.open("a+b", buffering=0) as peer:
                peer.seek(0)
                msvcrt.locking(peer.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    with patch.object(Path, "open", new=track_open):
                        with self.assertRaisesRegex(ValueError, "Another producer holds the CSV lock"):
                            with _writer_lock(path, timeout=0):
                                self.fail("Entered a peer's locked producer section")
                    self.assertEqual(len(opened), 1)
                    self.assertTrue(opened[0].closed)
                    self.assertEqual(lock.stat().st_size, 0)
                    self.assertEqual(path.read_bytes(), b"SYNTHETIC-UNCHANGED")
                finally:
                    peer.seek(0)
                    msvcrt.locking(peer.fileno(), msvcrt.LK_UNLCK, 1)
                    for handle in opened:
                        if not handle.closed:
                            handle.close()
            # The refusal must leave the next valid producer usable after release.
            with _writer_lock(path, timeout=0):
                self.assertEqual(lock.stat().st_size, 0)
