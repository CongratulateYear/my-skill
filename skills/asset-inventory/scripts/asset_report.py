"""Credential-free execution reports from local module state and builder results.

No network requests or workbook writes. Unknown input fields are deliberately omitted.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

# URLs (including signed URLs) and credential assignments never enter report text.
_URL = re.compile(r"(?:https?://|//)[^\s<>\"']+", re.I)
_SECRET = re.compile(r"(?:password|passwd|cookie|token|authorization|x[-_]?xsrf|signature|credential|密码)\s*[:=]\s*[^\s;,]+", re.I)
_SECRET_KEY = re.compile(r"password|passwd|cookie|token|authorization|xsrf|signature|credential|signed.?url|file.?url|download.?url|密码", re.I)


def sanitize(value: Any) -> Any:
    """Defensive sanitization after whitelisting; credential keys are removed."""
    if isinstance(value, Mapping):
        return {str(k): sanitize(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return _SECRET.sub('[REDACTED]', _URL.sub('[URL REDACTED]', value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


def _pick(data: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data}


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def build_report(state: Mapping[str, Any], builder_result: Mapping[str, Any] | None = None,
                 validation: Mapping[str, Any] | None = None, *, shop: str | None = None,
                 workbook: str | Path | None = None) -> dict[str, Any]:
    """Return stable JSON-ready report; absent verification stays unknown (None).

    Accepts downloader state {completedModules, results} and builder merge() return.
    Builder import_stats is authoritative for written rows when two-phase imports ran.
    """
    builder = builder_result or {}
    stats = {s.get('target'): s for s in builder.get('import_stats', []) if isinstance(s, Mapping)}
    results = state.get('results', {})
    if not isinstance(results, Mapping):
        results = {}
    completed = state.get('completedModules', [])
    modules = []
    for key in dict.fromkeys(list(completed) + list(results)):
        result = results.get(key, {})
        if not isinstance(result, Mapping):
            continue
        target = result.get('targetSheet')
        stat = stats.get(target, {})
        row = _pick(result, ('name', 'dates', 'taskId', 'extractedFiles'))
        row.update({'module': key, 'completed': key in completed,
                    'queryRows': _number(result.get('queryRows', result.get('queryCount'))),
                    'targetSheet': target,
                    'sourceFiles': result.get('savedFiles', []),
                    'writtenRows': _number(stat.get('written_rows', result.get('writtenRows'))),
                    'sheetWriteVerified': builder.get('sheet_write_verified') if stat else result.get('sheetWriteVerified')})
        if stat:
            row['import'] = _pick(stat, ('source', 'read_rows', 'written_rows', 'duplicates', 'borders_verified'))
        modules.append(row)
    report = {'schemaVersion': 1, 'shop': shop if shop is not None else state.get('shop', state.get('shopName')),
              'workbook': str(workbook) if workbook is not None else builder.get('output', state.get('panOutput')),
              'dates': state.get('dates'), 'modules': modules,
              'completedModules': [m['module'] for m in modules if m['completed']],
              'builder': _pick(builder, ('output', 'imported_sheets', 'import_stats', 'product_count',
                                        'allocated_rows', 'unassigned_rows', 'sheet_write_verified',
                                        'summary_header_preserved', 'data_borders_verified', 'presentation_verified',
                                        'all_product_effects_preserved', 'formula_cache_recalculated')),
              'validation': _pick(validation or {}, ('ok', 'checks', 'failures', 'summary', 'productCount',
                                                    'unassignedRows', 'readOnly', 'errorType'))}
    # import_stats itself also follows a strict field whitelist.
    report['builder']['import_stats'] = [_pick(s, ('target', 'source', 'read_rows', 'written_rows',
                                                    'duplicates', 'borders_verified'))
                                          for s in builder.get('import_stats', []) if isinstance(s, Mapping)]
    report['queryRowsTotal'] = sum(m['queryRows'] or 0 for m in modules)
    report['queryRowsUnknownModules'] = [m['module'] for m in modules if m['queryRows'] is None]
    return sanitize(report)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render already structured data, escaping table delimiters and line breaks."""
    data = sanitize(report)
    def cell(value: Any) -> str:
        if value is None:
            return '未记录'
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        return str(value).replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')
    lines = ['# 资产盘算执行报告', '', f"店铺：{cell(data.get('shop'))}", '',
             f"盘算文件：{cell(data.get('workbook'))}", '',
             '| 流程 | 完成 | 查询数 | taskId | 源文件 | 目标 Sheet | 写入行数 | 写入验证 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for module in data.get('modules', []):
        lines.append('| ' + ' | '.join(cell(module.get(k)) for k in
                     ('module', 'completed', 'queryRows', 'taskId', 'sourceFiles', 'targetSheet', 'writtenRows', 'sheetWriteVerified')) + ' |')
    lines += ['', '## 构建结果', '', '```json', json.dumps(data.get('builder', {}), ensure_ascii=False, indent=2),
              '```', '', '## 独立验证', '', '```json', json.dumps(data.get('validation', {}), ensure_ascii=False, indent=2), '```', '']
    return '\n'.join(lines)


def write_report(report: Mapping[str, Any], json_path: str | Path,
                 markdown_path: str | Path | None = None) -> dict[str, str]:
    """Write separate .json/.md deliverables, returning their resolved paths."""
    output = Path(json_path).resolve()
    markdown = Path(markdown_path).resolve() if markdown_path else output.with_suffix('.md')
    if output.suffix.lower() != '.json' or markdown.suffix.lower() != '.md':
        raise ValueError('Report outputs must use .json and .md extensions')
    data = sanitize(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    markdown.write_text(render_markdown(data), encoding='utf-8')
    return {'json': str(output), 'markdown': str(markdown)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--builder-result', type=Path)
    parser.add_argument('--validation', type=Path)
    parser.add_argument('--shop')
    parser.add_argument('--workbook', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--markdown', type=Path)
    args = parser.parse_args()
    inputs = {p.resolve() for p in (args.state, args.builder_result, args.validation, args.workbook) if p}
    outputs = {args.report.resolve(), (args.markdown or args.report.with_suffix('.md')).resolve()}
    if inputs & outputs:
        parser.error('Report outputs must be separate from input files')
    def load(path: Path | None) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path else {}
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data
    report = build_report(load(args.state), load(args.builder_result), load(args.validation),
                          shop=args.shop, workbook=args.workbook)
    print(json.dumps(write_report(report, args.report, args.markdown), ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
