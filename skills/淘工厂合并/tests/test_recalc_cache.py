"""Offline synthetic cache-transfer regressions; never open business workbooks."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from lxml import etree as ET
from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recalc_cache


class CacheTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.xlsx'
        self.calculated = self.root / 'calculated.xlsx'
        self.output = self.root / 'output.xlsx'
        wb = Workbook()
        wb.active.title = 'Formula'
        wb.active['A1'] = 7
        wb.active['B1'] = '=A1+1'
        wb.create_sheet('Plain')['A1'] = 'literal'
        wb.save(self.source)
        wb.close()
        with ZipFile(self.source) as original, ZipFile(self.calculated, 'w') as calculated:
            for info in original.infolist():
                data = original.read(info.filename)
                if info.filename == 'xl/worksheets/sheet1.xml':
                    root = ET.fromstring(data)
                    root.find(".//s:c[@r='B1']/s:v", recalc_cache.NS).text = '8'
                    data = ET.tostring(root)
                calculated.writestr(info, data)

    def test_replacement_skips_original_read_only_during_write(self):
        source_reads, writes, copied_members = [], [], []
        state = {'writing': False}
        source_path, output_path = self.source, self.output

        class ObservedZipFile(ZipFile):
            def __init__(self, file, mode='r', *args, **kwargs):
                super().__init__(file, mode, *args, **kwargs)
                self.is_output = Path(self.filename) == output_path and mode == 'w'
                if self.is_output:
                    state['writing'] = True

            def read(self, name, *args, **kwargs):
                member = name.filename if hasattr(name, 'filename') else name
                if Path(self.filename) == source_path:
                    source_reads.append(member)
                    if state['writing']:
                        copied_members.append(member)
                return super().read(name, *args, **kwargs)

            def writestr(self, name, data, *args, **kwargs):
                if self.is_output:
                    writes.append(name.filename if hasattr(name, 'filename') else name)
                return super().writestr(name, data, *args, **kwargs)

            def close(self):
                if getattr(self, 'is_output', False):
                    state['writing'] = False
                super().close()

        before = self.source.read_bytes()
        with patch.object(recalc_cache.zipfile, 'ZipFile', ObservedZipFile):
            counts = recalc_cache.transplant_caches(self.source, self.calculated, self.output)
        formula_member = 'xl/worksheets/sheet1.xml'
        self.assertNotIn(formula_member, copied_members)
        self.assertIn('xl/worksheets/sheet2.xml', copied_members)
        # Initial parsing AND independent post-write validation must still read it.
        self.assertEqual(source_reads.count(formula_member), 2)
        self.assertEqual(counts['formula_caches'], 1)
        self.assertEqual(before, self.source.read_bytes())
        with ZipFile(self.source) as original, ZipFile(self.output) as final:
            self.assertEqual(writes, original.namelist())
            self.assertEqual(final.namelist(), original.namelist())
            for member in original.namelist():
                if member != formula_member:
                    self.assertEqual(original.read(member), final.read(member))
        wb = load_workbook(self.output, data_only=False)
        try:
            self.assertEqual(wb['Formula']['B1'].value, '=A1+1')
        finally:
            wb.close()
        wb = load_workbook(self.output, data_only=True)
        try:
            self.assertEqual(wb['Formula']['B1'].value, 8)
        finally:
            wb.close()

    def test_exact_formulas_do_not_require_tokenization(self):
        with patch.object(recalc_cache, 'canonical_formula', side_effect=AssertionError('unneeded tokenizer')):
            self.assertEqual(recalc_cache.transplant_caches(self.source, self.calculated, self.output)['formula_caches'], 1)

    def test_equivalent_rewrite_is_still_tokenized_and_preserves_source(self):
        with ZipFile(self.calculated) as zin:
            entries = [(info, zin.read(info.filename)) for info in zin.infolist()]
        with ZipFile(self.calculated, 'w') as zout:
            for info, data in entries:
                if info.filename == 'xl/worksheets/sheet1.xml':
                    root = ET.fromstring(data)
                    root.find(".//s:c[@r='B1']/s:f", recalc_cache.NS).text = 'A1:A1+1'
                    data = ET.tostring(root)
                zout.writestr(info, data)
        with patch.object(recalc_cache, 'canonical_formula', wraps=recalc_cache.canonical_formula) as normalize:
            recalc_cache.transplant_caches(self.source, self.calculated, self.output)
            self.assertEqual(normalize.call_count, 2)
        with ZipFile(self.output) as final:
            root = ET.fromstring(final.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(root.find(".//s:c[@r='B1']/s:f", recalc_cache.NS).text, 'A1+1')

    def test_changed_nonformula_input_still_rejected(self):
        wb = load_workbook(self.calculated)
        wb['Formula']['A1'] = 9
        wb.save(self.calculated)
        wb.close()
        with self.assertRaisesRegex(ValueError, 'Calculation input changed'):
            recalc_cache.transplant_caches(self.source, self.calculated, self.output)
        self.assertFalse(self.output.exists())

    def test_postwrite_audit_rejects_each_output_corruption(self):
        for mutation, message in (
            ('plain', 'Unchanged package entry differs'),
            ('nonformula', 'Nonformula cell changed'),
            ('formula', 'Original formula changed'),
            ('cache', 'Cache mismatch'),
        ):
            output_path = self.output
            class CorruptingZipFile(ZipFile):
                def writestr(self, name, data, *args, **kwargs):
                    member = name.filename if hasattr(name, 'filename') else name
                    if Path(self.filename) == output_path:
                        if mutation == 'plain' and member == 'xl/worksheets/sheet2.xml':
                            data = data.replace(b'literal', b'changed')
                        elif mutation != 'plain' and member == 'xl/worksheets/sheet1.xml':
                            root = ET.fromstring(data)
                            if mutation == 'nonformula':
                                root.find(".//s:c[@r='A1']/s:v", recalc_cache.NS).text = '999'
                            elif mutation == 'formula':
                                root.find(".//s:c[@r='B1']/s:f", recalc_cache.NS).text = 'A1+2'
                            else:
                                root.find(".//s:c[@r='B1']/s:v", recalc_cache.NS).text = '999'
                            data = ET.tostring(root)
                    return super().writestr(name, data, *args, **kwargs)
            with self.subTest(mutation=mutation), patch.object(recalc_cache.zipfile, 'ZipFile', CorruptingZipFile):
                with self.assertRaisesRegex(ValueError, message):
                    recalc_cache.transplant_caches(self.source, self.calculated, self.output)

    def test_missing_cache_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected empty cache'):
            recalc_cache.transplant_caches(self.source, self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_changed_formula_still_rejected(self):
        wb = load_workbook(self.calculated)
        wb['Formula']['B1'] = '=A1+2'
        wb.save(self.calculated)
        wb.close()
        with self.assertRaisesRegex(ValueError, 'Unapproved formula rewrite'):
            recalc_cache.transplant_caches(self.source, self.calculated, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
