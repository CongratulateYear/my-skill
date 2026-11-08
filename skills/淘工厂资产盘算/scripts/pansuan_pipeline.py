#!/usr/bin/env python3
"""淘工厂资产盘算：单公司登录 → 导出 → 条件ERP → 一次严格合并 → 校验。

公司级日志复用本次发布凭证；ERP失败后续跑不重导已完成账单。
多公司仍由上层严格串行。凭据仅内存，ERP身份约束独立提供。
退出码0=流水线完成（仍需查看ERP部分缺单报告），3=登录需干预，其他=失败。
"""
from __future__ import annotations

import json
import re
from datetime import date
import subprocess
import sys
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[2]


def resolve_skill_script(chinese: str, english: str, script: str) -> Path:
    roots = (SKILLS_ROOT, Path.home() / ".agents" / "skills", Path.home() / ".dsh" / "skills")
    for root in roots:
        for name in (chinese, english):
            candidate = root / name / "scripts" / script
            if candidate.is_file():
                return candidate
    return SKILLS_ROOT / chinese / "scripts" / script


DEPENDENCIES = [
    ("淘工厂登录", resolve_skill_script("淘工厂登录", "taogongchang-login", "ensure_login.py")),
    ("淘工厂导出", resolve_skill_script("淘工厂导出", "taogongchang-export", "taogongchang_export.py")),
    ("淘工厂合并", resolve_skill_script("淘工厂合并", "taogongchang-merge", "merge_excel.py")),
]
EXPORT_ROOT_DEFAULT = Path("~/Downloads/淘工厂导出").expanduser()
ALL_MODULE_WORDS = {"all", "全部", ""}


def check_dependencies() -> None:
    missing = [
        f"  {name}：{script}（应位于 {script.parent.parent}）"
        for name, script in DEPENDENCIES
        if not script.is_file()
    ]
    if missing:
        raise SystemExit(
            "缺少依赖 skill，本 skill 无法运行：\n" + "\n".join(missing)
            + "\n「淘工厂资产盘算」与 淘工厂登录/淘工厂导出/淘工厂合并 是一套，"
            "安装本 skill 时必须同时安装这三个依赖 skill，请补装后重跑。"
        )


def startup_preflight(company_folder, output_dir, *, merge_required):
    """One local preflight before journals/login/export; never changes ACLs.

    Probe existing destination ancestors with automatically removed temporary
    files, not business directories. This is not a guarantee against later races.
    Dry-run deliberately never calls this function.
    """
    import os
    import tempfile
    from openpyxl import load_workbook
    from company_run import load_module
    from tgc_runtime import runtime_for

    erp = resolve_skill_script('淘工厂聚水潭', 'taogongchang-jushuitan', 'jushuitan.py')
    required = [erp, erp.with_name('jst_export.py'), erp.with_name('jst_scope_approval.py')]
    if merge_required:
        merge = load_module(DEPENDENCIES[2][1], 'tgc_preflight_merge')
        required += [merge.PAN_SUAN_BUILDER, merge.PAN_SUAN_TEMPLATE,
                     merge.PAN_SUAN_BUILDER.with_name('template_compat.py')]
        import delivery_enrichment as delivery
        required += [Path(os.environ.get('TGC_RECALC_NODE') or delivery.NODE),
                     Path(os.environ.get('TGC_RECALC_CLI') or delivery.CLI)]
        helpers = [SKILLS_ROOT / name / 'scripts' / 'recalc_cache.py'
                   for name in ('淘工厂合并', 'taogongchang-merge')]
        if not any(path.is_file() for path in helpers):
            raise ValueError('启动预检：缺少公式缓存工具')
    if any(not Path(path).is_file() for path in required):
        raise ValueError('启动预检：依赖或模板缺失')
    if merge_required:
        workbook = load_workbook(merge.PAN_SUAN_TEMPLATE, read_only=True)
        try:
            if not {'公司汇总', '所有商品效果数据导出'} <= set(workbook.sheetnames):
                raise ValueError('启动预检：模板结构无效')
        finally:
            workbook.close()
    checked = set()
    for destination in (Path(company_folder), Path(output_dir), runtime_for(company_folder)):
        ancestor = destination.expanduser().resolve()
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ValueError('启动预检：目标祖先不是目录')
        if ancestor not in checked:
            with tempfile.TemporaryFile(prefix='.tgc-preflight-', dir=ancestor) as probe:
                probe.write(b'preflight')
                probe.flush()
            checked.add(ancestor)


