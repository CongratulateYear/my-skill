"""Offline filesystem-only regression tests (no downloader imports/network)."""
import argparse
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("export", Path(__file__).with_name("taogongchang_export.py"))
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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
        self.assertFalse((self.root / "batch").exists())

    def test_batch_path_and_public_folder_name(self):
        args = argparse.Namespace(write_off_start="2026-01-01", write_off_end="2026-01-31",
                                  order_pay_start=None, order_pay_end=None, batch_folder=self.root / "batch")
        self.assertEqual(export.folder_name("公司/A", "2026-01-01", "2026-01-31"), "公司-A_2026-01-01_2026-01-31")
        self.assertEqual(export.build_folder(self.root / "unused", "公司/A", args),
                         self.root / "batch" / "公司-A_2026-01-01_2026-01-31")


if __name__ == "__main__":
    unittest.main()
