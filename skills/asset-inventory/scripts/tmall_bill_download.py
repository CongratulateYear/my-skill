#!/usr/bin/env python3
"""Run all configured asset-inventory bill flows from one DevTools fetch.

The copied queryBillDetail request supplies only the authenticated session and
date ranges. By default every configured module runs with its own fixed
billType and date-field requirements. Date sources follow the user's input:
"结算+支付" modules use both the settlement and payment dates the user gave,
while the four settlement-only modules (评价有礼/商业化账户充值提现/客服服务费/
赔付追缴) take their settlement range from the user's PAYMENT dates. The
dedicated 所有商品效果数据导出 module uses the hosting download-task chain
(bizType/bizScene) and only the orderPayTime range. Credentials are never
persisted.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urlparse
from urllib.request import Request, build_opener

BASE = "https://tgc.tmall.com/ds/api/v1/fbi"
DOWNLOAD_TYPE = "DETAIL_BILL"
HOSTING_BASE = "https://tgc.tmall.com/ds/api/v1/hosting"
HOSTING_REFERER = (
    "https://tgc.tmall.com/ds/page/supplier/commercial-hosting-report"
    "?c2mNavigatorShellPage=2&c2mNavigatorPageOpener=1"
)
HOSTING_POLL_SECONDS = 5
HOSTING_MAX_POLLS = 10
RULES_CENTER_PATH = "/ds/api/v1/rules-center/pageList"
RULES_CENTER_REFERER = (
    "https://tgc.tmall.com/ds/page/supplier/rules-center"
    "?c2mNavigatorShellPage=1&c2mNavigatorPageOpener=0"
)
RULES_CENTER_PAGE_SIZE = 50
RULES_CENTER_MAX_PAGES = 60
RULES_CENTER_TABS = ("punish", "compensation")
MODULES = {
    "consumer-experience-service": {
        "name": "消费者体验提升服务",
        "target_sheet": "消费者体验提升服务",
        "bill_type": "FREIGHT_INSURANCE_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "product-loan": {
        "name": "商品贷款",
        "target_sheet": "商品货款",
        "bill_type": "NYPX_MODE_PAYMENT_BILL,NYPX_GOODS_FUND_PAYMENT_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "trade-subsidy": {
        "name": "交易补贴",
        "target_sheet": "交易补贴",
        "bill_type": "GOLD_STANDARD_INVOICE_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "trade-commission": {
        "name": "交易佣金",
        "target_sheet": "交易佣金",
        "bill_type": "NYPX_MODE_SETTLE_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "credit-buy": {
        "name": "先用后付",
        "target_sheet": "先用后付",
        "bill_type": "CREDIT_BUY_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "xinjiang-logistics-consolidation": {
        "name": "新疆物流集运",
        "target_sheet": "新疆物流集运",
        "bill_type": "COLLECTIVE_LOGISTICS_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "review-gift": {
        "name": "评价有礼",
        "target_sheet": "评价有礼",
        "bill_type": "REVIEW_GIFT_BILL",
        "date_keys": ("writeOffTime",),
        # 仅结算流程：结算区间取用户给的支付日期
        "settle_from": "orderPayTime",
    },
    "channel-promotion": {
        "name": "营销费用",
        "target_sheet": "营销费用",
        "bill_type": "CHANNEL_PROMOTION_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "ad-agency": {
        "name": "商业化账户充值提现",
        "target_sheet": "商业化账户充值提现",
        "bill_type": "AD_AGENCY_BILL",
        "date_keys": ("writeOffTime",),
        # 仅结算流程：结算区间取用户给的支付日期
        "settle_from": "orderPayTime",
    },
    "all-site-channel-promotion": {
        "name": "投流推广佣金",
        "target_sheet": "投流推广佣金",
        "bill_type": "ALL_SITE_CHANNEL_PROMOTION_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "taoke-channel-promotion": {
        "name": "淘客推广佣金",
        "target_sheet": "淘客推广佣金",
        "bill_type": "TAOKE_CHANNEL_PROMOTION_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "live-channel-promotion": {
        "name": "直播推广佣金",
        "target_sheet": "直播推广佣金",
        "bill_type": "LIVE_CHANNEL_PROMOTION_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "customer-service-fee": {
        "name": "客服服务费",
        "target_sheet": "客服服务费",
        "bill_type": "CUSTOMER_SERVICE_FEE_BILL",
        "date_keys": ("writeOffTime",),
        # 仅结算流程：结算区间取用户给的支付日期
        "settle_from": "orderPayTime",
    },
    "compensate-recovery": {
        "name": "赔付追缴",
        "target_sheet": "赔付追缴",
        "bill_type": "COMPENSATE_RECOVERY_BILL",
        "date_keys": ("writeOffTime",),
        # 仅结算流程：结算区间取用户给的支付日期
        "settle_from": "orderPayTime",
    },
    "punishment-data": {
        "name": "处罚数据",
        "target_sheet": "处罚数据",
        # 违规记录(punish)+赔付记录(compensation)两接口合并；处罚时间对应支付时间
        "date_keys": ("orderPayTime",),
        "api_style": "rules-center",
        "tab_types": RULES_CENTER_TABS,
    },
    "overseas-added-commission": {
        "name": "出海增值服务费",
        "target_sheet": "出海增值服务费",
        "bill_type": "OVERSEAS_ADDED_COMMISSION_BILL",
        "date_keys": ("writeOffTime", "orderPayTime"),
    },
    "all-item-effects-export": {
        "name": "所有商品效果数据导出",
        "target_sheet": "所有商品效果数据导出",
        "biz_type": "COMMISSION_RATE",
        "biz_scene": "SUPPLIER_ALL_ITEM_DAILY_DETAIL",
        "date_keys": ("orderPayTime",),
        "api_style": "hosting",
    },
}
DEFAULT_POLL_SECONDS = 10
DEFAULT_MAX_WAIT_SECONDS = 900
QUERY_PATH = "/ds/api/v1/fbi/bill/queryBillDetail"
SKILL_ROOT = Path(__file__).resolve().parents[1]
PAN_SUAN_BUILDER = Path(__file__).resolve().with_name("pan_suan_builder.py")
DEFAULT_PAN_SUAN_TEMPLATE = SKILL_ROOT / "assets" / "盘算模板.xlsx"
DEFAULT_SPREADSHEET_PYTHON = Path(
    "/Users/rabbit/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="下载资产盘算全部账单流程并写入工作簿")
    p.add_argument(
        "--module", choices=["all", *MODULES], default="all",
        help="默认 all 执行全部17个流程；仅在用户明确限定时选择单一模块",
    )
    p.add_argument(
        "--skip-module", choices=list(MODULES), action="append", default=[],
        help="跳过已确认完成的模块，可重复；用于旧运行结果的手工续跑",
    )
    p.add_argument("--resume", action="store_true", help="读取无凭据状态文件并跳过其中已完成的模块")
    p.add_argument("--state-file", type=Path, help="续跑状态文件；默认与盘算输出同目录同前缀")
    p.add_argument(
        "--fetch-file", type=Path,
        help="包含最新第一步 fetch 的文本文件；省略则自动复用本机浏览器的天猫登录态",
    )
    p.add_argument(
        "--write-off-start",
        help="结算时间开始 YYYY-MM-DD；仅结算流程不用它（那4个流程的结算区间取支付日期）",
    )
    p.add_argument(
        "--write-off-end",
        help="结算时间结束 YYYY-MM-DD；仅结算流程不用它（那4个流程的结算区间取支付日期）",
    )
    p.add_argument("--order-pay-start", help="支付时间开始 YYYY-MM-DD；结算+支付流程与仅结算流程都用它")
    p.add_argument("--order-pay-end", help="支付时间结束 YYYY-MM-DD；结算+支付流程与仅结算流程都用它")
    p.add_argument("--output-dir", type=Path, default=Path("outputs"), help="原始下载文件、解压目录与默认盘算文件目录")
    p.add_argument(
        "--pan-suan-template", type=Path, default=DEFAULT_PAN_SUAN_TEMPLATE,
        help="盘算模板；默认使用技能内置模板，已有同名输出时自动继续累积",
    )
    p.add_argument("--pan-suan-output", type=Path, help="盘算输出文件；默认按 店铺_结算日期 生成到 output-dir")
    p.add_argument(
        "--shop-name",
        help="店铺名称，取自用户发送的名称，例如 埔鸣--迎风部 或 埔鸣；写入公司汇总「店铺」列，并作为盘算文件名前缀",
    )
    p.add_argument("--no-pan-suan", action="store_true", help="只下载和解压，不写入盘算工作簿")
    p.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    p.add_argument("--max-wait-seconds", type=int, default=DEFAULT_MAX_WAIT_SECONDS)
    p.add_argument("--dry-run", action="store_true", help="仅解析并显示全部执行计划，不调用接口")
    return p.parse_args()

def parse_fetches(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse JSON-like fetch blocks copied from DevTools."""
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
            # Some chat/Markdown surfaces escape underscores inside cookie keys
            # (for example `_tb_token_` -> `*tb\_token*`), which leaves an
            # invalid JSON escape sequence. Try the minimal repair that preserves
            # all other copied headers/body values before failing.
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
        c = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
            continue
        if c == '"':
            quoted = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("fetch 请求配置缺少结束大括号")