def _safe_path(value, basename=None):
    from pathlib import PureWindowsPath, PurePosixPath
    if not isinstance(value, str) or '://' in value or any(ord(ch) < 32 for ch in value):
        return False
    path = PureWindowsPath(value) if PureWindowsPath(value).is_absolute() else PurePosixPath(value)
    return path.is_absolute() and (basename is None or path.name == basename)


def _safe_code(value):
    return (isinstance(value, str) and bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,79}', value))
            and not re.search(r'(?i)secret|token|cookie|password|authorization', value))


def _safe_business_text(value):
    return (isinstance(value, str) and len(value) <= 200
            and bool(re.fullmatch(r'[\w .()*·-]+', value))
            and not re.search(r'(?i)secret|token|cookie|password|authorization', value))


def safe_recovery(value):
    if not isinstance(value, dict):
        return None
    result = {}
    constants = {
        'type': {'inspect-own-magpie-tab'}, 'executor': {'agent-with-magpie-browser'},
        'browser': {'current-existing-browser'}, 'tab': {'new-own-magpie-tab'},
        'bridgeState': {'not_running','agent_missing','unverified'},
        'loginState': {'login-or-permission-required','unverified'},
        'prefilledCredentials': {'preserve-and-submit'},
        'credentialPrompt': {'only-after-form-inspection-if-missing'},
        'successCriterion': {'server-order-query-and-requested-company-validated'},
    }
    for key, allowed in constants.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    if _safe_code(value.get('reason')):
        result['reason'] = value['reason']
    if value.get('expectCompany') is None or _safe_business_text(value.get('expectCompany')):
        result['expectCompany'] = value.get('expectCompany')
    argv = value.get('bridgeStartArgv')
    if (isinstance(argv, list) and len(argv) == 4 and argv[0] == 'python'
            and _safe_path(argv[1], 'browser_bridge.py') and argv[2] == '--port'
            and isinstance(argv[3], str) and argv[3].isdigit() and 1 <= int(argv[3]) <= 65535):
        result['bridgeStartArgv'] = argv
    if _safe_path(value.get('bridgeAgentSource'), 'bridge_agent.js'):
        result['bridgeAgentSource'] = value['bridgeAgentSource']
    policy = value.get('resumePolicy')
    if isinstance(policy, dict):
        result['resumePolicy'] = {key: True for key in ('sameInputs','sameOutputDirectory',
            'preserveTaskState','addResumeAfterExportAttempt','neverRecreateUncertainTask') if policy.get(key) is True}
    for key, expected in (('workbenchUrl','https://www.erp321.com/epaas'),
                          ('loginUrl','https://www.erp321.com/login.aspx')):
        if value.get(key) == expected:
            result[key] = expected
    if value.get('pythonControlsBrowser') is False:
        result['pythonControlsBrowser'] = False
    known_steps = (
        'Agent starts bridgeStartArgv as a tracked background job if bridge is not running; Python never injects Magpie.',
        'Open workbench in a new own Magpie tab; do not touch user/other-agent tabs.',
        'If already signed in, attach bridge and verify server session; do not log out.',
        'If login is needed, inspect the real form refs and only credential presence, never password values.',
        'Preserve prefilled credentials; if both fields are populated, click the real login submit without asking for credentials.',
        'Only fill missing fields using ERP credentials explicitly supplied for this attempt; never use Taogongchang credentials.',
        'Ask once for missing ERP credentials only after inspecting the form; do not extract or persist autofill/passwords.',
        'Stop for human handling of SMS, captcha, slider or risk checks; do not bypass.',
        'After submitting, attach bridge in the same own ERP tab and rerun server validation with original company constraint.',
    )
    if isinstance(value.get('steps'), list):
        result['steps'] = [step for step in value['steps'] if isinstance(step, str) and step in known_steps]
    human = value.get('humanRequired')
    if isinstance(human, list):
        result['humanRequired'] = [item for item in human if item in ('sms','captcha','slider','risk-check')]
    return result


