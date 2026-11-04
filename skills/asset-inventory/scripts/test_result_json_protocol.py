"""Offline result-file protocol tests; no browser, network or bill fixtures."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "pan_suan_builder_result_protocol", Path(__file__).with_name("pan_suan_builder.py")
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ResultJsonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f"builder_protocol_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.template = self.root / "template.xlsx"
        self.output = self.root / "output.xlsx"
        self.result_path = self.root / "result.json"
        self.result = {
            "output": str(self.output), "product_count": 3,
            "allocated_rows": 4, "unassigned_rows": 2,
            "sheet_write_verified": True, "presentation_verified": True,
            "imported_sheets": ["商品货款"], "import_stats": [{"written_rows": 4}],
        }

    def run_main(self, *extra):
        argv = ["builder", "--template", str(self.template), "--output", str(self.output), *extra]
        stdout = io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
            status = builder.main()
        return status, stdout.getvalue()

    def test_versioned_file_and_legacy_stdout_are_independent(self):
        with patch.object(builder, "merge", return_value=self.result):
            status, stdout = self.run_main("--result-json", str(self.result_path), "--json")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), self.result)
        self.assertEqual(json.loads(self.result_path.read_text(encoding="utf-8")),
                         {**self.result, "schemaVersion": 1})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_legacy_json_requires_no_result_file(self):
        with patch.object(builder, "merge", return_value=self.result):
            status, stdout = self.run_main("--json")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), self.result)
        self.assertFalse(self.result_path.exists())

    def test_old_success_removed_before_merge_failure(self):
        self.result_path.write_text('{"schemaVersion":1}', encoding="utf-8")
        def failed_merge(*args):
            self.assertFalse(self.result_path.exists())
            raise ValueError("synthetic merge failure")
        with patch.object(builder, "merge", side_effect=failed_merge):
            with self.assertRaisesRegex(ValueError, "synthetic merge failure"):
                self.run_main("--result-json", str(self.result_path))
        self.assertFalse(self.result_path.exists())

    def test_real_merge_failure_invalidates_old_success(self):
        self.result_path.write_text('{"schemaVersion":1}', encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            self.run_main("--result-json", str(self.result_path))
        self.assertFalse(self.result_path.exists())

    def test_atomic_replace_publishes_complete_document(self):
        destination = builder.prepare_result_json(self.result_path, [])
        replace = builder.os.replace
        def checked_replace(source, target):
            self.assertEqual(Path(source).parent, destination.parent)
            self.assertFalse(destination.exists())
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")),
                             {**self.result, "schemaVersion": 1})
            replace(source, target)
        with patch.object(builder.os, "replace", side_effect=checked_replace):
            builder.write_result_json(destination, self.result)
        self.assertTrue(destination.is_file())

    def test_failed_publish_leaves_no_success_or_temp_file(self):
        destination = builder.prepare_result_json(self.result_path, [])
        with patch.object(builder.os, "replace", side_effect=OSError("synthetic publish failure")):
            with self.assertRaisesRegex(OSError, "synthetic publish failure"):
                builder.write_result_json(destination, self.result)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reject_workbook_path_without_deleting_it(self):
        self.template.write_bytes(b"synthetic workbook sentinel")
        with self.assertRaisesRegex(ValueError, r"\.json"):
            builder.prepare_result_json(self.template, [self.template, self.output])
        self.assertEqual(self.template.read_bytes(), b"synthetic workbook sentinel")

    def test_reject_protected_json_path_and_directory(self):
        self.result_path.write_text("protected input", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            builder.prepare_result_json(self.result_path, [self.root / "." / "result.json"])
        self.assertEqual(self.result_path.read_text(encoding="utf-8"), "protected input")
        directory = self.root / "directory.json"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "普通文件"):
            builder.prepare_result_json(directory, [])
        self.assertTrue(directory.is_dir())

    def test_reject_hardlink_to_protected_file(self):
        self.template.write_bytes(b"protected workbook")
        try:
            self.result_path.hardlink_to(self.template)
        except OSError as error:
            self.skipTest(f"OS does not permit hardlink creation: {error}")
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            builder.prepare_result_json(self.result_path, [self.template])
        self.assertEqual(self.template.read_bytes(), b"protected workbook")
        self.assertTrue(self.result_path.is_file())

    def test_reject_symlink_even_when_target_is_missing(self):
        link = self.root / "link.json"
        try:
            link.symlink_to(self.root / "missing.json")
        except OSError as error:
            self.skipTest(f"OS does not permit symlink creation: {error}")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            builder.prepare_result_json(link, [])
        self.assertTrue(link.is_symlink())

    def test_nested_parent_is_created(self):
        destination = builder.prepare_result_json(self.root / "nested" / "result.json", [])
        self.assertTrue(destination.parent.is_dir())
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
