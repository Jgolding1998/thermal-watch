import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock

from thermal_watch.monitor import Rule, load_rules


RULE = {"sensor": "freezer", "label": "Test freezer", "minimum_c": -24, "maximum_c": -14}


class ConfigLimitsTests(unittest.TestCase):
    def test_duplicate_keys_cannot_silently_replace_thresholds(self):
        documents = ['{"sensors":[],"sensors":[' + json.dumps(RULE) + ']}',
                     '{"sensors":[{"sensor":"freezer","label":"Test","minimum_c":-24,"minimum_c":-10,"maximum_c":-14}]}']
        for document in documents:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(document)
                with self.assertRaisesRegex(ValueError, "duplicate keys"):
                    load_rules(path)

    def test_read_is_bounded_without_trusting_a_size_check(self):
        stream = io.BytesIO(b" " * 40000)
        path = Mock()
        path.open.return_value = MagicMock()
        path.open.return_value.__enter__.return_value = stream
        with self.assertRaisesRegex(ValueError, "too large"):
            load_rules(path)
        self.assertEqual(stream.tell(), 32769)
        path.stat.assert_not_called()

    def test_invalid_utf8_and_nonprintable_labels_are_rejected(self):
        for label in ("\ud800", "line\nbreak", "tab\tlabel", "\x00", "\u202e", "   "):
            with self.assertRaises(ValueError):
                Rule(**{**RULE, "label": label})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
                load_rules(path)
        self.assertEqual(Rule(**{**RULE, "label": "Freezer 2 °C"}).label, "Freezer 2 °C")

    def test_malformed_shapes_constants_and_deep_nesting_have_fixed_errors(self):
        documents = [json.dumps({"sensors": [entry]}) for entry in
                     ([], "PRIVATE_TEXT", {**RULE, "PRIVATE_FIELD": 1}, {"label": "PRIVATE_TEXT"})]
        documents += ['{"sensors":[{"minimum_c":NaN}]}']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for document in documents:
                with self.subTest(document=document[:35]):
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaises(ValueError) as raised:
                        load_rules(path)
                    self.assertNotIn("PRIVATE", str(raised.exception))
                    if "PRIVATE" in document or '"sensors": [[]]' in document:
                        self.assertEqual(str(raised.exception), "Each sensor must contain the required rule fields and no unknown fields")

    def test_deep_nesting_is_a_fixed_parse_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("[" * 16000 + "]" * 16000, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "^Configuration must be valid UTF-8 JSON$"):
                load_rules(path)

    def test_exact_file_size_limit_preserves_a_valid_document(self):
        document = json.dumps({"sensors": [RULE]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(document + b" " * (32768 - len(document)))
            self.assertEqual(load_rules(path), [Rule(**RULE)])
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "too large"):
                load_rules(path)


if __name__ == "__main__":
    unittest.main()