def safe_diagnostic(state: dict) -> dict:
    """Forward validated recovery fields; never unknown/network fields."""
    result = {key: state[key] for key in ('ok', 'loggedIn') if isinstance(state.get(key), bool)}
    for key in ('reason', 'error_code', 'session_status'):
        if _safe_code(state.get(key)):
            result[key] = state[key]
    for key in ('company', 'companyName', 'expectCompany', 'accountHint'):
        if _safe_business_text(state.get(key)):
            result[key] = state[key]
    for key in ('action', 'recovery_action', 'nextAction'):
        value = state.get(key)
        if isinstance(value, dict):
            result[key] = safe_recovery(value)
        elif _safe_code(value):
            result[key] = value

    recovery = state.get('export_recovery')
    if isinstance(recovery, dict):
        filtered_recovery = {}
        if recovery.get('action') in ('explicit-scope-approval', 'inspect-and-resume'):
            filtered_recovery['action'] = recovery['action']
        if isinstance(recovery.get('requiredResume'), bool):
            filtered_recovery['requiredResume'] = recovery['requiredResume']
        result['export_recovery'] = filtered_recovery
    details = state.get('error_details')
    if isinstance(details, dict):
        clean = {}
        for key in ('extra_ids', 'missing_ids', 'requested_ids', 'actual_extra_ids', 'actual-extra', 'missing'):
            value = details.get(key)
            if isinstance(value, list) and all(isinstance(item, str) and re.fullmatch(r'[0-9]+', item) for item in value):
                clean[key] = value
        for key, pattern in (('task_id', r'[0-9]+'), ('sha256', r'[a-f0-9]{64}')):
            if isinstance(details.get(key), str) and re.fullmatch(pattern, details[key]):
                clean[key] = details[key]
        if _safe_path(details.get('output')):
            clean['output'] = details['output']
        if isinstance(details.get('scope_approval_eligible'), bool):
            clean['scope_approval_eligible'] = details['scope_approval_eligible']
        approval = details.get('approval_required')
        if isinstance(approval, dict):
            filtered = {}
            for key in ('approved', 'task_id', 'sha256', 'output', 'requested_ids', 'extra_ids'):
                value = approval.get(key)
                if key == 'approved' and value is False:
                    filtered[key] = value
                elif key in ('requested_ids', 'extra_ids') and isinstance(value, list) and all(isinstance(item, str) and item.isdigit() for item in value):
                    filtered[key] = value
                elif key == 'task_id' and isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
                    filtered[key] = value
                elif key == 'sha256' and isinstance(value, str) and re.fullmatch(r'[a-fA-F0-9]{64}', value):
                    filtered[key] = value
                elif key == 'output' and _safe_path(value):
                    filtered[key] = value
            clean['approval_required'] = filtered
        result['error_details'] = clean
    return result


def parse_state(text: str) -> dict:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != '{':
            continue
        try:
            parsed, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and any(key in parsed for key in ('loggedIn', 'ok', 'error_code')):
            return parsed
    return {}


