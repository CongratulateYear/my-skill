"""CLI authorization checks only: reject before login/network/directory writes."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pansuan_pipeline as p


class RecoveryCliTests(unittest.TestCase):
    def rejected(self, extra, expected):
        with patch.object(sys, 'argv', ['pipeline', '--company-name', 'synthetic', *extra]), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as result:
                p.main()
        self.assertEqual(result.exception.code, 2)
        self.assertIn(expected, err.getvalue())

    def test_id_requires_resume(self):
        self.rejected(['--recover-hosting-task-id', '123'], '--recover-hosting-task-id 只能与 --resume')

    def test_legacy_requires_id_and_resume(self):
        for extra in ([], ['--resume']):
            self.rejected([*extra, '--recover-hosting-legacy-mtime'], '--recover-hosting-legacy-mtime 需要')
