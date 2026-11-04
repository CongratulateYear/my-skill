import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import contextlib
import shutil
import uuid

@contextlib.contextmanager
def test_directory():
    root = Path(__file__).resolve().parent / ('report-test-' + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asset_report import build_report, write_report
from validate_asset_workbook import DEFAULT_TEMPLATE, keyrow


class ReportTests(unittest.TestCase):
    def test_state_builder_join_and_security(self):
        state = {'completedModules': ['a'], 'results': {'a': {'targetSheet': '商品货款',
                 'queryRows': 3, 'taskId': 42, 'savedFiles': ['source.xlsx'], 'writtenRows': 0,
                 'password': 'secret', 'fileUrl': 'https://host/file?signature=secret'}}}
        builder = {'output': 'out.xlsx', 'sheet_write_verified': True, 'product_count': 2,
                   'import_stats': [{'target': '商品货款', 'source': 'source.xlsx',
                                    'written_rows': 2, 'cookie': 'hidden'}]}
        report = build_report(state, builder, {'ok': True}, shop='店铺')
        self.assertEqual(report['modules'][0]['writtenRows'], 2)
        self.assertTrue(report['modules'][0]['sheetWriteVerified'])
        self.assertEqual(report['modules'][0]['taskId'], 42)
        text = json.dumps(report)
        self.assertNotIn('secret', text)
        self.assertNotIn('hidden', text)
        self.assertNotIn('cookie', text)

    def test_unknown_verification_and_redaction(self):
        report = build_report({'results': {'a': {'savedFiles': ['https://host/path?sig=secret'],
                                                  'name': 'cookie=hidden'}}})
        self.assertIsNone(report['modules'][0]['sheetWriteVerified'])
        self.assertIsNone(report['modules'][0]['queryRows'])
        self.assertEqual(report['queryRowsUnknownModules'], ['a'])
        self.assertNotIn('secret', json.dumps(report))
        self.assertNotIn('hidden', json.dumps(report))

    def test_outputs_and_cli_preserve_input(self):
        with test_directory() as root:
            root = Path(root)
            state = root / 'state.json'
            state.write_text('{"completedModules": [], "results": {}}', encoding='utf-8')
            before = state.read_bytes()
            cli = Path(__file__).resolve().parents[1] / 'scripts' / 'asset_report.py'
            result = subprocess.run([sys.executable, str(cli), '--state', str(state),
                                     '--report', str(root / 'report.json')], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state.read_bytes(), before)
            self.assertTrue((root / 'report.md').exists())
            refused = subprocess.run([sys.executable, str(cli), '--state', str(state),
                                       '--report', str(state)], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(state.read_bytes(), before)

    def test_template_is_local_and_row_keys_preserve_distinct_rows(self):
        self.assertEqual(DEFAULT_TEMPLATE.parent.parent, Path(__file__).resolve().parents[1])
        self.assertTrue(DEFAULT_TEMPLATE.exists())
        self.assertEqual(keyrow(['123.0', 1.0], ['商品ID', '金额']), keyrow(['123', 1], ['商品ID', '金额']))
        self.assertNotEqual(keyrow(['123', 1], ['商品ID', '金额']), keyrow(['123', 2], ['商品ID', '金额']))


if __name__ == '__main__':
    unittest.main()
