"""Synthetic, offline tests. No credentials or live business data are used."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import jst_export
import jst_scope_approval as scope
from jushuitan import load_scope_approval


def workbook(ids):
    book = Workbook()
    sheet = book.active
    sheet.append(["原始线上订单号"])
    for order in ids:
        sheet.append([order])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


class ScopeApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jst-scope-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "task.state.json"
        self.evidence_path = self.root / "safe-result.json"
        self.approval_path = self.root / "授权.json"
        self.pending_path = jst_export._pending_path(self.state_path)
        self.content = workbook(["102", "101", "201"])
        self.pending_path.write_bytes(self.content)
        self.state = dict(version=2, phase="candidate", requested_ids=["102", "101"],
                          found_ids=["102", "101"], missing_ids=[], internal_ids=["900"],
                          baseline_ids=["40"], candidate_ids=["41"], export_type="orders",
                          output=str(self.root / "ERP原始导出"), download_attempted=True,
                          merge_evidence={"900": ["101", "102", "201"]},
                          internal_order_ids={"900": ["101", "102", "201"]})
        self.state["pending"] = dict(sha256=hashlib.sha256(self.content).hexdigest(),
                                     task_id="41", output=self.state["output"],
                                     requested_ids=["102", "101"])
        self.template = dict(self.state["pending"], approved=False, extra_ids=["201"])
        self.evidence = dict(ok=False, error_code="scope_approval_required",
                             error_details=dict(approval_required=self.template))
        self.save_inputs()
        # Prove this entrypoint never uses transport/export creation, even on success.
        for target in ("socket.create_connection", "socket.socket.connect",
                       "jst_export.run_export", "jst_export._accept_pending",
                       "jst_export.create_export", "jst_export.download_export",
                       "jst_export.poll_export", "jst_export._save_state"):
            guard = patch(target, side_effect=AssertionError("network/business/state write forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def save_inputs(self):
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        self.evidence_path.write_text(json.dumps(self.evidence, ensure_ascii=False), encoding="utf-8")

    def generate(self, **kwargs):
        return scope.generate_scope_approval(state_file=self.state_path,
                                             evidence_file=self.evidence_path,
                                             approval_file=self.approval_path,
                                             human_approved=kwargs.get("human_approved", True))

    def reject(self, **kwargs):
        before = {p: p.read_bytes() for p in (self.state_path, self.pending_path, self.evidence_path)}
        with self.assertRaises(jst_export.ExportError):
            self.generate(**kwargs)
        self.assertFalse(self.approval_path.exists())
        for path, data in before.items():
            self.assertEqual(data, path.read_bytes())

    def test_no_explicit_human_approval(self):
        for flag in (False, None, "true", 1):
            with self.subTest(flag=flag):
                self.reject(human_approved=flag)

    def test_success_no_bom_loader_compatible_inputs_unchanged(self):
        before = {p: p.read_bytes() for p in (self.state_path, self.pending_path, self.evidence_path)}
        result = self.generate()
        self.assertEqual(result, dict(self.template, approved=True))
        self.assertEqual(result, load_scope_approval(self.approval_path))
        raw = self.approval_path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(json.loads(raw.decode("utf-8")), result)
        self.assertEqual(set(result), {"approved", "task_id", "sha256", "output", "requested_ids", "extra_ids"})
        self.assertEqual(result["requested_ids"], ["102", "101"])
        for path, data in before.items():
            self.assertEqual(data, path.read_bytes())
        self.assertFalse(Path(self.state["output"]).exists())
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {self.state_path.name, self.pending_path.name, self.evidence_path.name, self.approval_path.name})

    def test_direct_six_field_template_accepted(self):
        self.evidence = self.template
        self.save_inputs()
        self.assertEqual(self.generate(), dict(self.template, approved=True))

    def test_evidence_six_field_bindings_strict(self):
        mutations = {"approved": True, "task_id": "42", "sha256": "0" * 64,
                     "output": str(self.root / "other"), "requested_ids": ["101", "102"],
                     "extra_ids": ["202"], "unknown": "no"}
        for key, value in mutations.items():
            with self.subTest(field=key):
                self.evidence = dict(self.template, **{key: value})
                self.save_inputs()
                self.reject()

    def test_wrong_result_code_or_missing_template(self):
        for value in ({"error_code": "scope_not_eligible", "error_details": {"approval_required": self.template}},
                      {"error_code": "scope_approval_required", "error_details": {}}, [], None):
            with self.subTest(evidence=value):
                self.evidence = value
                self.save_inputs()
                self.reject()

    def test_pending_hash_mismatch(self):
        self.pending_path.write_bytes(workbook(["102", "101", "202"]))
        self.reject()

    def test_state_pending_bindings_strict(self):
        original = copy.deepcopy(self.state)
        for key, value in {"task_id": "42", "sha256": "0" * 64,
                           "output": str(self.root / "other"), "requested_ids": ["101", "102"]}.items():
            with self.subTest(field=key):
                self.state = copy.deepcopy(original)
                self.state["pending"][key] = value
                self.save_inputs()
                self.reject()

    def test_missing_merge_evidence_or_mismatched_constituents(self):
        original = copy.deepcopy(self.state)
        for mapping in ({}, {"900": ["101", "102"]}, {"901": ["101", "102", "201"]}):
            with self.subTest(mapping=mapping):
                self.state = copy.deepcopy(original)
                self.state["merge_evidence"] = mapping
                self.save_inputs()
                self.reject()

    def test_missing_requested_order_and_unproven_extra(self):
        for ids in (["101", "201"], ["101", "102", "201", "999"]):
            with self.subTest(ids=ids):
                content = workbook(ids)
                self.pending_path.write_bytes(content)
                self.state["pending"]["sha256"] = hashlib.sha256(content).hexdigest()
                self.save_inputs()
                self.reject()

    def test_no_extra_or_invalid_workbook(self):
        for content in (workbook(["101", "102"]), b"not an Excel archive"):
            with self.subTest(valid_excel=content[:2] == b"PK"):
                self.pending_path.write_bytes(content)
                self.state["pending"]["sha256"] = hashlib.sha256(content).hexdigest()
                self.save_inputs()
                self.reject()

    def test_wrong_phase_or_legacy_state_rejected(self):
        original = copy.deepcopy(self.state)
        for field, value in (("phase", "complete"), ("phase", "prepared"), ("version", 1)):
            with self.subTest(field=field, value=value):
                self.state = dict(original, **{field: value})
                self.save_inputs()
                self.reject()

    def test_duplicate_evidence_keys_rejected(self):
        raw = json.dumps(self.template)
        self.evidence_path.write_text(raw[:-1] + ', "task_id": "41"}', encoding="utf-8")
        self.reject()

    def test_missing_pending_rejected(self):
        self.pending_path.unlink()
        before = self.state_path.read_bytes()
        with self.assertRaises(jst_export.ExportError):
            self.generate()
        self.assertFalse(self.approval_path.exists())
        self.assertEqual(before, self.state_path.read_bytes())

    def test_existing_output_not_overwritten(self):
        self.approval_path.write_bytes(b"keep this file")
        with self.assertRaises(jst_export.ExportError):
            self.generate()
        self.assertEqual(self.approval_path.read_bytes(), b"keep this file")

    def test_output_cannot_be_state_pending_evidence_or_business_path(self):
        original = self.approval_path
        for path in (self.state_path, self.pending_path, self.evidence_path,
                     Path(self.state["output"]), Path(self.state["output"]).with_suffix(".xlsx")):
            with self.subTest(path=path.name):
                self.approval_path = path
                before = path.read_bytes() if path.exists() else None
                with self.assertRaises(jst_export.ExportError):
                    self.generate()
                self.assertEqual(before, path.read_bytes() if path.exists() else None)
        self.approval_path = original

    def test_existing_hardlink_alias_not_overwritten(self):
        before = self.state_path.read_bytes()
        os.link(self.state_path, self.approval_path)
        with self.assertRaises(jst_export.ExportError):
            self.generate()
        self.assertEqual(before, self.state_path.read_bytes())
        self.assertEqual(before, self.approval_path.read_bytes())

    def test_symlink_destination_rejected(self):
        # Mock filesystem capability, not validation: Windows symlink privilege
        # is not assumed. Existing and dangling aliases must both fail closed.
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == self.approval_path):
            self.reject()

    def test_cli_missing_approval_rejects_and_success_is_offline(self):
        argv = ["--state-file", str(self.state_path), "--evidence-file", str(self.evidence_path),
                "--approval-file", str(self.approval_path)]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(scope.main(argv), 2)
            self.assertEqual(json.loads(output.getvalue())["ok"], False)
        self.assertFalse(self.approval_path.exists())
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(scope.main(argv + ["--human-approved"]), 0)
            self.assertEqual(json.loads(output.getvalue()),
                             {"ok": True, "offline": True, "state_modified": False})


if __name__ == "__main__":
    unittest.main()
