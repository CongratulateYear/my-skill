"""Offline filesystem-only regression tests (no downloader imports/network)."""
import argparse
import io
import os
import importlib.util
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location("export", Path(__file__).with_name("taogongchang_export.py"))
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def publish(self, work, module):
        keys = list(export.MODULE_DISPLAY_NAMES) if module == 'all' else [module]
        outcomes = {}
        for key in keys:
            files = [p for p in work.rglob('*.xlsx')
                     if (export.display_for_path(p, work, None) == export.MODULE_DISPLAY_NAMES[key]
                         or len(keys) == 1)]
            outcomes[key] = {'status': 'completed' if files else 'zero',
                             'publishFiles': [str(p) for p in sorted(files)]}
        return export.finalize_folder(work, module, output_folder=self.root, outcomes=outcomes)

    def put(self, name, value="data"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_single_does_not_touch_other_modules_or_unknown_files(self):
        other = self.put("asset-inventory-product-loan-20260101-20260131/raw.xlsx", "loan")
        unknown = self.put("notes.xlsx", "private")
        state = self.put("resume.json", "state")
        self.put("处罚数据_2026-01.xlsx", "fine")
        export.finalize_folder(self.root, "punishment-data")
        self.assertEqual(other.read_text(), "loan")
        self.assertEqual(unknown.read_text(), "private")
        self.assertEqual(state.read_text(), "state")
        self.assertEqual((self.root / "处罚数据.xlsx").read_text(), "fine")

    def test_idempotent_existing_shards_not_counted_again(self):
        self.put("处罚数据.xlsx", "one")
        self.put("处罚数据_2.xlsx", "two")
        self.put(".previous-run/处罚数据_old.xlsx", "stale")
        for _ in range(2):
            result = export.finalize_folder(self.root, "punishment-data")
            self.assertEqual(len(result), 2)
        self.assertFalse((self.root / "处罚数据_3.xlsx").exists())

    def test_rerun_replaces_old_shards_and_preserves_other_module(self):
        self.put("处罚数据.xlsx", "old")
        self.put("处罚数据_2.xlsx", "old2")
        other = self.put("商品贷款.xlsx", "loan")
        work = self.root / ".work"
        self.put(".work/new.xlsx", "new")
        result = self.publish(work, "punishment-data")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].read_text(), "new")
        self.assertFalse((self.root / "处罚数据_2.xlsx").exists())
        self.assertEqual(other.read_text(), "loan")

    def test_empty_completed_run_removes_only_selected_stale_output(self):
        self.put("处罚数据.xlsx", "old")
        other = self.put("商品贷款.xlsx", "loan")
        work = self.root / ".work"
        work.mkdir()
        self.assertEqual(self.publish(work, "punishment-data"), [])
        self.assertFalse((self.root / "处罚数据.xlsx").exists())
        self.assertEqual(other.read_text(), "loan")

    def test_copy_failure_keeps_old_and_source(self):
        old = self.put("处罚数据.xlsx", "old")
        source = self.put(".work/new.xlsx", "new")
        with patch.object(export.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.publish(source.parent, "punishment-data")
        self.assertEqual(old.read_text(), "old")
        self.assertEqual(source.read_text(), "new")

    def test_publish_failure_rolls_back_all_modules(self):
        fine = self.put("处罚数据.xlsx", "old fine")
        loan = self.put("商品贷款.xlsx", "old loan")
        source = self.put(".work/asset-inventory-product-loan-20260101-20260131/new.xlsx", "new loan")
        self.put(".work/处罚数据_new.xlsx", "new fine")
        real_replace = export.os.replace
        count = 0
        def fail_once(src, dst):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("publish failed")
            return real_replace(src, dst)
        with patch.object(export.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.publish(self.root / ".work", "all")
        self.assertEqual(fine.read_text(), "old fine")
        self.assertEqual(loan.read_text(), "old loan")
        self.assertTrue(source.exists())

    def test_all_groups_shards_by_module(self):
        self.put("asset-inventory-product-loan-20260101-20260131/a.xlsx", "a")
        self.put("asset-inventory-product-loan-20260101-20260131/b.xlsx", "b")
        self.put("GEI@2026.xlsx", "effects")
        result = export.finalize_folder(self.root, "all")
        self.assertEqual({p.name for p in result}, {"商品贷款.xlsx", "商品贷款_2.xlsx", "所有商品效果数据导出.xlsx"})

    def test_cli_passthrough_without_network(self):
        argv = ["export", "--company-name", "公司", "--module", "罚款",
                "--order-pay-start", "2026-01-01", "--order-pay-end", "2026-01-31",
                "--batch-folder", str(self.root / "batch"), "--browser", "edge",
                "--transport", "bridge", "--bridge-url", "http://127.0.0.1:8899", "--dry-run"]
        with patch.object(export.sys, "argv", argv), patch.object(export.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(export.main(), 0)
        cmd = run.call_args.args[0]
        for option, value in [("--browser", "edge"), ("--transport", "bridge"),
                              ("--bridge-url", "http://127.0.0.1:8899"), ("--module", "punishment-data")]:
            self.assertEqual(cmd[cmd.index(option) + 1], value)
        self.assertIn("--dry-run", cmd)
        self.assertEqual(cmd[1], '-u')
        self.assertFalse((self.root / "batch").exists())

    def cli_args(self, *extra):
        return ['export', '--company-name', '公司', '--module', '罚款',
                '--order-pay-start', '2026-01-01', '--order-pay-end', '2026-01-31',
                '--output-root', str(self.root / 'output'), *extra]

    def test_main_publishes_only_excel_and_keeps_receipt_external(self):
        state_root = self.root / 'internal'
        receipt = state_root / 'receipt.json'
        def download(cmd):
            work = Path(cmd[cmd.index('--output-dir') + 1])
            self.assertEqual(work.parent, state_root)
            source = work / 'raw.xlsx'
            source.write_bytes(b'fixture')
            (work / 'fixture.asset-inventory-state.json').write_text(json.dumps({
                'completedModules': ['punishment-data'],
                'results': {'punishment-data': {'status': 'completed', 'queryRows': 7,
                    'dates': {'startDate':'2026-01-01'}, 'downloadUrl':'https://untrusted.invalid/signed-secret',
                    'publishFiles': [str(source)]}}
            }), encoding='utf-8')
            return argparse.Namespace(returncode=0)
        with patch.object(export.sys, 'argv', self.cli_args('--state-dir', str(state_root),
                         '--result-file', str(receipt), '--run-id', 'offline-run')), \
                patch.object(export.subprocess, 'run', side_effect=download):
            self.assertEqual(export.main(), 0)
        folder = self.root / 'output' / '公司_2026-01-01_2026-01-31'
        self.assertEqual([p.name for p in folder.iterdir()], ['处罚数据.xlsx'])
        proof=json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(proof['run_id'], 'offline-run')
        self.assertEqual(proof['results']['punishment-data'],{'status':'completed','queryRows':7,'dates':{'startDate':'2026-01-01'}})
        self.assertNotIn('signed-secret',receipt.read_text(encoding='utf-8'))
        self.assertFalse((state_root / '.work-punishment-data').exists())
        self.assertFalse(list(folder.parent.glob('.finalize-*')))

    def test_resume_uses_same_external_work_and_failure_preserves_it(self):
        state_root = self.root / 'internal'
        work = state_root / '.work-punishment-data'
        work.mkdir(parents=True)
        (work / 'marker').write_text('unchanged')
        with patch.object(export.sys, 'argv', self.cli_args('--state-dir', str(state_root), '--resume')), \
                patch.object(export.subprocess, 'run', return_value=argparse.Namespace(returncode=3)) as run:
            self.assertEqual(export.main(), 3)
        self.assertIn('--resume', run.call_args.args[0])
        self.assertEqual((work / 'marker').read_text(), 'unchanged')
        self.assertEqual(list((self.root/'output'/'公司_2026-01-01_2026-01-31').iterdir()), [])

    def test_legacy_work_blocks_network_instead_of_creating_another_task(self):
        folder = self.root / 'output' / '公司_2026-01-01_2026-01-31'
        legacy = folder / '.work-punishment-data'
        legacy.mkdir(parents=True)
        with patch.object(export.sys, 'argv', self.cli_args()), \
                patch.object(export.subprocess, 'run') as run, self.assertRaises(SystemExit):
            export.main()
        run.assert_not_called()
        self.assertTrue(legacy.is_dir())

    def test_rejects_internal_state_or_receipt_in_delivery_folder(self):
        folder = self.root / 'output' / '公司_2026-01-01_2026-01-31'
        for extra in [('--state-dir', str(folder/'state')), ('--result-file', str(folder/'receipt.json'))]:
            with patch.object(export.sys, 'argv', self.cli_args(*extra)), \
                    patch.object(export.subprocess, 'run') as run, self.assertRaises(SystemExit):
                export.main()
            run.assert_not_called()
        self.assertFalse(folder.exists())

    def test_receipt_inside_disposable_work_rejected_before_download(self):
        state_root = self.root / 'internal'
        receipt = state_root / '.work-punishment-data' / 'anything.json'
        with patch.object(export.sys, 'argv', self.cli_args('--state-dir', str(state_root),
                         '--result-file', str(receipt))), \
                patch.object(export.subprocess, 'run') as run, self.assertRaises(SystemExit):
            export.main()
        run.assert_not_called()
        self.assertFalse(state_root.exists())

    def test_batch_path_and_public_folder_name(self):
        args = argparse.Namespace(write_off_start="2026-01-01", write_off_end="2026-01-31",
                                  order_pay_start=None, order_pay_end=None, batch_folder=self.root / "batch")
        self.assertEqual(export.folder_name("公司/A", "2026-01-01", "2026-01-31"), "公司-A_2026-01-01_2026-01-31")
        self.assertEqual(export.build_folder(self.root / "unused", "公司/A", args),
                         self.root / "batch" / "公司-A_2026-01-01_2026-01-31")


class PublishedCleanupTests(unittest.TestCase):
    def setUp(self):
        # Keep every test write within this skill; never operate on real runtimes.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.retired = self.root / ('.published-all-' + 'a' * 32)
        self.retired.mkdir()

    def test_success_only_removes_retired_run(self):
        for name in ('journal.json', 'receipt.json', '.previous-all-backup/keep.xlsx',
                     'business/商品贷款.xlsx'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'keep')
        (self.retired / 'source.xlsx').write_bytes(b'data')
        (self.retired / 'run.asset-inventory-state.json').write_text('{}')
        export.cleanup_published(self.retired, self.root)
        self.assertFalse(self.retired.exists())
        for name in ('journal.json', 'receipt.json', '.previous-all-backup/keep.xlsx',
                     'business/商品贷款.xlsx'):
            self.assertEqual((self.root / name).read_bytes(), b'keep')

    def test_refuses_unknown_and_recovery_material_before_any_delete(self):
        for name in ('journal.json', 'receipt.json', 'backup.xlsx', 'unknown.txt'):
            with self.subTest(name=name):
                source = self.retired / 'source.xlsx'
                source.write_bytes(b'data')
                protected = self.retired / name
                protected.write_bytes(b'keep')
                with self.assertRaises(ValueError):
                    export.cleanup_published(self.retired, self.root)
                self.assertEqual(source.read_bytes(), b'data')
                self.assertEqual(protected.read_bytes(), b'keep')
                protected.unlink()

    def test_rejects_wrong_root_or_name(self):
        for path, root in ((self.retired, self.root/'elsewhere'),
                           (self.root, self.root.parent)):
            with self.assertRaises(ValueError):
                export.cleanup_published(path, root)
        self.assertTrue(self.retired.exists())

    def test_reparse_point_rejected_before_delete(self):
        real_lstat = export.os.lstat
        def reparse(path):
            result = real_lstat(path)
            if str(path).endswith('source.xlsx'):
                return argparse.Namespace(st_mode=result.st_mode, st_file_attributes=0x400)
            return result
        source = self.retired / 'source.xlsx'
        source.write_bytes(b'keep')
        with patch.object(export.os, 'lstat', side_effect=reparse):
            with self.assertRaises(ValueError):
                export.cleanup_published(self.retired, self.root)
        self.assertEqual(source.read_bytes(), b'keep')

    def test_failure_diagnostics_are_specific(self):
        exc = PermissionError(13, 'fixture locked', 'source.xlsx')
        exc.winerror = 32
        with patch.object(export.shutil, 'rmtree', side_effect=exc):
            with self.assertRaises(PermissionError) as caught:
                export.cleanup_published(self.retired, self.root)
        message = export.cleanup_warning(self.retired, caught.exception)
        for token in ('PermissionError', 'errno=13', 'winerror=32', 'source.xlsx', str(self.retired)):
            self.assertIn(token, message)
        self.assertTrue(self.retired.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows MAX_PATH regression')
    def test_real_windows_long_path_after_retirement(self):
        source = self.retired / ('asset-inventory-' + 'x' * 60) / ('extract-' + 'y' * 30) / ('账单' + 'z'*75 + '.xlsx')
        self.assertGreater(len(str(source)), 260)
        os.makedirs(export.filesystem_path(source.parent))
        with open(export.filesystem_path(source), 'wb') as stream:
            stream.write(b'fixture')
        self.assertTrue(os.path.isfile(export.filesystem_path(source)))
        export.cleanup_published(self.retired, self.root)
        self.assertFalse(self.retired.exists())

    def test_workbook_publication_cleanup_preserves_real_xlsx(self):
        from openpyxl import Workbook, load_workbook
        work = self.root / '.work-punishment-data'
        work.mkdir()
        source = work / 'raw.xlsx'
        workbook = Workbook()
        workbook.active.append(['fixture', 123])
        workbook.save(source)
        workbook.close()
        output = self.root / 'delivery'
        export.finalize_folder(work, 'punishment-data', output_folder=output,
                               outcomes={'punishment-data': {'status': 'completed',
                                                            'publishFiles': [str(source)]}})
        # This unused fixture must be empty before the test's work retirement.
        self.retired.rmdir()
        work.rename(self.retired)
        export.cleanup_published(self.retired, self.root)
        result = load_workbook(output / '处罚数据.xlsx', read_only=True)
        try:
            self.assertEqual(list(result.active.values), [('fixture', 123)])
        finally:
            result.close()

    def test_downloader_progress_also_defaults_flush(self):
        spec = importlib.util.spec_from_file_location('offline_downloader', export.DOWNLOAD_SCRIPT)
        downloader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(downloader)
        self.assertTrue(downloader.print.keywords['flush'])

    def test_progress_print_defaults_flush(self):
        class Stream(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
        stream = Stream()
        export.print('progress', file=stream)
        self.assertTrue(stream.flushed)


if __name__ == "__main__":
    unittest.main()
