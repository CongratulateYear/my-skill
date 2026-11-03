#!/usr/bin/env python3
"""罚款导出：拉取天猫 rules-center 处罚数据（违规记录+赔付记录）并导出罚款明细 Excel。

登录态来源：本机 Chromium 系浏览器的 cookie 库（scripts/tmall_session.py 解密），
或显式 --fetch-file 复用 DevTools 复制的请求头。流程：登录态 → loginStatus 校验
→ 从 cookie 提取 XSRF token → 两个 tab（punish/compensation）按 paginator.items
翻页取满 → 合并映射 11 列 → 写出 `<店铺>_罚款_<支付开始>_<支付结束>.xlsx`。

支付日期即处罚时间范围：开始日 00:00:00.000、结束日 23:59:59.999（北京时间毫秒）。
凭据（cookie、XSRF token、授权头）只在内存使用，不写入技能文件、状态文件或日志。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

RULES_CENTER_URL = "https://tgc.tmall.com/ds/api/v1/rules-center/pageList"
RULES_CENTER_REFERER = (
    "https://tgc.tmall.com/ds/page/supplier/rules-center"
    "?c2mNavigatorShellPage=1&c2mNavigatorPageOpener=0"
)
RULES_CENTER_PAGE_SIZE = 50
RULES_CENTER_MAX_PAGES = 60
TAB_TYPES = ("punish", "compensation")
TAB_LABELS = {"punish": "违规记录", "compensation": "赔付记录"}

CST = timezone(timedelta(hours=8))
SHEET_NAME = "处罚数据"  # 与资产盘算「处罚数据」Sheet 表头一致，可直接导入盘算
FINE_HEADERS = [
    "店铺", "类型", "处罚内容", "主订单号", "处罚时间", "违规类型", "违规事件",
    "商品id", "商品标题", "运营", "处罚金额",
]
# 类型列：punish=违规记录归为「处罚数据」，compensation=赔付记录归为「赔付追缴」
TAB_TYPE_LABELS = {"punish": "处罚数据", "compensation": "赔付追缴"}

DEFAULT_SPREADSHEET_PYTHON = Path(
    "/Users/rabbit/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)
WORKBOOK_WRITER = """
import json, sys
from openpyxl import Workbook
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
wb = Workbook()
ws = wb.active
ws.title = payload["sheet"]
ws.append(payload["headers"])
for row in payload["rows"]:
    ws.append(row)
