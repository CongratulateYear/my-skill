"""Offline advisory lock tests, including process death recovery."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/jst_match.py'
spec = importlib.util.spec_from_file_location('match_lock_tests', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MatchLockTest(unittest.TestCase):
    def test_stale_lock_is_reused_without_unlink(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / 'fine.xlsx.lock'
            lock.write_bytes(b'stale previous process')
            stream = m._lock_output(lock)
            m._unlock_output(stream)
            self.assertTrue(lock.is_file())
            again = m._lock_output(lock)
            m._unlock_output(again)

    def test_live_process_rejected_and_killed_process_releases(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / 'fine.xlsx.lock'
            code = ("import importlib.util,sys; from pathlib import Path; "
                    "s=importlib.util.spec_from_file_location('lock_child',sys.argv[1]); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                    "held=m._lock_output(Path(sys.argv[2])); "
                    "print('locked',flush=True); sys.stdin.read()")
            child = subprocess.Popen([sys.executable, '-c', code, str(SCRIPT), str(lock)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                with self.assertRaisesRegex(m.MatchError, 'Output locked'):
                    m._lock_output(lock)
                child.kill()
                child.communicate(timeout=10)
                stream = m._lock_output(lock)
                m._unlock_output(stream)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_symlink_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'target'
            target.write_bytes(b'unchanged')
            lock = Path(td) / 'fine.xlsx.lock'
            try:
                lock.symlink_to(target)
            except OSError:
                self.skipTest('Symlinks unavailable')
            with self.assertRaisesRegex(m.MatchError, 'Symlink'):
                m._lock_output(lock)
            self.assertEqual(target.read_bytes(), b'unchanged')


if __name__ == '__main__':
    unittest.main()