def run(command: list[str]) -> int:
    # Child diagnostics may contain credentials/HTTP bodies. Never relay raw streams.
    completed = subprocess.run(command, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    if completed.returncode:
        diagnostic = safe_diagnostic(parse_state(completed.stdout or ''))
        if diagnostic:
            print(json.dumps(diagnostic, ensure_ascii=False), flush=True)
    return completed.returncode


def run_gate(command: list[str]) -> tuple[int, dict]:
    """跑登录闸门：截获 stdout 的 JSON 状态，失败时把 action 文案带给调用方。"""
    completed = subprocess.run(command, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    state = safe_diagnostic(parse_state(completed.stdout or ''))
    if completed.returncode and state:
        print(json.dumps(state, ensure_ascii=False), flush=True)
    return completed.returncode, state


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="淘工厂资产盘算：登录闸门 → 导出 → 合并（单公司）")
    p.add_argument("--company-name", required=True, help="公司/店铺名称，取用户消息原文")
    p.add_argument("--mode", choices=["full", "export"], default="full",
                   help="full=导出+合并（淘工厂资产盘算）；export=仅导出（淘工厂资产盘算导出）")
    p.add_argument("--module", default="all",
                   help="流程：默认 all 全部17流程；罚款模式传 罚款（淘工厂资产盘算导出罚款）")
    p.add_argument("--order-pay-start", help="支付开始 YYYY-MM-DD（用户给的支付日期）")
    p.add_argument("--order-pay-end", help="支付结束 YYYY-MM-DD（用户给的支付日期）")
    p.add_argument("--write-off-start", help="结算开始 YYYY-MM-DD（用户给的结算日期）")
    p.add_argument("--write-off-end", help="结算结束 YYYY-MM-DD（用户给的结算日期）")
    p.add_argument("--settlement-date",
                   help="合并用结算日期；缺省用结算区间，再退支付区间，再退当天")
    p.add_argument("--export-root", type=Path, default=EXPORT_ROOT_DEFAULT,
                   help="淘工厂导出输出根目录（默认 ~/Downloads/淘工厂导出）")
    p.add_argument("--fetch-file", type=Path,
                   help="用户贴的 queryBillDetail fetch；跳过本机闸门，导出内核仍校验该会话公司")
    p.add_argument("--login-wait", type=float, default=10.0,
                   help="登录闸门等待 cookie 落库的秒数（默认 10）")
    p.add_argument("--expect-account", help="仅用户明确要求核对特定账号时提供；不得自动填登录手机号")
    p.add_argument("--strict-account", action="store_true",
                   help="显式严格账号核对；公司匹配也阻断可比账号不符，须同时给 --expect-account")
    p.add_argument("--resume", action="store_true", help="从同一输出路径对应的内部运行状态续跑")
    p.add_argument("--browser", default="auto")
    p.add_argument("--transport", choices=["auto", "cookie", "bridge"], default="auto")
    p.add_argument("--bridge-url")
    p.add_argument("--batch-folder", type=Path, help="多公司共用的批次总目录；各公司仍严格串行")
    p.add_argument("--output-dir", type=Path, help="显式覆盖合并输出目录；默认与公司导出文件夹并列（导出根或批次总目录）")
    p.add_argument("--dry-run", action="store_true", help="离线显示执行命令，不登录、不导出、不写文件")
    p.add_argument("--erp-transport", choices=["auto", "cookie", "bridge"], default="auto")
    p.add_argument("--erp-expect-company", help="用户明确指定的ERP公司，不从淘工厂简称推断")
    p.add_argument("--erp-browser", default="auto")
    p.add_argument("--erp-bridge-url", help="独立ERP桥，默认8901；不复用淘工厂桥")
    p.add_argument('--heartbeat-interval', type=float, default=60.0,
                   help='stderr安全进度心跳间隔秒数，须为有限正数（默认60；阶段切换立即报告）')
    p.add_argument('--erp-scope-approval-file', type=Path,
                   help='仅显式--resume：ERP下载范围授权证据文件')
    p.add_argument('--erp-proven-partner-consent', action='store_true',
                   help='仅已取得有证据合单伙伴仅ERP保留的固定授权时使用；离线六字段重验后最多续跑一次，不扩大处罚范围')
    args = p.parse_args()
    if args.dry_run:
        # Dynamic layout imports must not create __pycache__ during dry-run.
        sys.dont_write_bytecode = True
    import math
    if not math.isfinite(args.heartbeat_interval) or args.heartbeat_interval <= 0:
        p.error('--heartbeat-interval 须为有限正数')
    if args.erp_scope_approval_file and not args.resume:
        p.error('--erp-scope-approval-file 只能与 --resume 一起使用')
    if args.strict_account and not (args.expect_account or "").strip():
        p.error("--strict-account requires --expect-account")
    if (not args.company_name.strip() or re.search(r'[\\/:*?"<>|]', args.company_name)
            or args.company_name in ('.', '..') or args.company_name.endswith(('.', ' '))):
        p.error("公司名称为空或含不安全文件名字符")
    # Validate supplied dates before login, network requests, or directory writes.
    for prefix in ("order_pay", "write_off"):
        start, end = (getattr(args, prefix + suffix) for suffix in ("_start", "_end"))
        if bool(start) != bool(end):
            p.error(f"{prefix} 的开始和结束日期必须同时提供")
        if start:
            try:
                if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start, end)):
                    raise ValueError("日期格式须为 YYYY-MM-DD")
                if date.fromisoformat(start) > date.fromisoformat(end):
                    raise ValueError("开始日期不能晚于结束日期")
            except ValueError as exc:
                p.error(f"{prefix} 日期无效：{exc}")
    if args.settlement_date:
        parts = re.split(r"[~_]", args.settlement_date.strip())
        try:
            if len(parts) not in (1, 2) or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in parts):
                raise ValueError("须为 YYYY-MM-DD 或 YYYY-MM-DD_YYYY-MM-DD")
            parsed = [date.fromisoformat(value) for value in parts]
            if len(parsed) == 2 and parsed[0] > parsed[1]:
                raise ValueError("开始日期不能晚于结束日期")
        except ValueError as exc:
            p.error(f"--settlement-date 无效：{exc}")
    if not args.order_pay_start or not args.write_off_start:
        p.error("支付和结算日期区间均须提供")
    args.export_root = args.export_root.expanduser().resolve()
    if args.batch_folder:
        args.batch_folder = args.batch_folder.expanduser().resolve()

    check_dependencies()
    ensure_login, export_script, merge_script = (str(script) for _, script in DEPENDENCIES)
    module = str(args.module).strip()
    is_all_module = module.lower() in ALL_MODULE_WORDS or module in ALL_MODULE_WORDS

    # 1) 登录闸门：先确认当前浏览器登录的正是目标公司（ensure_login 自带
    #    cookie 库 → 浏览器桥回退；失败时其 JSON 里的 action 指引会原样转达）
    transport_args = ["--browser", args.browser, "--transport", args.transport]
    if args.bridge_url:
        transport_args += ["--bridge-url", args.bridge_url]
    gate_cmd = None
    if args.fetch_file is None:
        gate_cmd = [sys.executable, ensure_login, "--expect-company", args.company_name,
                    "--wait", str(max(args.login_wait, 0.0)), *transport_args]
        if args.expect_account:
            gate_cmd += ["--expect-account", args.expect_account]
        if args.strict_account:
            gate_cmd.append("--strict-account")

    # 2) 导出
    export_cmd = [
        sys.executable, export_script,
        "--company-name", args.company_name,
        "--module", module,
        "--output-root", str(args.export_root),
    ]
    for key in ("order_pay_start", "order_pay_end", "write_off_start", "write_off_end"):
        value = getattr(args, key)
        if value:
            export_cmd += [f"--{key.replace('_', '-')}", value]
    if args.fetch_file:
        export_cmd += ["--fetch-file", str(args.fetch_file)]
    export_cmd += transport_args
    if args.batch_folder:
        export_cmd += ["--batch-folder", str(args.batch_folder)]
    if args.resume:
        export_cmd.append("--resume")
    import importlib.util
    spec = importlib.util.spec_from_file_location("tgc_export_layout", export_script)
    layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout)
    company_folder = layout.build_folder(args.batch_folder or args.export_root, args.company_name, args)
    args.canonical_module = layout.MODULE_ALIASES.get(module, module)
    if args.canonical_module not in {"all", *layout.MODULE_DISPLAY_NAMES}:
        p.error("未知流程")
    merge_cmd = None

    # 3) 合并：仅 full + 全部流程 时执行
    if args.mode == "full" and is_all_module:
        settlement = (
            args.settlement_date
            or (f"{args.write_off_start}_{args.write_off_end}"
                if args.write_off_start and args.write_off_end else None)
            or (f"{args.order_pay_start}_{args.order_pay_end}"
                if args.order_pay_start and args.order_pay_end else None)
        )
        merge_cmd = [
            sys.executable, merge_script,
            "--company-name", args.company_name,
            "--export-root", str(args.export_root),
            "--strict", "--no-recalculate",
        ]
        if settlement:
            merge_cmd += ["--settlement-date", settlement]
        # 始终显式定位本次导出的公司目录，避免自定义结算标签导致匹配错目录。
        # 合并默认输出到该目录的父目录，主表与原始导出目录并列；不增加公司包装层。
        merge_cmd += ["--export-folder", str(company_folder)]
        if args.output_dir:
            merge_cmd += ["--output-dir", str(args.output_dir.expanduser().resolve())]
    elif args.mode == "full":
        print("仅导出部分流程（--module 非 all），跳过合并；需要合并时跑「淘工厂合并」skill")
    else:
        print("export 模式：不合并；需要合并时跑「淘工厂合并」skill")

    if args.dry_run:
        print("DRY RUN: 登录 → 导出 → 本次有效罚款条件ERP → 一次严格合并 → 待核查页 → 公式重算 → 校验发布")
        print('DRY RUN: export（参数不回显）')
        if merge_cmd:
            print('DRY RUN: strict merge（参数不回显）')
        return 0
    from company_run import execute
    try:
        startup_preflight(company_folder,
                          args.output_dir or company_folder.parent,
                          merge_required=merge_cmd is not None)
        return execute(args, company_folder, export_cmd, merge_cmd, run, run_gate,
                       gate_cmd, resolve_skill_script)
    except (ValueError, OSError, KeyError, ImportError):
        # Exceptions can contain child/network values or user-supplied paths.
        print('流水线未完成；保留成果，同批次 --resume；请核实阶段状态', file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
