"""Strictly serial, credential-free input binding over the fixed company pipeline.

The manifest is NOT a progress journal. Company journals and final summaries remain
execution authority. No browser control, automatic recovery, task creation or retries.
"""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
import sys

from company_run import locked, save
from tgc_runtime import runtime_for

# Deliberately allowlist CLI options: never forward a password, cookie or token.
VALUES = {'--mode', '--module', '--order-pay-start', '--order-pay-end',
          '--write-off-start', '--write-off-end', '--settlement-date', '--export-root',
          '--batch-folder', '--output-dir', '--login-wait', '--browser', '--transport',
          '--erp-transport', '--erp-browser', '--erp-expect-company', '--heartbeat-interval'}
FLAGS = {'--resume', '--dry-run', '--erp-proven-partner-consent'}
RECOVERY = {'--recover-hosting-task-id', '--erp-scope-approval-file'}


def parse(argv):
    companies, options = [], []
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag == '--companies':
            i += 1
            while i < len(argv) and not argv[i].startswith('--'):
                companies.append(argv[i]); i += 1
            continue
        if flag in VALUES | RECOVERY:
            if i + 1 >= len(argv):
                raise ValueError('missing option value')
            options += [flag, argv[i + 1]]; i += 2
        elif flag in FLAGS or flag == '--recover-hosting-legacy-mtime':
            options.append(flag); i += 1
        else:
            raise ValueError('unsupported serial option; credentials and fetch input are not accepted')
    if not companies or len(set(companies)) != len(companies):
        raise ValueError('ordered companies must be nonempty and unique')
    if len([x for x in options if x == '--batch-folder']) != 1:
        raise ValueError('serial entry requires one fixed --batch-folder')
    # Reject duplicate options rather than binding one value while executing another.
    keys = [x for x in options if x.startswith('--')]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate serial option')
    import re
    safe_names = [re.sub(r'[\\/:*?"<>|\s]+', '-', name.strip()) for name in companies]
    if len(set(safe_names)) != len(safe_names):
        raise ValueError('company folder names collide')
    return companies, options


def binding(companies, options):
    values = {}
    for flag in VALUES:
        if flag in options:
            values[flag] = options[options.index(flag) + 1]
    for flag in ('--batch-folder', '--export-root', '--output-dir'):
        if flag in values:
            values[flag] = str(Path(values[flag]).expanduser().resolve())
    for flag, default in (('--mode', 'full'), ('--module', 'all')):
        values.setdefault(flag, default)
    # Operational transport/wait options may change during connection repair.
    immutable = {k: v for k, v in values.items() if k not in {
        '--login-wait', '--heartbeat-interval', '--browser', '--transport',
        '--erp-browser', '--erp-transport'}}
    immutable['--erp-proven-partner-consent'] = '--erp-proven-partner-consent' in options
    return {'version': 1, 'companies': companies, 'inputs': immutable}


def company_paths(company, bound):
    import re
    inputs = bound['inputs']
    name = re.sub(r'[\\/:*?"<>|\s]+', '-', company.strip())
    folder = Path(inputs['--batch-folder']) / (name + '_' + inputs['--write-off-start'] + '_' + inputs['--write-off-end'])
    return folder, runtime_for(folder) / '.company-run.json'


def is_completed(company, bound):
    # Only scopes explicit recovery arguments; never a completion proof. The
    # fixed pipeline revalidates exact identity and all hashes before returning.
    _, path = company_paths(company, bound)
    return path.exists() and json.loads(path.read_text(encoding='utf-8')).get('stage') == 'complete'


def execute(argv, company_main):
    companies, options = parse(argv)
    bound = binding(companies, options)
    # Let the fixed entry validate every company's names/dates/dependencies first;
    # dry-run has no network, credentials, journals or directory writes.
    clean = [x for x in options if x not in ('--resume',)]
    for flag in RECOVERY:
        if flag in clean:
            at = clean.index(flag); del clean[at:at + 2]
    clean = [x for x in clean if x != '--recover-hosting-legacy-mtime']
    if '--dry-run' not in clean:
        clean.append('--dry-run')
    for company in companies:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = company_main(['--company-name', company, *clean])
        if rc:
            return rc
    if '--dry-run' in options:
        print(json.dumps({'dry_run': True, 'company_count': len(companies), 'serial': True}))
        return 0
    root = runtime_for(Path(bound['inputs']['--batch-folder']))
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / 'serial-inputs.json'
    with locked(root / '.serial-batch.lock'):
        if manifest.exists():
            if '--resume' not in options or json.loads(manifest.read_text(encoding='utf-8')) != bound:
                raise ValueError('serial resume requires identical ordered companies and inputs')
        elif '--resume' in options:
            raise ValueError('no serial input binding; do not adopt an old batch')
        else:
            batch = Path(bound['inputs']['--batch-folder'])
            if batch.exists() and any(batch.iterdir()):
                raise ValueError('new serial run requires an empty batch')
            if any(company_paths(company, bound)[1].exists() for company in companies):
                raise ValueError('new serial run cannot adopt an existing company journal')
            save(manifest, bound)
        results = []
        recovery_used = False
        for company in companies:
            completed = is_completed(company, bound)
            current = list(options)
            # Explicit recovery is scoped only to the first unresolved company.
            if completed or recovery_used:
                for flag in RECOVERY:
                    if flag in current:
                        at = current.index(flag); del current[at:at + 2]
                current = [x for x in current if x != '--recover-hosting-legacy-mtime']
            capture = io.StringIO()
            try:
                with contextlib.redirect_stdout(capture):
                    rc = company_main(['--company-name', company, *current])
            except (KeyboardInterrupt, SystemExit):
                rc = 2
            if not completed:
                recovery_used = True
            summary = None
            for line in capture.getvalue().splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get('summary'), dict):
                    summary = record['summary']
            if summary is None:
                summary = {'company': company, 'execution_status': 'failed',
                           'conclusion': '流程未完成，需要处理后续跑', 'resume_required': True}
            results.append(summary)
            if rc or summary.get('execution_status') != 'completed':
                print(json.dumps({'summary': {'execution_status': summary['execution_status'],
                    'companies': results, 'current_company': company,
                    'remaining_companies': companies[len(results):], 'recovery': summary.get('recovery')}}, ensure_ascii=False))
                return rc or 2
        print(json.dumps({'summary': {'execution_status': 'completed', 'companies': results}}, ensure_ascii=False))
        return 0
