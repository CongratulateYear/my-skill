#!/usr/bin/env python3
"""Evidence-backed binding of an uncertain hosting create; never creates/downloads."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tmall_bill_download as bill

MODULE = "all-item-effects-export"


def recover(args: argparse.Namespace, *, lock_ownership=None) -> dict:
    from company_lock import company_lock_for, locked
    lock_path = company_lock_for(args.state_file, getattr(args, 'company_lock', None))
    if lock_ownership is not None:
        lock_ownership.require(lock_path)
        return _recover_locked(args)
    with locked(lock_path):
        return _recover_locked(args)


def _recover_locked(args: argparse.Namespace) -> dict:
    path = args.state_file.expanduser().resolve()
    original = path.read_bytes()
    stat = path.stat()
    digest = hashlib.sha256(original).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_state_sha256) or digest != args.expected_state_sha256:
        raise ValueError("state SHA256 mismatch")
    state = json.loads(original)
    if state.get("expectedCompany") != args.expect_company or state.get("shopName") != args.expect_company:
        raise ValueError("state company mismatch")
    entry = state.get("remoteTasks", {}).get(MODULE)
    if not isinstance(entry, dict) or entry.get("phase") != "creating" or "taskId" in entry:
        raise ValueError("requires uncertain hosting creating entry without taskId")
    if MODULE in state.get("completedModules", []):
        raise ValueError("hosting module already completed")
    dates = entry.get("dates")
    if not isinstance(dates, dict) or set(dates) != {"orderPayTime"}:
        raise ValueError("invalid hosting dates")
    pair = dates["orderPayTime"]
    if not isinstance(pair, dict) or set(pair) != {"start", "end"}:
        raise ValueError("invalid hosting date pair")
    for value in pair.values():
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("invalid hosting date")
        datetime.strptime(value, "%Y-%m-%d")
    if pair["start"] > pair["end"]:
        raise ValueError("reversed hosting dates")
    attempt = entry.get('attemptStartedAt')
    if attempt is not None:
        if (not isinstance(attempt, str) or
                entry.get('requestIdentitySha256') != bill.request_identity_digest(MODULE, dates, entry.get('queryRows'))):
            raise ValueError('request identity digest mismatch')
        moment = datetime.fromisoformat(attempt)
        if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
            raise ValueError('attemptStartedAt must be UTC')
        reference_time = moment.timestamp()
        time_basis = 'attemptStartedAt'
    elif getattr(args, 'allow_legacy_mtime', False) is True:
        reference_time = stat.st_mtime
        time_basis = 'legacy-state-mtime-explicit-optin'
    else:
        raise ValueError('legacy state lacks attemptStartedAt; explicit --allow-legacy-mtime required')
    expected_name = f"投流推广_推广佣金_推广明细_{pair['start'].replace('-', '')}至{pair['end'].replace('-', '')}"
    task_id = bill.validate_hosting_task_id(args.task_id)
    headers, session = bill.load_browser_headers(args.transport, None, args.browser)
    bill.validate_company(args.expect_company, headers, session)
    _, response = bill.post(session, f"{bill.HOSTING_BASE}/task/pageListDownloadTask",
                            {**headers, "referer": bill.HOSTING_REFERER}, {"pageNo": 1, "pageSize": 10})
    records = bill.hosting_task_records(response)
    matches = [record for record in records if bill.hosting_task_id(record) == task_id]
    if len(matches) != 1:
        raise ValueError("task ID missing or non-unique in first 10 records")
    candidates = []
    for record in records:
        if record.get("taskName") != expected_name:
            continue
        created = record.get("createTime")
        if type(created) is not int or created <= 0:
            raise ValueError("matching task has invalid createTime")
        if abs(created / 1000 - reference_time) <= 120:
            candidates.append(record)
    if len(candidates) != 1 or candidates[0] is not matches[0]:
        raise ValueError("task name/time evidence missing or ambiguous")
    record = matches[0]
    if bill.hosting_status(record) != "FINISHED":
        raise ValueError("verified recovery currently requires FINISHED task")
    evidence = {"taskId": task_id, "taskName": expected_name, "createTime": record["createTime"],
                "taskStatus": "FINISHED", "stateSha256": digest,
                "timeBasis": time_basis, "attemptStartedAt": attempt,
                "requestIdentitySha256": entry.get('requestIdentitySha256'),
                "stateMtimeNs": stat.st_mtime_ns, "verifiedAt": datetime.now(timezone.utc).isoformat()}
    result = {"module": MODULE, "bound": False, "evidence": evidence}
    if args.bind:
        # Check the entire bytes AND mtime again after remote verification.
        if path.read_bytes() != original or path.stat().st_mtime_ns != stat.st_mtime_ns:
            raise ValueError("state changed during verification")
        backup = path.with_name(path.name + "." + uuid.uuid4().hex + ".before-hosting-recovery")
        with backup.open("xb") as stream:
            stream.write(original)
            stream.flush()
            bill.os.fsync(stream.fileno())
        if path.read_bytes() != original or path.stat().st_mtime_ns != stat.st_mtime_ns:
            raise ValueError("state changed before binding")
        entry.update(phase="created", taskId=task_id, recoveryEvidence=evidence)
        bill.save_state(path, state)
        result.update(bound=True, backup=str(backup))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--expect-company", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-state-sha256", required=True)
    parser.add_argument("--company-lock", type=Path, help="must match the known parent runtime lock")
    parser.add_argument("--allow-legacy-mtime", action="store_true", help="explicit weaker evidence opt-in for pre-attempt states")
    parser.add_argument("--bind", action="store_true", help="persist verified binding; default read-only")
    parser.add_argument("--transport", default="auto", choices=["auto", "cookie", "playwright", "bridge"])
    parser.add_argument("--browser", default="auto")
    print(json.dumps(recover(parser.parse_args()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
