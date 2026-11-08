"""Offline-only scope authorization; --human-approved records explicit human consent.

Reads an existing candidate journal, its .pending file and a safe failure result
(or its six-field approved=false template). Never calls export/session APIs,
changes a journal, publishes ERP files or approves without the explicit switch.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
from pathlib import Path

from jst_export import (ExportError, _atomic_create, _coverage_with_approval,
                        _load_state, _pending_path, _read_pending)
from jushuitan import load_scope_approval


def _read_json(path, limit=2 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ExportError("离线证据不是有界普通JSON文件", code="scope_approval_invalid")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError):
        raise ExportError("离线证据JSON不合法", code="scope_approval_invalid") from None


def generate_scope_approval(*, state_file, evidence_file, approval_file, human_approved=False):
    """Write only a new authorization JSON after rechecking all local evidence.

    Consent applies to the exact six-field template in evidence_file. Inputs are
    local trust anchors, not authenticated server evidence; run only on a quiescent
    task after a human has reviewed the template. No task/state repair is done.
    """
    if human_approved is not True:
        raise ExportError("必须明确提供人工批准开关", code="scope_approval_invalid")
    state_path = Path(state_file).expanduser().absolute()
    evidence_path = Path(evidence_file).expanduser().absolute()
    destination = Path(approval_file).expanduser().absolute()
    # Reject duplicate JSON keys even though the existing state loader is lenient.
    _read_json(state_path)
    state = _load_state(state_path)
    if (state["version"] != 2 or state["phase"] != "candidate" or
            len(state["candidate_ids"]) != 1 or not state.get("pending")):
        raise ExportError("仅接受已有绑定待验收文件的v2候选任务", code="scope_approval_invalid")
    pending_path = _pending_path(state_path)
    protected = [state_path, evidence_path, pending_path, Path(state["output"])]
    protected.extend(Path(state["output"]).with_suffix(suffix) for suffix in (".xlsx", ".zip"))
    if destination.is_symlink() or any(destination.resolve() == path.resolve() for path in protected):
        raise ExportError("授权输出不能覆盖证据/状态/待验收/业务输出", code="scope_approval_invalid")
    if destination.exists():
        raise ExportError("授权输出已存在，禁止覆盖", code="scope_approval_invalid")
    content = _read_pending(pending_path)
    digest = hashlib.sha256(content).hexdigest()
    expected_binding = dict(sha256=digest, task_id=state["candidate_ids"][0],
                            output=state["output"], requested_ids=state["requested_ids"])
    if state["pending"] != expected_binding:
        raise ExportError("待验收文件或绑定已改变", code="pending_binding_mismatch")
    try:
        _coverage_with_approval(content, copy.deepcopy(state), digest, None)
    except ExportError as error:
        if error.code != "scope_approval_required":
            raise
        template = error.details["approval_required"]
    else:
        raise ExportError("没有待批准范围例外", code="scope_approval_invalid")
    evidence = _read_json(evidence_path)
    if isinstance(evidence, dict) and "error_code" in evidence:
        if evidence["error_code"] != "scope_approval_required":
            raise ExportError("证据不是待人工范围批准结果", code="scope_approval_invalid")
        details = evidence.get("error_details")
        evidence = details.get("approval_required") if isinstance(details, dict) else None
    # Full equality binds task, SHA-256, original output, ordered requests and extras.
    if type(evidence) is not dict or evidence != template or evidence.get("approved") is not False:
        raise ExportError("人工批准证据与本任务六字段不一致", code="scope_approval_invalid")
    approval = dict(template, approved=True)
    _coverage_with_approval(content, copy.deepcopy(state), digest, approval)
    encoded = (json.dumps(approval, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Existing loader validates the actual bytes before exposing the destination.
    # Temporary directory is removed on every outcome; no journal/pending writes.
    with tempfile.TemporaryDirectory(prefix="jst-scope-approval-") as directory:
        check_path = Path(directory) / "approval.json"
        check_path.write_bytes(encoded)
        checked = load_scope_approval(check_path)
        if checked != approval:
            raise ExportError("现有授权loader核验失败", code="scope_approval_invalid")
        _coverage_with_approval(content, copy.deepcopy(state), digest, checked)
    _atomic_create(destination, encoded)
    generated = load_scope_approval(destination)
    _coverage_with_approval(content, copy.deepcopy(state), digest, generated)
    return generated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, help="现有内部任务state JSON；pending固定为该路径+.pending")
    parser.add_argument("--evidence-file", required=True, help="已有安全失败结果JSON或六字段approved=false模板")
    parser.add_argument("--approval-file", required=True, help="新授权JSON路径，绝不覆盖")
    parser.add_argument("--human-approved", action="store_true", help="仅在已获得明确人工批准后显式提供；工具不代为批准")
    args = parser.parse_args(argv)
    try:
        generate_scope_approval(state_file=args.state_file, evidence_file=args.evidence_file,
                                approval_file=args.approval_file, human_approved=args.human_approved)
    except (ExportError, OSError):
        # Do not echo arbitrary exception text, paths, evidence or credentials.
        print(json.dumps({"ok": False, "error_code": "scope_approval_generation_rejected"}))
        return 2
    print(json.dumps({"ok": True, "offline": True, "state_modified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