def json_body(request: dict[str, Any]) -> dict[str, Any]:
    raw = request.get("body")
    if not isinstance(raw, str):
        raise ValueError("第一步 fetch 缺少 JSON body")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"第一步 fetch body 不是有效 JSON：{exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("第一步 fetch body 必须是 JSON 对象")
    return result


def normalize_cookie(cookie: str) -> str:
    # Markdown can render `_tb_token_` and `_nk_` as starred/escaped text.
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


def headers_from(request: dict[str, Any]) -> dict[str, str]:
    source = request.get("headers") or {}
    if not isinstance(source, dict):
        raise ValueError("fetch headers 格式不正确")
    headers = {str(k).lower(): str(v) for k, v in source.items()}
    # Do not forward browser-managed transport headers from the copied request.
    for key in list(headers):
        if key in {"host", "content-length", "connection", "accept-encoding", "priority"}:
            headers.pop(key, None)
    if "cookie" in headers:
        headers["cookie"] = normalize_cookie(headers["cookie"])
    return headers


def validate_fetch_source(url: str, request: dict[str, Any]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "tgc.tmall.com" or parsed.path != QUERY_PATH:
        raise ValueError("fetch 必须来自天猫 queryBillDetail 第一步请求")
    if str(request.get("method", "GET")).upper() != "POST":
        raise ValueError("第一步 queryBillDetail fetch 必须是 POST 请求")


def load_browser_headers() -> dict[str, str]:
    """复用本机浏览器已登录的天猫会话，省去手工复制 fetch。"""
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import tmall_session

    headers = tmall_session.build_headers()
    if not tmall_session.check_session(headers):
        raise ValueError(
            "本机浏览器登录态已失效：请在 Chrome 中登录 "
            "https://tgcwork.tmall.com/login 后重试"
        )
    return headers


def validate_runtime_options(args: argparse.Namespace) -> None:
    if args.poll_seconds <= 0:
        raise ValueError("--poll-seconds 必须大于 0")
    if args.max_wait_seconds <= 0:
        raise ValueError("--max-wait-seconds 必须大于 0")
    if args.shop_name is not None and not safe_shop_prefix(args.shop_name):
        raise ValueError("--shop-name 不能为空；请填写用户发送的店铺名称，例如 埔鸣--迎风部 或 埔鸣")


DATE_ARG_PREFIXES = {
    "writeOffTime": "write_off",
    "orderPayTime": "order_pay",
}


def date_value(cli: str | None, body: dict[str, Any], *path: str) -> str | None:
    if cli is not None:
        return cli
    value: Any = body
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value if isinstance(value, str) else None


def date_source_key(module: dict[str, Any], key: str) -> str:
    """仅结算流程（settle_from=orderPayTime）的结算区间取用户给的支付日期。"""
    if key == "writeOffTime" and module.get("settle_from"):
        return str(module["settle_from"])
    return key


def date_cli_hint(source_key: str, side: str) -> str:
    return "--" + DATE_ARG_PREFIXES[source_key].replace("_", "-") + "-" + side


def module_date_filters(args: argparse.Namespace, body: dict[str, Any], module: dict[str, Any]) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for key in module["date_keys"]:
        source_key = date_source_key(module, key)
        prefix = DATE_ARG_PREFIXES[source_key]
        pair = {
            "start": date_value(getattr(args, f"{prefix}_start"), body, "param", source_key, "start"),
            "end": date_value(getattr(args, f"{prefix}_end"), body, "param", source_key, "end"),
        }
        for side, value in pair.items():
            if not value:
                missing.append(f"{key}.{side}（请提供 {date_cli_hint(source_key, side)}）")
            else:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    invalid.append(f"{key}.{side}={value}")
        if pair["start"] and pair["end"]:
            try:
                if date.fromisoformat(pair["start"]) > date.fromisoformat(pair["end"]):
                    invalid.append(f"{key}.start 晚于 {key}.end")
            except ValueError:
                pass
        values[key] = pair
    if missing:
        raise ValueError(f"{module['name']} 缺少日期：" + ", ".join(missing))
    if invalid:
        raise ValueError("日期必须为 YYYY-MM-DD：" + ", ".join(invalid))
    return values


def make_referer(date_filters: dict[str, dict[str, str]], bill_type: str) -> str:
    query: dict[str, str] = {
        "billType": bill_type,
        "c2mNavigatorShellPage": "0",
        "c2mNavigatorPageOpener": "",
    }
    for key, pair in date_filters.items():
        if key == "writeOffTime":
            query["writeOffTimeStart"] = pair["start"]
            query["writeOffTimeEnd"] = pair["end"]
        elif key == "orderPayTime":
            query["orderPayTimeStart"] = pair["start"]
            query["orderPayTimeEnd"] = pair["end"]
    return f"https://tgc.tmall.com/ds/page/supplier/common-bill-details-page-render?{urlencode(query)}"



class AuthenticationError(RuntimeError):
    """The copied browser session is no longer authorized."""


def is_auth_failure(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False).lower()
    return any(token in text for token in (
        "unauthorized", "not login", "login required", "session expired",
        "未登录", "请登录", "登录失效", "登录过期", "鉴权失败", "无权限", '"401"',
    ))

def post(session: Any, url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[Any, Any]:
    request_headers = {key: value for key, value in headers.items() if key.lower() not in {"content-length", "host"}}
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
            raise AuthenticationError(f"HTTP {exc.code}：登录态已失效，请提供重新登录后复制的最新 fetch") from exc
        raise RuntimeError(f"HTTP {exc.code}: {safe_summary(data)}") from exc
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = {"_text": raw.decode("utf-8", errors="replace")[:1000]}
    if isinstance(data, dict) and data.get("success") is False:
        if is_auth_failure(data):
            raise AuthenticationError("登录态已失效，请提供重新登录后复制的最新 fetch")
        raise RuntimeError(f"接口失败：{safe_summary(data)}")
    return response, data


def get_json(session: Any, url: str, headers: dict[str, str]) -> Any:
    request_headers = {
        key: value for key, value in headers.items()
        if key.lower() not in {"content-length", "host", "content-type"}
    }
    request = Request(url, headers=request_headers, method="GET")
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
            raise AuthenticationError(f"HTTP {exc.code}：登录态已失效，请提供重新登录后复制的最新 fetch") from exc
        raise RuntimeError(f"HTTP {exc.code}: {safe_summary(data)}") from exc
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = {"_text": raw.decode("utf-8", errors="replace")[:1000]}
    if isinstance(data, dict) and data.get("success") is False:
        if is_auth_failure(data):
            raise AuthenticationError("登录态已失效，请提供重新登录后复制的最新 fetch")
        raise RuntimeError(f"接口失败：{safe_summary(data)}")
    return data


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def safe_summary(value: Any) -> str:
    # Never print credentials or signed URLs from arbitrary responses.
    text = json.dumps(value, ensure_ascii=False)
    text = re.sub(
        r'("(?:cookie|set-cookie|authorization|x-xsrf-token|xsrf-token)"\s*:\s*)"[^"]*"',
        r'\1"<redacted>"',
        text,
        flags=re.I,
    )
    text = re.sub(r"https?://[^\s\"<>]+", "<download-url>", text)
    return text[:1500]


def find_task_id(value: Any) -> str | None:
    for item in walk(value):
        for key, child in item.items():
            if key.lower() in {"taskid", "task_id"} and isinstance(child, (str, int)):
                return str(child)
    match = re.search(r"\bDB_[A-Za-z0-9_-]+\b", json.dumps(value, ensure_ascii=False))
    return match.group(0) if match else None


def task_state(value: Any, task_id: str) -> tuple[str, str]:
    records = []
    for item in walk(value):
        if any(str(v) == task_id for k, v in item.items() if k.lower() in {"taskid", "task_id"}):
            records.append(item)
    fields: list[str] = []
    status_values: list[str] = []
    for item in records:
        for key, child in item.items():
            if any(word in key.lower() for word in ("status", "state", "progress")):
                fields.append(f"{key}={child}")
            if key.lower() in {"status", "state", "taskstatus", "task_state"}:
                status_values.append(str(child).strip().upper())
    text = " ".join(fields).lower()
    failed_states = {"FAIL", "FAILED", "ERROR", "CANCEL", "CANCELED", "CANCELLED"}
    success_states = {"SUCCEED", "SUCCEEDED", "SUCCESS", "FINISH", "FINISHED", "COMPLETE", "COMPLETED"}
    if any(state in failed_states for state in status_values) or any(word in text for word in ("fail", "error", "失败", "错误", "异常", "已取消")):
        return "failed", "; ".join(fields)
    if any(state in success_states for state in status_values) or any(word in text for word in ("下载完成", "已完成", "成功")):
        return "success", "; ".join(fields)
    return "pending", "; ".join(fields) or "未识别到状态字段"


def find_urls(value: Any) -> list[str]:
    urls: list[str] = []
    for item in walk(value):
        for key, child in item.items():
            if isinstance(child, str) and ("url" in key.lower() or "download" in key.lower() or child.startswith(("http://", "https://"))):
                urls.extend(re.findall(r"https?://[^\s\"<>]+", child))
    # The API can also return the URL as a top-level string.
    if isinstance(value, str):
        urls.extend(re.findall(r"https?://[^\s\"<>]+", value))
    return list(dict.fromkeys(urls))


def hosting_task_records(value: Any) -> list[dict[str, Any]]:
    """Return the first list of task records carrying hosting task fields."""
    found: list[list[dict[str, Any]]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            items = [item for item in node if isinstance(item, dict)]
            if items and any(
                any(key.lower() in {"taskstatus", "status", "fileurl", "taskid", "task_id"} for key in item)
                for item in items
            ):
                found.append(items)
            for item in items:
                visit(item)

    visit(value)
    return found[0] if found else []


def hosting_first_record(value: Any) -> dict[str, Any] | None:
    """Hosting list responses are checked by their first record (newest first)."""
    records = hosting_task_records(value)
    return records[0] if records else None


def hosting_status(record: dict[str, Any]) -> str:
    for key, child in record.items():
        if key.lower() in {"taskstatus", "status"} and isinstance(child, (str, int)):
            return str(child).strip().upper()
    return ""


def hosting_file_url(record: dict[str, Any]) -> str | None:
    for key, child in record.items():
        if "fileurl" in key.lower() and isinstance(child, str) and child.startswith("http"):
            return child
    return None


def safe_zip_extract(archive: Path, target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        root = target.resolve()
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"ZIP 包含不允许的符号链接：{info.filename}")
            destination = (target / info.filename).resolve()
            if root not in destination.parents and destination != root:
                raise RuntimeError(f"ZIP 包含不安全路径：{info.filename}")
        zf.extractall(target)
    return [p for p in target.rglob("*") if p.is_file()]


def safe_tar_extract(archive: Path, target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise RuntimeError(f"TAR 包含不允许的链接或设备文件：{member.name}")
            destination = (target / member.name).resolve()
            if root not in destination.parents and destination != root:
                raise RuntimeError(f"TAR 包含不安全路径：{member.name}")
        tf.extractall(target)
    return [p for p in target.rglob("*") if p.is_file()]


def archive_base_name(path: Path) -> str:
    lower = path.name.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".zip", ".tar", ".gz"):
        if lower.endswith(suffix):
            return path.name[: -len(suffix)] or "archive"
    return path.stem or "archive"


def download_and_extract(url: str, output_dir: Path, task_id: str) -> tuple[list[Path], list[Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"user-agent": "Mozilla/5.0"})
    try:
        response = build_opener().open(request, timeout=300)
    except HTTPError as exc:
        raise RuntimeError(f"下载文件失败：HTTP {exc.code}") from exc
    with response:
        disposition = response.headers.get("content-disposition", "")
        name = None
        match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
        if match:
            name = unquote(match.group(1))
        if not name:
            match = re.search(r'filename="?([^";]+)', disposition, re.I)
            if match:
                name = unquote(match.group(1))
        if not name:
            name = unquote(Path(urlparse(response.geturl()).path).name) or f"tmall_{task_id}.bin"
        destination = output_dir / Path(name).name
        if destination.exists():
            destination = output_dir / f"{destination.stem}_{task_id}{destination.suffix}"
        partial = destination.with_name(f".{destination.name}.part")
        try:
            with partial.open("wb") as target:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    saved = [destination]
    extracted: list[Path] = []
    extract_dir = output_dir / f"{archive_base_name(destination)}_extracted"
    if destination.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        # Already a usable workbook (e.g. the hosting all-item export); its
        # internal OOXML parts are not useful downloads, so do not unpack it.
        pass
    elif zipfile.is_zipfile(destination):
        extracted = safe_zip_extract(destination, extract_dir)
    elif tarfile.is_tarfile(destination):
        extracted = safe_tar_extract(destination, extract_dir)
    else:
        with destination.open("rb") as source:
            gzip_magic = source.read(2) == b"\x1f\x8b"
        if gzip_magic:
            extract_dir.mkdir(parents=True, exist_ok=True)
            result = extract_dir / archive_base_name(destination)
            with gzip.open(destination, "rb") as source, result.open("wb") as target:
                shutil.copyfileobj(source, target)
            extracted = [result]
        elif destination.name.lower().endswith((".zip", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".gz")):
            raise RuntimeError(f"下载文件看似压缩包但格式损坏或不受支持：{destination.name}")
    return saved, extracted


def safe_shop_prefix(shop_name: str | None) -> str:
    """Sanitize the shop name for use as a file-name prefix."""
    text = str(shop_name or "").strip()
    for char in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t'):
        text = text.replace(char, "-")
    return text.strip(" .-")


def default_pan_suan_output(
    output_dir: Path, date_filters: dict[str, dict[str, str]], shop_name: str | None = None,
) -> Path:
    key = "writeOffTime" if "writeOffTime" in date_filters else "orderPayTime"
    pair = date_filters[key]
    prefix = safe_shop_prefix(shop_name) or "盘算"
    return output_dir / f"{prefix}_{pair['start']}_{pair['end']}.xlsx"


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


def build_pan_suan(
    template: Path,
    output: Path,
    downloaded: list[Path],
    extracted: list[Path],
    expected_target: str,
    recalculate: bool = False,
    shop_name: str | None = None,
) -> dict[str, Any]:
    candidates = [p for p in extracted if p.is_file() and p.suffix.lower() == ".xlsx"]
    if not candidates:
        candidates = [p for p in downloaded if p.is_file() and p.suffix.lower() == ".xlsx"]
    if not candidates:
        raise RuntimeError("账单下载成功，但解压结果中没有可写入盘算的 xlsx 明细")
    source_template = output if output.exists() else template
    if not source_template.exists():
        raise RuntimeError(f"盘算模板不存在：{source_template}")
    python = find_spreadsheet_python()
    command = [
        str(python), str(PAN_SUAN_BUILDER),
        "--template", str(source_template),
        "--output", str(output),
        "--strict", "--expected-target", expected_target, "--json",
    ]
    if shop_name:
        command.extend(["--shop-name", shop_name])
    if not recalculate:
        command.append("--no-recalculate")
    for path in candidates:
        command.extend(["--input", str(path)])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"账单已保存，但写入盘算失败：{detail[-2000:]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("盘算构建脚本返回了无法解析的结果") from exc
    imported = result.get("imported_sheets", [])
    verified = result.get("verified_sheets", [])
    if expected_target not in imported or expected_target not in verified or not result.get("sheet_write_verified"):
        raise RuntimeError(f"账单写入后验证失败：预期Sheet {expected_target} 未通过校验")
    return result


def build_empty_pan_suan(
    template: Path, output: Path, expected_target: str, recalculate: bool = False,
    shop_name: str | None = None,
) -> dict[str, Any]:
    source_template = output if output.exists() else template
    if not source_template.exists():
        raise RuntimeError(f"盘算模板不存在：{source_template}")
    python = find_spreadsheet_python()
    command = [
        str(python), str(PAN_SUAN_BUILDER),
        "--template", str(source_template), "--output", str(output),
        "--strict", "--expected-target", expected_target, "--allow-empty-target", "--json",
    ]
    if shop_name:
        command.extend(["--shop-name", shop_name])
    if not recalculate:
        command.append("--no-recalculate")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"无数据流程清理目标Sheet失败：{detail[-2000:]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("盘算构建脚本返回了无法解析的结果") from exc
    imported = result.get("imported_sheets", [])
    verified = result.get("verified_sheets", [])
    stats = next((item for item in result.get("import_stats", []) if item.get("target") == expected_target), {})
    if expected_target not in imported or expected_target not in verified or stats.get("written_rows") != 0:
        raise RuntimeError(f"无数据流程验证失败：{expected_target} 未确认清空为0行")
    return result


def is_zero_total(total: Any) -> bool:
    if isinstance(total, bool) or total is None:
        return False
    try:
        return int(total) == 0 and float(total) == 0.0
    except (TypeError, ValueError):
        return False


def selected_module_keys(scope: str, skipped: list[str], completed: list[str] | None = None) -> list[str]:
    keys = list(MODULES) if scope == "all" else [scope]
    excluded = set(skipped)
    excluded.update(completed or [])
    return [key for key in keys if key not in excluded]


def state_path_for(args: argparse.Namespace, pan_output: Path) -> Path:
    if args.state_file:
        return args.state_file.expanduser().resolve()
    return pan_output.with_name(pan_output.stem + ".asset-inventory-state.json")


def load_resume_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"--resume 指定的状态文件不存在：{path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"续跑状态文件无效：{path}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("completedModules", []), list):
        raise ValueError(f"续跑状态文件格式不正确：{path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Persist progress without cookies, tokens, headers, or signed URLs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def validate_resume_dates(state: dict[str, Any], common_dates: dict[str, dict[str, str]]) -> None:
    expected = {key: common_dates[key] for key in ("writeOffTime", "orderPayTime") if key in common_dates}
    previous = state.get("dates")
    if previous and previous != expected:
        raise ValueError("续跑状态文件的结算/支付时间与当前 fetch 不一致，拒绝混合不同账期")


def module_output_dir(root: Path, module_key: str, date_filters: dict[str, dict[str, str]], multi: bool) -> Path:
    if not multi:
        return root
    key = "writeOffTime" if "writeOffTime" in date_filters else "orderPayTime"
    pair = date_filters[key]
    return root / f"asset-inventory-{module_key}-{pair['start'].replace('-', '')}-{pair['end'].replace('-', '')}"


def query_total(value: Any) -> Any:
    for item in walk(value):
        if isinstance(item, dict) and isinstance(item.get("paginator"), dict):
            total = item["paginator"].get("total")
            if total is not None:
                return total
    return None


CST = timezone(timedelta(hours=8))
PUNISHMENT_HEADERS = [
    "店铺", "类型", "处罚内容", "主订单号", "处罚时间", "违规类型", "违规事件",
    "商品id", "商品标题", "运营", "处罚金额",
]
# 类型列：punish=违规记录归为「处罚数据」，compensation=赔付记录归为「赔付追缴」
PUNISHMENT_TAB_LABELS = {"punish": "处罚数据", "compensation": "赔付追缴"}
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


def rules_center_epoch_ms(value: str, *, end: bool) -> int:
    """把 YYYY-MM-DD 转成 rules-center 需要的毫秒时间戳（北京时间）。"""
    parsed = date.fromisoformat(value)
    moment = datetime(parsed.year, parsed.month, parsed.day, tzinfo=CST)
    if end:
        moment = moment + timedelta(days=1) - timedelta(milliseconds=1)
    return int(moment.timestamp() * 1000)


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


def rules_center_item_id(item: dict[str, Any]) -> str | None:
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


def rules_center_time_text(item: dict[str, Any]) -> str | None:
    for key in ("punishTime", "gmtFinish", "gmtCreate"):
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return datetime.fromtimestamp(value / 1000.0, CST).strftime("%Y-%m-%d %H:%M:%S")
    return None


def rules_center_amount(item: dict[str, Any]) -> float:
    """接口金额单位为分，写入盘算按元。"""
    extend = rules_center_extend(item)
    for value in (item.get("punishAmount"), extend.get("amount")):
        if value is None or isinstance(value, bool):
            continue
        try:
            return round(float(value) / 100.0, 2)
        except (TypeError, ValueError):
            continue
    return 0.0


def rules_center_row(tab_type: str, item: dict[str, Any], shop_name: str | None) -> list[Any]:
    content = item.get("punishValue") or item.get("remark") or item.get("reason")
    return [
        shop_name,
        PUNISHMENT_TAB_LABELS.get(tab_type),
        content,
        text_identifier(item.get("orderNo")),
        rules_center_time_text(item),
        item.get("violationType"),
        item.get("violationEvent"),
        rules_center_item_id(item),
        None,
        None,
        rules_center_amount(item),
    ]


def fetch_rules_center_rows(
    session: Any,
    url: str,
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
        _, response = post(session, url, headers, payload)
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
            f"  {tab_type} 第 {page_no} 页：{len(data)} 条，累计 {len(rows)}/"
            f"{total if total is not None else '未知'} 条"
        )
        if not data:
            break
        if total is not None and len(rows) >= total:
            break
        if len(data) < RULES_CENTER_PAGE_SIZE:
            break
        page_no += 1
        if page_no > RULES_CENTER_MAX_PAGES:
            raise RuntimeError(f"{tab_type} 翻页超过 {RULES_CENTER_MAX_PAGES} 页仍未取满")
    return rows, total


def write_rules_center_workbook(path: Path, rows: list[list[Any]]) -> None:
    """用可导入 openpyxl 的 Python 生成明细 xlsx，供盘算构建脚本按 Sheet 表头导入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_file = path.with_name(path.stem + ".payload.json")
    payload_file.write_text(
        json.dumps(
            {"sheet": "处罚数据", "headers": PUNISHMENT_HEADERS, "rows": rows},
            ensure_ascii=False,
        ),
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
            raise RuntimeError(f"生成处罚数据明细失败：{detail[-1000:]}")
    finally:
        try:
            payload_file.unlink()
        except OSError:
            pass


def run_sequence(module_keys: list[str], runner: Any) -> list[dict[str, Any]]:
    """Run in fixed order; exceptions deliberately stop all later modules."""
    results: list[dict[str, Any]] = []
    for index, module_key in enumerate(module_keys):
        results.append(runner(module_key, index == len(module_keys) - 1))
    return results


def execute_export_module(
    args: argparse.Namespace,
    session: Any,
    base_headers: dict[str, str],
    module_key: str,
    date_filters: dict[str, dict[str, str]],
    pan_output: Path,
    recalculate: bool,
    multi: bool,
) -> dict[str, Any]:
    """Hosting download-task chain for 所有商品效果数据导出."""
    module = MODULES[module_key]
    print(f"\n=== {module['name']}（{module_key}）===")
    pair = date_filters["orderPayTime"]
    headers = {**base_headers, "referer": HOSTING_REFERER}
    create_query = urlencode({
        "bizType": module["biz_type"],
        "bizScene": module["biz_scene"],
        "startDate": pair["start"].replace("-", ""),
        "endDate": pair["end"].replace("-", ""),
    })
    created = get_json(session, f"{HOSTING_BASE}/task/createDownloadTask?{create_query}", headers)
    created_task_id = find_task_id(created)
    print(f"已创建导出任务：{created_task_id or '未知 taskId'}")
    time.sleep(HOSTING_POLL_SECONDS)

    poll_payload = {"pageNo": 1, "pageSize": 10}
    record: dict[str, Any] | None = None
    status = ""
    for attempt in range(1, HOSTING_MAX_POLLS + 1):
        _, status_response = post(session, f"{HOSTING_BASE}/task/pageListDownloadTask", headers, poll_payload)
        record = hosting_first_record(status_response)
        status = hosting_status(record) if record else ""
        print(f"轮询 {attempt}/{HOSTING_MAX_POLLS}：taskStatus={status or '未找到任务记录'}")
        if status == "FINISHED":
            break
        if status in {"FAIL", "FAILED", "ERROR", "CANCEL", "CANCELED", "CANCELLED"}:
            raise RuntimeError(f"导出任务失败：taskStatus={status}")
        if attempt < HOSTING_MAX_POLLS:
            time.sleep(HOSTING_POLL_SECONDS)
    else:
        raise TimeoutError(f"轮询 {HOSTING_MAX_POLLS} 次后导出任务仍未 FINISHED")

    if not record:
        raise RuntimeError("查询导出任务列表成功，但第一条没有任务记录")
    task_id = find_task_id(record) or created_task_id or "export"
    if not created_task_id:
        print(f"导出任务 taskId：{task_id}")
    file_url = hosting_file_url(record)
    if not file_url:
        raise RuntimeError("任务已 FINISHED，但第一条记录中没有 fileUrl")
    target_dir = module_output_dir(args.output_dir.expanduser().resolve(), module_key, date_filters, multi)
    saved, extracted = download_and_extract(file_url, target_dir, task_id)
    print("最终状态：FINISHED")
    print("原始文件：")
    for path in saved:
        print(path.resolve())
    print("解压文件：")
    for path in extracted:
        print(path.resolve())
    print(f"解压文件数量：{len(extracted)}")

    result: dict[str, Any] | None = None
    if not args.no_pan_suan:
        result = build_pan_suan(
            args.pan_suan_template.expanduser().resolve(), pan_output, saved, extracted,
            module["target_sheet"], recalculate=recalculate, shop_name=args.shop_name,
        )
        stats = next((item for item in result.get("import_stats", []) if item.get("target") == module["target_sheet"]), {})
        print(f"盘算文件：{pan_output}")
        print(f"已写入Sheet：{module['target_sheet']}")
        print(f"公司汇总商品数：{result.get('product_count', 0)}")
        print(f"Sheet写入验证：已通过（{module['target_sheet']}，{stats.get('written_rows', 0)} 行）")
        print(f"公式结果缓存：{'已生成（用户明确要求）' if result.get('formula_cache_recalculated') else '未重算；文件未自动打开'}")
        print("公司汇总固定表头：已保留")
        print("所有商品效果数据导出：已按本次导出写入并校验")
    else:
        print("盘算写入：已按 --no-pan-suan 跳过")

    return {
        "module": module_key,
        "name": module["name"],
        "bizType": module["biz_type"],
        "bizScene": module["biz_scene"],
        "dates": date_filters,
        "queryRows": None,
        "taskId": task_id,
        "outputDir": str(target_dir),
        "savedFiles": [str(path.resolve()) for path in saved],
        "extractedFiles": len(extracted),
        "targetSheet": module["target_sheet"],
        "writtenRows": (next((item for item in (result or {}).get("import_stats", []) if item.get("target") == module["target_sheet"]), {}).get("written_rows", 0)),
        "sheetWriteVerified": bool((result or {}).get("sheet_write_verified")) if not args.no_pan_suan else None,
    }


def execute_rules_center_module(
    args: argparse.Namespace,
    session: Any,
    base_headers: dict[str, str],
    module_key: str,
    date_filters: dict[str, dict[str, str]],
    pan_output: Path,
    recalculate: bool,
    multi: bool,
) -> dict[str, Any]:
    """处罚数据：违规记录(punish)+赔付记录(compensation) 两接口合并写入同一 Sheet。"""
    module = MODULES[module_key]
    pair = date_filters["orderPayTime"]
    print(f"\n=== {module['name']}（{module_key}）===")
    print(f"处罚时间范围：{pair['start']} ~ {pair['end']}")
    headers = {**base_headers, "referer": RULES_CENTER_REFERER}
    url = f"https://tgc.tmall.com{RULES_CENTER_PATH}"
    start_ms = rules_center_epoch_ms(pair["start"], end=False)
    end_ms = rules_center_epoch_ms(pair["end"], end=True)

    rows: list[list[Any]] = []
    totals: dict[str, int | None] = {}
    for tab_type in module["tab_types"]:
        label = "违规记录" if tab_type == "punish" else "赔付记录"
        print(f"获取{label}（tabType={tab_type}）：")
        items, total = fetch_rules_center_rows(session, url, headers, start_ms, end_ms, tab_type)
        totals[tab_type] = total
        print(f"  {label}合计：{len(items)} 条（接口总数 {total if total is not None else '未知'}）")
        if total is not None and len(items) < total:
            raise RuntimeError(f"{label}仅取到 {len(items)}/{total} 条，数据不完整，停止写入")
        rows.extend(rules_center_row(tab_type, item, args.shop_name) for item in items)

    result: dict[str, Any] | None = None
    target_dir = module_output_dir(args.output_dir.expanduser().resolve(), module_key, date_filters, multi)
    detail_path = target_dir / f"处罚数据_{pair['start']}_{pair['end']}.xlsx"
    write_rules_center_workbook(detail_path, rows)
    print(f"处罚数据明细：{detail_path}")
    print(f"合并后行数：{len(rows)}")

    if not args.no_pan_suan:
        result = build_pan_suan(
            args.pan_suan_template.expanduser().resolve(), pan_output, [], [detail_path],
            module["target_sheet"], recalculate=recalculate, shop_name=args.shop_name,
        )
        stats = next((item for item in result.get("import_stats", []) if item.get("target") == module["target_sheet"]), {})
        print(f"盘算文件：{pan_output}")
        print(f"已写入Sheet：{module['target_sheet']}")
        print(f"公司汇总商品数：{result.get('product_count', 0)}")
        print(f"处罚金额合计：{stats.get('amount_total', 0):.2f}")
        print(f"Sheet写入验证：已通过（{module['target_sheet']}，{stats.get('written_rows', 0)} 行）")
        print("公司汇总固定表头：已保留")
        print("所有商品效果数据导出：已保留，未覆盖")
    else:
        print("盘算写入：已按 --no-pan-suan 跳过")

    return {
        "module": module_key,
        "name": module["name"],
        "apiStyle": "rules-center",
        "tabTypes": list(module["tab_types"]),
        "dates": date_filters,
        "queryRows": sum(value or 0 for value in totals.values()),
        "tabTotals": totals,
        "taskId": None,
        "outputDir": str(target_dir),
        "savedFiles": [str(detail_path.resolve())],
        "extractedFiles": 1,
        "targetSheet": module["target_sheet"],
        "writtenRows": (next((item for item in (result or {}).get("import_stats", []) if item.get("target") == module["target_sheet"]), {}).get("written_rows", 0)),
        "sheetWriteVerified": bool((result or {}).get("sheet_write_verified")) if not args.no_pan_suan else None,
    }


def execute_module(
    args: argparse.Namespace,
    session: Any,
    base_headers: dict[str, str],
    module_key: str,
    date_filters: dict[str, dict[str, str]],
    pan_output: Path,
    recalculate: bool,
    multi: bool,
) -> dict[str, Any]:
    module = MODULES[module_key]
    if module.get("api_style") == "hosting":
        return execute_export_module(
            args, session, base_headers, module_key, date_filters, pan_output, recalculate, multi,
        )
    if module.get("api_style") == "rules-center":
        return execute_rules_center_module(
            args, session, base_headers, module_key, date_filters, pan_output, recalculate, multi,
        )
    bill_type = module["bill_type"]
    print(f"\n=== {module['name']}（{module_key}）===")
    headers = {**base_headers, "referer": make_referer(date_filters, bill_type)}
    query_payload = {
        "pageParam": {"itemsPerPage": 10, "page": 1},
        "param": {**date_filters},
        "billType": bill_type,
    }
    _, query = post(session, f"{BASE}/bill/queryBillDetail", headers, query_payload)
    total = query_total(query)
    print(f"查询成功：{total if total is not None else '未知'} 条")
    if is_zero_total(total):
        result: dict[str, Any] | None = None
        if not args.no_pan_suan:
            result = build_empty_pan_suan(
                args.pan_suan_template.expanduser().resolve(), pan_output, module["target_sheet"],
                recalculate=recalculate, shop_name=args.shop_name,
            )
            print(f"无数据：未创建下载任务；已确认 {module['target_sheet']} 为0行")
            print(f"盘算文件：{pan_output}")
        else:
            print("无数据：未创建下载任务")
        return {
            "module": module_key, "name": module["name"], "billType": bill_type,
            "dates": date_filters, "queryRows": 0, "taskId": None,
            "outputDir": None, "savedFiles": [], "extractedFiles": 0,
            "targetSheet": module["target_sheet"], "writtenRows": 0,
            "sheetWriteVerified": bool((result or {}).get("sheet_write_verified")) if not args.no_pan_suan else None,
            "noData": True,
        }

    create_payload = {"downloadType": DOWNLOAD_TYPE, "param": {**date_filters, "billType": bill_type}}
    _, created = post(session, f"{BASE}/download/download", headers, create_payload)
    task_id = find_task_id(created)
    if not task_id:
        raise RuntimeError("创建下载任务成功但响应中没有 taskId")
    print(f"已创建下载任务：{task_id}")

    center_referer = f"https://tgc.tmall.com/ds/page/supplier/download-service-center-next?taskId={task_id}&c2mNavigatorShellPage=7&c2mNavigatorPageOpener=0"
    poll_payload = {"pageParam": {"page": 1, "size": 10}, "taskId": task_id}
    started = time.monotonic()
    poll_headers = {**headers, "referer": center_referer}
    while True:
        _, status_response = post(session, f"{BASE}/download/pageQueryDownloadTask", poll_headers, poll_payload)
        state, detail = task_state(status_response, task_id)
        print(f"任务状态：{detail}")
        if state == "success":
            break
        if state == "failed":
            raise RuntimeError(f"下载任务失败：{detail}")
        remaining = args.max_wait_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError(f"等待下载任务超过 {args.max_wait_seconds} 秒")
        time.sleep(min(args.poll_seconds, remaining))

    _, download_response = post(session, f"{BASE}/download/getDownloadUrl", poll_headers, {"taskIdList": [task_id]})
    urls = find_urls(download_response)
    if not urls:
        raise RuntimeError("任务成功但没有找到下载地址")
    target_dir = module_output_dir(args.output_dir.expanduser().resolve(), module_key, date_filters, multi)
    saved, extracted = download_and_extract(urls[0], target_dir, task_id)
    print("最终状态：SUCCEED")
    print("原始文件：")
    for path in saved:
        print(path.resolve())
    print("解压文件：")
    for path in extracted:
        print(path.resolve())
    print(f"解压文件数量：{len(extracted)}")

    result: dict[str, Any] | None = None
    if not args.no_pan_suan:
        result = build_pan_suan(
            args.pan_suan_template.expanduser().resolve(), pan_output, saved, extracted,
            module["target_sheet"], recalculate=recalculate, shop_name=args.shop_name,
        )
        stats = next((item for item in result.get("import_stats", []) if item.get("target") == module["target_sheet"]), {})
        print(f"盘算文件：{pan_output}")
        print(f"已写入Sheet：{module['target_sheet']}")
        print(f"公司汇总商品数：{result.get('product_count', 0)}")
        print(f"未归属费用：{result.get('unassigned_rows', 0)} 行")
        print(f"Sheet写入验证：已通过（{module['target_sheet']}，{stats.get('written_rows', 0)} 行）")
        print(f"公式结果缓存：{'已生成（用户明确要求）' if result.get('formula_cache_recalculated') else '未重算；文件未自动打开'}")
        print("公司汇总固定表头：已保留")
        print("所有商品效果数据导出：已保留，未覆盖")
    else:
        print("盘算写入：已按 --no-pan-suan 跳过")

    return {
        "module": module_key,
        "name": module["name"],
        "billType": bill_type,
        "dates": date_filters,
        "queryRows": total,
        "taskId": task_id,
        "outputDir": str(target_dir),
        "savedFiles": [str(path.resolve()) for path in saved],
        "extractedFiles": len(extracted),
        "targetSheet": module["target_sheet"],
        "writtenRows": (next((item for item in (result or {}).get("import_stats", []) if item.get("target") == module["target_sheet"]), {}).get("written_rows", 0)),
        "sheetWriteVerified": bool((result or {}).get("sheet_write_verified")) if not args.no_pan_suan else None,
    }


def dry_run_plan(
    args: argparse.Namespace,
    query_url: str,
    body: dict[str, Any],
    module_keys: list[str],
    pan_output: Path,
    state_file: Path,
) -> dict[str, Any]:
    plans = []
    for key in module_keys:
        module = MODULES[key]
        dates = module_date_filters(args, body, module)
        if module.get("api_style") == "rules-center":
            pair = dates["orderPayTime"]
            plans.append({
                "module": key,
                "name": module["name"],
                "targetSheet": module["target_sheet"],
                "apiStyle": "rules-center",
                "dateKeys": list(module["date_keys"]),
                "dates": dates,
                "queryUrl": f"https://tgc.tmall.com{RULES_CENTER_PATH}",
                "tabTypes": list(module["tab_types"]),
                "pagePayload": [{
                    "pageNo": 1,
                    "pageSize": RULES_CENTER_PAGE_SIZE,
                    "startDateTime": rules_center_epoch_ms(pair["start"], end=False),
                    "endDateTime": rules_center_epoch_ms(pair["end"], end=True),
                    "tabType": tab,
                } for tab in module["tab_types"]],
            })
        elif module.get("api_style") == "hosting":
            pair = dates["orderPayTime"]
            create_url = f"{HOSTING_BASE}/task/createDownloadTask?{urlencode({
                'bizType': module['biz_type'],
                'bizScene': module['biz_scene'],
                'startDate': pair['start'].replace('-', ''),
                'endDate': pair['end'].replace('-', ''),
            })}"
            plans.append({
                "module": key,
                "name": module["name"],
                "targetSheet": module["target_sheet"],
                "bizType": module["biz_type"],
                "bizScene": module["biz_scene"],
                "dateKeys": list(module["date_keys"]),
                "dates": dates,
                "apiStyle": "hosting",
                "createTaskUrl": create_url,
                "pollPayload": {"pageNo": 1, "pageSize": 10},
                "pollIntervalSeconds": HOSTING_POLL_SECONDS,
                "maxPolls": HOSTING_MAX_POLLS,
                "downloadField": "fileUrl",
            })
        else:
            plans.append({
                "module": key,
                "name": module["name"],
                "targetSheet": module["target_sheet"],
                "billType": module["bill_type"],
                "dateKeys": list(module["date_keys"]),
                "dates": dates,
                "apiStyle": "fbi",
                "queryPayload": {
                    "pageParam": {"itemsPerPage": 10, "page": 1},
                    "param": {**dates},
                    "billType": module["bill_type"],
                },
            })
    return {
        "scope": args.module,
        "queryUrl": query_url,
        "downloadType": DOWNLOAD_TYPE,
        "moduleCount": len(plans),
        "modules": plans,
        "skippedModules": list(dict.fromkeys(args.skip_module)),
        "resume": args.resume,
        "pollSeconds": args.poll_seconds,
        "maxWaitSeconds": args.max_wait_seconds,
        "outputDir": str(args.output_dir.expanduser().resolve()),
        "shopName": args.shop_name,
        "panSuanEnabled": not args.no_pan_suan,
        "panSuanTemplate": str(args.pan_suan_template.expanduser().resolve()),
        "panSuanOutput": str(pan_output),
        "stateFile": str(state_file),
        "network": False,
    }


def load_query_source(args: argparse.Namespace) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """有 fetch 文件就用它；没有则复用本机浏览器登录态，日期全部由命令行给出。"""
    if args.fetch_file is None:
        return f"https://tgc.tmall.com{QUERY_PATH}", None, {}
    text = args.fetch_file.read_text(encoding="utf-8")
    fetches = parse_fetches(text)
    if not fetches:
        raise ValueError("没有找到 fetch 请求")
    query_url, query_request = fetches[0]
    validate_fetch_source(query_url, query_request)
    return query_url, query_request, json_body(query_request)


def main() -> int:
    args = parse_args()
    validate_runtime_options(args)
    query_url, query_request, body = load_query_source(args)

    # 全流程默认按第一个模块的日期需求校验（结算+支付两类）；仅结算流程的
    # 结算区间本来就取自支付日期，所以这里的 common_dates 仍是 writeOffTime/orderPayTime 两把。
    scope_module = MODULES[next(iter(MODULES))] if args.module == "all" else MODULES[args.module]
    common_dates = module_date_filters(args, body, scope_module)
    pan_output = (
        args.pan_suan_output or default_pan_suan_output(args.output_dir, common_dates, args.shop_name)
    ).expanduser().resolve()
    state_file = state_path_for(args, pan_output)
    resume_state: dict[str, Any] = {}
    completed: list[str] = []
    persisted_skips: list[str] = []
    if args.resume:
        resume_state = load_resume_state(state_file)
        validate_resume_dates(resume_state, common_dates)
        completed = [key for key in resume_state.get("completedModules", []) if key in MODULES]
        persisted_skips = [key for key in resume_state.get("manualSkippedModules", []) if key in MODULES]

    effective_skips = list(dict.fromkeys([*persisted_skips, *args.skip_module]))
    module_keys = selected_module_keys(args.module, effective_skips, completed)
    if args.dry_run:
        print(json.dumps(dry_run_plan(args, query_url, body, module_keys, pan_output, state_file), ensure_ascii=False, indent=2))
        return 0
    if not module_keys:
        print("没有待执行模块：所选流程均已完成或被 --skip-module 跳过。")
        print(f"盘算文件：{pan_output}")
        return 0

    # Validate every module's date payload before making the first network request.
    dates_by_module = {key: module_date_filters(args, body, MODULES[key]) for key in module_keys}
    base_headers = headers_from(query_request) if query_request else load_browser_headers()
    if not base_headers.get("cookie"):
        raise ValueError("最新 fetch 中没有 Cookie，无法复用登录状态")
    session = build_opener()
    multi = args.module == "all" or len(module_keys) > 1
    state_dates = {key: common_dates[key] for key in ("writeOffTime", "orderPayTime") if key in common_dates}
    state = {
        "version": 1,
        "shopName": args.shop_name,
        "dates": state_dates,
        "panSuanOutput": str(pan_output),
        "completedModules": list(dict.fromkeys(completed)),
        "manualSkippedModules": effective_skips,
        "results": dict(resume_state.get("results", {})) if isinstance(resume_state.get("results", {}), dict) else {},
    }

    def run_one(module_key: str, is_last: bool) -> dict[str, Any]:
        try:
            result = execute_module(
                args, session, base_headers, module_key, dates_by_module[module_key], pan_output,
                # Do not open/re-save with a spreadsheet engine after completion.
                recalculate=False, multi=multi,
            )
        except Exception as exc:
            state["failedModule"] = module_key
            state["failure"] = safe_summary(str(exc))
            save_state(state_file, state)
            finished = ", ".join(MODULES[key]["name"] for key in state["completedModules"]) or "无"
            print(f"已完成模块已保留：{finished}", file=sys.stderr)
            print(f"续跑状态：{state_file}", file=sys.stderr)
            raise
        state["completedModules"].append(module_key)
        state["completedModules"] = list(dict.fromkeys(state["completedModules"]))
        state["results"][module_key] = result
        state.pop("failedModule", None)
        state.pop("failure", None)
        save_state(state_file, state)
        return result

    results = run_sequence(module_keys, run_one)
    print(f"\n全部待执行流程完成：{len(results)} 个")
    print("本次完成：" + "、".join(item["name"] for item in results))
    if not args.no_pan_suan:
        print(f"盘算文件：{pan_output}")
    print(f"续跑状态：{state_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, TimeoutError, HTTPError, URLError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