wb.save(sys.argv[2])
"""

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEED_LOGIN = 2


class AuthenticationError(RuntimeError):
    """登录态失效或未通过鉴权。"""


def is_auth_failure(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False).lower()
    return any(token in text for token in (
        "unauthorized", "not login", "login required", "session expired",
        "未登录", "请登录", "登录失效", "登录过期", "鉴权失败", "无权限", '"401"',
    ))


def safe_summary(value: Any) -> str:
    # 响应可能携带凭据或签名地址，统一脱敏后再进入日志/错误信息。
    text = json.dumps(value, ensure_ascii=False)
    text = re.sub(
        r'("(?:cookie|set-cookie|authorization|x-xsrf-token|xsrf-token)"\s*:\s*)"[^"]*"',
        r'\1"<redacted>"',
        text,
        flags=re.I,
    )
    text = re.sub(r"https?://[^\s\"<>]+", "<download-url>", text)
    return text[:1500]


def post_json(session: Any, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
    request_headers = {
        key: value for key, value in headers.items()
        if key.lower() not in {"content-length", "host"}
    }
    request_headers.setdefault("content-type", "application/json")
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        response = session.open(request, timeout=180)
        raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            data = {"_text": raw.decode("utf-8", errors="replace")[:1000]}
        if exc.code == 401 or is_auth_failure(data):
            raise AuthenticationError(f"HTTP {exc.code}：登录态已失效，请重新登录后重试") from exc
        raise RuntimeError(f"HTTP {exc.code}: {safe_summary(data)}") from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"网络请求失败：{exc}") from exc
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = {"_text": raw.decode("utf-8", errors="replace")[:1000]}
    if isinstance(data, dict) and data.get("success") is False:
        if is_auth_failure(data):
            raise AuthenticationError("登录态已失效，请重新登录后重试")
        raise RuntimeError(f"接口失败：{safe_summary(data)}")
    return data


# ---------------------------------------------------------------- 登录态与 token


def normalize_cookie(cookie: str) -> str:
    # Markdown 可能把 `_tb_token_`、`_nk_` 渲染成星号转义文本。
    replacements = {
        "*tb\\_token*": "_tb_token_",
        "*tb_token*": "_tb_token_",
        "*nk*": "_nk_",
        "*l\\_g*": "_l_g_",
        "*l_g*": "_l_g_",
    }
    for source, target in replacements.items():
        cookie = cookie.replace(source, target)
    return cookie


def parse_fetches(text: str) -> list[tuple[str, dict[str, Any]]]:
    """解析从 DevTools 复制的 fetch 代码块。"""
    results: list[tuple[str, dict[str, Any]]] = []
    offset = 0
    while True:
        match = re.search(r'''fetch\(\s*(["'])(.*?)\1\s*,\s*''', text[offset:])
        if not match:
            break
        url = match.group(2)
        start = offset + match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text) or text[start] != "{":
            raise ValueError("无法解析 fetch 的请求配置")
        end = _balanced_object_end(text, start)
        raw_object = text[start:end]
        try:
            obj = json.loads(raw_object)
        except json.JSONDecodeError as exc:
            repaired = raw_object.replace("\\_", "_")
            if repaired == raw_object:
                raise ValueError(f"fetch 请求配置不是有效 JSON：{exc}") from exc
            try:
                obj = json.loads(repaired)
            except json.JSONDecodeError:
                raise ValueError(f"fetch 请求配置不是有效 JSON：{exc}") from exc
        results.append((url, obj))
        offset = end
    return results


def _balanced_object_end(text: str, start: int) -> int:
    depth = 0
    quoted = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("fetch 请求配置缺少结束大括号")


def load_fetch_headers(path: Path) -> dict[str, str]:
    """从 fetch 文件只取登录请求头（cookie/xsrf）；日期一律以命令行为准。"""
    fetches = parse_fetches(path.read_text(encoding="utf-8"))
    if not fetches:
        raise ValueError("fetch 文件中没有找到 fetch 请求")
    url, request = fetches[0]
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "tgc.tmall.com":
        raise ValueError("fetch 必须来自 https://tgc.tmall.com 的请求")
    if str(request.get("method", "GET")).upper() != "POST":
        raise ValueError("fetch 必须是 POST 请求")
    source = request.get("headers") or {}
    if not isinstance(source, dict):
        raise ValueError("fetch headers 格式不正确")
    headers = {str(key).lower(): str(value) for key, value in source.items()}
    for key in ("host", "content-length", "connection", "accept-encoding", "priority"):
        headers.pop(key, None)
    if "cookie" not in headers:
        raise ValueError("fetch headers 中没有 Cookie，无法复用登录态")
    headers["cookie"] = normalize_cookie(headers["cookie"])
    headers.setdefault(
        "user-agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    )
    print("已读取登录态：fetch 文件")
    return headers


def load_browser_headers(browser: str) -> dict[str, str]:
    import tmall_session

    return tmall_session.build_headers(browser)


def resolve_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.fetch_file is not None:
        return load_fetch_headers(args.fetch_file.expanduser().resolve())
    return load_browser_headers(args.browser)


def check_session(headers: dict[str, str]) -> bool:
    import tmall_session

    return tmall_session.check_session(headers)


def token_state(headers: dict[str, str]) -> dict[str, Any]:
    """脱敏的 token/账号诊断；绝不输出 token 或 cookie 值本身。"""
    cookie = headers.get("cookie", "")
    parts = [part for part in cookie.split("; ") if "=" in part]
    names = {part.split("=", 1)[0] for part in parts}
    account = "未知"
    for part in parts:
        if part.startswith("tracknick=") or part.startswith("lgc="):
            account = part.split("=", 1)[1]
            break
    return {
        "account": account,
        "cookieCount": len(parts),
        "hasCookie": bool(parts),
        "hasTbToken": "_tb_token_" in names,
        "hasCookie2": "cookie2" in names,
        "hasXsrfToken": bool(headers.get("x-xsrf-token")),
    }


def require_token(headers: dict[str, str]) -> dict[str, Any]:
    """POST 前确认 XSRF token 已就绪；缺失直接报错，不让请求白打。"""
    state = token_state(headers)
    if not state["hasCookie"]:
        raise AuthenticationError("登录态里没有 cookie，无法请求天猫接口")
    if not state["hasXsrfToken"]:
        raise AuthenticationError(
            "登录 cookie 中没有 XSRF token（XSRF-TOKEN 与 X-XSRF-TOKEN 均未找到），"
            "POST 会被 XSRF 校验拒绝；请在浏览器重新登录 tgc.tmall.com 后重试，"
            "或换用 --fetch-file 提供完整请求头"
        )
    print(
        "token 已获取：XSRF"
        f"（账号 {state['account']}，cookie {state['cookieCount']} 项，值不打印）"
    )
    return state


# ---------------------------------------------------------------- 日期与翻页


def parse_pay_dates(pay_start: str, pay_end: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(pay_start)
        end = date.fromisoformat(pay_end)
    except ValueError as exc:
        raise ValueError(f"支付日期必须为 YYYY-MM-DD：{exc}") from exc
    if start > end:
        raise ValueError(f"支付开始日期 {pay_start} 晚于结束日期 {pay_end}")
    return start, end


def epoch_ms(value: date, *, end: bool) -> int:
    """处罚时间范围转北京时间毫秒：开始日 00:00:00.000，结束日 23:59:59.999。"""
    moment = datetime(value.year, value.month, value.day, tzinfo=CST)
    if end:
        moment = moment + timedelta(days=1) - timedelta(milliseconds=1)
    return int(moment.timestamp() * 1000)


def fetch_tab_rows(
    session: Any,
    headers: dict[str, str],
    start_ms: int,
    end_ms: int,
    tab_type: str,
) -> tuple[list[dict[str, Any]], int | None]:
    """按 pageNo 翻页直到取满 paginator.items，保证数据条数不小于总数。"""
    rows: list[dict[str, Any]] = []
    total: int | None = None
    page_no = 1
    while True:
        payload = {
            "pageNo": page_no,
            "pageSize": RULES_CENTER_PAGE_SIZE,
            "startDateTime": start_ms,
            "endDateTime": end_ms,
            "tabType": tab_type,
        }
        response = post_json(session, RULES_CENTER_URL, headers, payload)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            data = []
        paginator = response.get("paginator") if isinstance(response, dict) else None
        if isinstance(paginator, dict) and total is None:
            for key in ("items", "total"):
                value = paginator.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                total = int(value)
                break
        rows.extend(item for item in data if isinstance(item, dict))
        print(
            f"  {TAB_LABELS[tab_type]} 第 {page_no} 页：{len(data)} 条，"
            f"累计 {len(rows)}/{total if total is not None else '未知'} 条"
        )
        if not data:
            break
        if total is not None and len(rows) >= total:
            break
        if len(data) < RULES_CENTER_PAGE_SIZE:
            break
        page_no += 1
        if page_no > RULES_CENTER_MAX_PAGES:
            raise RuntimeError(
                f"{TAB_LABELS[tab_type]} 翻页超过 {RULES_CENTER_MAX_PAGES} 页仍未取满"
            )
    return rows, total


# ---------------------------------------------------------------- 行映射


def safe_prefix(name: str | None) -> str:
    text = str(name or "").strip()
    for char in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t'):
        text = text.replace(char, "-")
    text = text.strip(" .-")
    if not text:
        raise ValueError("店铺名称不能为空；请填写用户发送的名称原文")
    return text


def rules_center_extend(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("extend")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def text_identifier(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


def item_id_of(item: dict[str, Any]) -> str | None:
    """商品id 三级回退：extend.itemId → extend.extendInfo.itemId → punishDimensionValue。"""
    extend = rules_center_extend(item)
    candidates: list[Any] = [extend.get("itemId")]
    inner = extend.get("extendInfo")
    if isinstance(inner, dict):
        candidates.append(inner.get("itemId"))
    candidates.append(item.get("itemId"))
    candidates.append(item.get("punishDimensionValue"))
    for value in candidates:
        text = text_identifier(value)
        if text:
            return text
    return None


def time_text_of(item: dict[str, Any]) -> str | None:
    for key in ("punishTime", "gmtFinish", "gmtCreate"):
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return datetime.fromtimestamp(value / 1000.0, CST).strftime("%Y-%m-%d %H:%M:%S")
    return None


def amount_of(item: dict[str, Any]) -> float:
    """接口金额单位为分，导出按元。"""
    extend = rules_center_extend(item)
    for value in (item.get("punishAmount"), extend.get("amount")):
        if value is None or isinstance(value, bool):
            continue
        try:
            return round(float(value) / 100.0, 2)
        except (TypeError, ValueError):
            continue
    return 0.0


def fine_row(item: dict[str, Any], shop_name: str, tab_type: str) -> list[Any]:
    content = item.get("punishValue") or item.get("remark") or item.get("reason")
    return [
        shop_name,
        TAB_TYPE_LABELS.get(tab_type),
        content,
        text_identifier(item.get("orderNo")),
        time_text_of(item),
        item.get("violationType"),
        item.get("violationEvent"),
        item_id_of(item),
        None,  # 接口不返回商品标题，不臆造
        None,  # 运营留空
        amount_of(item),
    ]


# ---------------------------------------------------------------- 写出


def find_spreadsheet_python() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("CODEX_SPREADSHEET_PYTHON")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([DEFAULT_SPREADSHEET_PYTHON, Path(sys.executable)])
    checked: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in checked or not candidate.exists():
            continue
        checked.add(candidate)
        probe = subprocess.run(
            [str(candidate), "-c", "import openpyxl"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        "未找到可导入 openpyxl 的 Python；可通过 CODEX_SPREADSHEET_PYTHON 指定工作区 Python"
    )


def write_workbook(path: Path, rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_file = path.with_name(path.stem + ".payload.json")
    payload_file.write_text(
        json.dumps({"sheet": SHEET_NAME, "headers": FINE_HEADERS, "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        python = find_spreadsheet_python()
        completed = subprocess.run(
            [str(python), "-c", WORKBOOK_WRITER, str(payload_file), str(path)],
            text=True, capture_output=True, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"生成罚款明细失败：{detail[-1000:]}")
    finally:
        try:
            payload_file.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 命令行


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    import tmall_session

    p = argparse.ArgumentParser(description="导出天猫罚款明细（违规记录+赔付记录合并）")
    p.add_argument("--shop-name", help="店铺名称，取用户发送的名称原文；写入「店铺」列并作文件名前缀")
    p.add_argument("--pay-start", "--order-pay-start", dest="pay_start",
                   help="支付日期开始 YYYY-MM-DD（即处罚时间范围开始）")
    p.add_argument("--pay-end", "--order-pay-end", dest="pay_end",
                   help="支付日期结束 YYYY-MM-DD（即处罚时间范围结束）")
    p.add_argument("--output-dir", type=Path, default=Path("outputs"),
                   help="罚款明细输出目录，默认 ./outputs")
    p.add_argument("--fetch-file", type=Path,
                   help="可选：含 DevTools 复制的 fetch 的文本文件，只取其登录请求头；日期仍以命令行为准")
    p.add_argument("--browser", default="chrome", choices=[*tmall_session.BROWSERS, "auto"],
                   help="读取哪个浏览器的 cookie，默认 chrome")
    p.add_argument("--check-login", action="store_true",
                   help="只检查登录态与 token 并输出脱敏 JSON，不请求数据")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析参数并显示请求计划（含时间戳），不登录、不请求、不写文件")
    p.add_argument("--json", action="store_true", help="结束时额外输出一行 JSON 摘要")
    return p.parse_args(argv)


def run_check_login(args: argparse.Namespace) -> int:
    import tmall_session

    try:
        headers = resolve_headers(args)
    except tmall_session.SessionError as exc:
        print(json.dumps({"loggedIn": False, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return EXIT_NEED_LOGIN
    logged_in = check_session(headers)
    state = token_state(headers)
    print(json.dumps({
        "loggedIn": logged_in,
        "account": state["account"],
        "cookieCount": state["cookieCount"],
        "hasTbToken": state["hasTbToken"],
        "hasXsrfToken": state["hasXsrfToken"],
    }, ensure_ascii=False, indent=2))
    return EXIT_OK if logged_in and state["hasXsrfToken"] else EXIT_NEED_LOGIN


def dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    start, end = parse_pay_dates(args.pay_start, args.pay_end)
    shop = safe_prefix(args.shop_name) if args.shop_name else "<店铺>"
    return {
        "shopName": args.shop_name,
        "payStart": args.pay_start,
        "payEnd": args.pay_end,
        "source": "fetch-file" if args.fetch_file else f"browser:{args.browser}",
        "requestUrl": RULES_CENTER_URL,
        "tabPayloads": [{
            "pageNo": 1,
            "pageSize": RULES_CENTER_PAGE_SIZE,
            "startDateTime": epoch_ms(start, end=False),
            "endDateTime": epoch_ms(end, end=True),
            "tabType": tab,
        } for tab in TAB_TYPES],
        "maxPages": RULES_CENTER_MAX_PAGES,
        "outputFile": str(
            (args.output_dir.expanduser().resolve() /
             f"{shop}_罚款_{args.pay_start}_{args.pay_end}.xlsx")
        ),
        "network": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check_login:
        return run_check_login(args)

    if not args.shop_name:
        raise ValueError("缺少 --shop-name；店铺名称取用户发送的名称原文")
    if not args.pay_start or not args.pay_end:
        raise ValueError("缺少支付日期：--pay-start/--pay-end（罚款按处罚时间取数，只需支付日期）")
    start, end = parse_pay_dates(args.pay_start, args.pay_end)
    shop = safe_prefix(args.shop_name)
    output_dir = args.output_dir.expanduser().resolve()
    output_file = output_dir / f"{shop}_罚款_{args.pay_start}_{args.pay_end}.xlsx"

    if args.dry_run:
        print(json.dumps(dry_run_plan(args), ensure_ascii=False, indent=2))
        return EXIT_OK

    headers = resolve_headers(args)
    if not check_session(headers):
        raise AuthenticationError(
            "登录态校验失败：请在当前浏览器里打开 https://tgcwork.tmall.com/login 重新登录"
            "（复用已有标签，不要新开浏览器），或用 "
            "/Users/rabbit/.agents/skills/asset-inventory/scripts/ensure_login.py --wait 30 "
            "确认 loggedIn=true 后重试"
        )
    require_token(headers)

    session = build_opener()
    start_ms, end_ms = epoch_ms(start, end=False), epoch_ms(end, end=True)
    request_headers = {**headers, "referer": RULES_CENTER_REFERER}

    rows: list[list[Any]] = []
    totals: dict[str, int | None] = {}
    for tab_type in TAB_TYPES:
        print(f"获取{TAB_LABELS[tab_type]}（tabType={tab_type}）：")
        items, total = fetch_tab_rows(session, request_headers, start_ms, end_ms, tab_type)
        totals[tab_type] = total
        print(f"  {TAB_LABELS[tab_type]}合计：{len(items)} 条"
              f"（接口总数 {total if total is not None else '未知'}）")
        if total is not None and len(items) < total:
            raise RuntimeError(
                f"{TAB_LABELS[tab_type]}仅取到 {len(items)}/{total} 条，数据不完整，停止写入"
            )
        rows.extend(fine_row(item, args.shop_name, tab_type) for item in items)

    write_workbook(output_file, rows)
    print(f"罚款明细：{output_file}")
    print(f"合并后行数：{len(rows)}（违规 {totals.get('punish') or 0} + "
          f"赔付 {totals.get('compensation') or 0}）")
    print(f"店铺列：{args.shop_name}")
    if args.json:
        print(json.dumps({
            "shopName": args.shop_name,
            "payStart": args.pay_start,
            "payEnd": args.pay_end,
            "punishRows": totals.get("punish") or 0,
            "compensationRows": totals.get("compensation") or 0,
            "totalRows": len(rows),
            "outputFile": str(output_file),
        }, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuthenticationError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(EXIT_NEED_LOGIN)
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
