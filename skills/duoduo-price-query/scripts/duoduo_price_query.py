#!/usr/bin/env python3
import argparse
import codecs
import csv
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.sax.saxutils import escape


API_URL = "https://mc.pinduoduo.com/orianna-mms/goods/schedule/supplierPrice/query"
REFERER = "https://mc.pinduoduo.com/ddmc-supplier-product/goods-schedule"
EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)
EDGE_SEC_CH_UA = '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"'
CSV_FIELDNAMES = [
    "商品id",
    "商品名称",
    "总数",
    "剩余数量",
    "销售数量",
    "价格",
    "活动价",
    "销售单价",
    "活动数量",
    "仓库",
]


class DuoduoPriceError(Exception):
    pass


def cents_to_yuan(value):
    if value is None:
        return "0"
    try:
        cents = int(value)
    except (TypeError, ValueError):
        raise DuoduoPriceError(f"price value is not numeric: {value!r}")
    return f"{cents / 100:.2f}"


def optional_cents_to_yuan(value):
    if value is None or value == "":
        return ""
    return cents_to_yuan(value)


def yuan_to_decimal(value):
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise DuoduoPriceError(f"yuan value is not numeric: {value!r}") from exc


def format_yuan(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_sale_unit_price(price, activity_price, activity_quantity, sold_quantity):
    activity_price_value = yuan_to_decimal(activity_price)
    try:
        activity_quantity_value = int(activity_quantity or 0)
        sold_quantity_value = int(sold_quantity or 0)
    except (TypeError, ValueError) as exc:
        raise DuoduoPriceError(f"quantity value is not numeric: {exc}") from exc

    if activity_quantity_value > 0 and sold_quantity_value > 0:
        regular_quantity = sold_quantity_value - activity_quantity_value
        if regular_quantity > 0:
            if price is None or price == "":
                return ""
            price_value = yuan_to_decimal(price)
            total = activity_quantity_value * activity_price_value + regular_quantity * price_value
            return format_yuan(total / sold_quantity_value)
        return format_yuan(activity_price_value)
    if price is None or price == "":
        return ""
    price_value = yuan_to_decimal(price)
    return format_yuan(price_value)


def query_supplier_price(cookie, goods_id, biz_date, area_id, anti_content=None, verify_auth_token=None, timeout=20):
    """Query supplier price for one goodsId. Returns a yuan string such as '17.20'."""
    if not cookie:
        raise DuoduoPriceError("cookie is required")

    body = json.dumps(
        {"bizDate": biz_date, "goodsId": int(goods_id), "areaId": int(area_id)},
        separators=(",", ":"),
    ).encode("utf-8")

    headers = {
        "sec-ch-ua-platform": '"Windows"',
        "User-Agent": EDGE_UA,
        "sec-ch-ua": EDGE_SEC_CH_UA,
        "Content-Type": "application/json",
        "sec-ch-ua-mobile": "?0",
        "Accept": "*/*",
        "Origin": "https://mc.pinduoduo.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": REFERER,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Cookie": cookie,
    }
    if anti_content:
        headers["anti-content"] = anti_content
    if verify_auth_token:
        headers["verifyauthtoken"] = verify_auth_token

    request = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DuoduoPriceError(f"HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise DuoduoPriceError(f"request failed: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DuoduoPriceError(f"non-JSON response: {raw[:300]}") from exc

    if not payload.get("success"):
        code = payload.get("errorCode", payload.get("error_code"))
        msg = payload.get("errorMsg", payload.get("error_msg"))
        raise DuoduoPriceError(f"API error for goodsId={goods_id}: {code} {msg}")

    result = payload.get("result") or {}
    if "supplierPrice" not in result:
        raise DuoduoPriceError(f"missing supplierPrice for goodsId={goods_id}: {payload}")
    return cents_to_yuan(result["supplierPrice"])


def js_string_unescape(value):
    """Best-effort unescape for JS string literals captured from request snippets."""
    try:
        return codecs.decode(value, "unicode_escape")
    except Exception:
        return value.replace(r"\"", '"').replace(r"\'", "'").replace(r"\\", "\\")


def extract_request_context(text):
    """Extract cookie, anti-content, verify token, and body fields from fetch/axios snippets."""
    context = {}
    if not text:
        return context

    header_pattern = re.compile(
        r"""(?is)(["']?)(cookie|anti-content|verifyauthtoken|verifyAuthToken)\1\s*:\s*(["'`])((?:\\.|(?!\3).)*?)\3"""
    )
    for match in header_pattern.finditer(text):
        key = match.group(2).lower()
        value = js_string_unescape(match.group(4))
        if key == "cookie":
            context["cookie"] = value
        elif key == "anti-content":
            context["anti_content"] = value
        elif key in {"verifyauthtoken", "verifyauthtoken".lower()}:
            context["verify_auth_token"] = value

    body_pattern = re.compile(r"""(?is)(["']?)body\1\s*:\s*(["'`])((?:\\.|(?!\2).)*?)\2""")
    body_match = body_pattern.search(text)
    if body_match:
        body_text = js_string_unescape(body_match.group(3))
        try:
            body = json.loads(body_text)
            if isinstance(body, dict):
                if body.get("bizDate"):
                    context["biz_date"] = body["bizDate"]
                if body.get("areaId") is not None:
                    context["area_id"] = body["areaId"]
        except json.JSONDecodeError:
            pass

    return context


def extract_fetch_request(text):
    context = extract_request_context(text)
    if not text:
        return context

    url_match = re.search(r"""(?is)\bfetch\s*\(\s*(["'`])((?:\\.|(?!\1).)*?)\1""", text)
    if url_match:
        context["url"] = js_string_unescape(url_match.group(2))

    method_match = re.search(r"""(?is)(["']?)method\1\s*:\s*(["'`])((?:\\.|(?!\2).)*?)\2""", text)
    if method_match:
        context["method"] = js_string_unescape(method_match.group(3)).upper()

    body_match = re.search(r"""(?is)(["']?)body\1\s*:\s*(["'`])((?:\\.|(?!\2).)*?)\2""", text)
    if body_match:
        context["body"] = js_string_unescape(body_match.group(3))

    headers = {}
    header_pattern = re.compile(r"""(?is)(["']?)([A-Za-z0-9_-]+)\1\s*:\s*(["'`])((?:\\.|(?!\3).)*?)\3""")
    ignored = {"body", "method"}
    for match in header_pattern.finditer(text):
        key = match.group(2)
        if key.lower() in ignored:
            continue
        headers[key] = js_string_unescape(match.group(4))
    if headers:
        context["headers"] = headers

    return context


def read_request_snippet(path):
    if not path:
        return ""
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def prepare_request(fetch_context, default_headers=None):
    url = fetch_context.get("url")
    if not url:
        raise DuoduoPriceError("request snippet URL is missing")

    body = fetch_context.get("body")
    data = body.encode("utf-8") if body is not None else None
    method = (fetch_context.get("method") or ("POST" if data is not None else "GET")).upper()
    headers = dict(fetch_context.get("headers") or {})
    for transport_header in ["content-length", "connection", "host"]:
        for key in list(headers):
            if key.lower() == transport_header:
                headers.pop(key, None)
    for key, value in (default_headers or {}).items():
        if key.lower() not in {header.lower() for header in headers}:
            headers[key] = value
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def query_goods_list(fetch_context, timeout=20):
    header_defaults = {
        "User-Agent": EDGE_UA,
        "sec-ch-ua": EDGE_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "Origin": "https://mc.pinduoduo.com",
        "Referer": REFERER,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    request = prepare_request(fetch_context, default_headers=header_defaults)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DuoduoPriceError(f"goods list HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise DuoduoPriceError(f"goods list request failed: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DuoduoPriceError(f"goods list non-JSON response: {raw[:300]}") from exc

    if isinstance(payload, dict) and payload.get("success") is False:
        code = payload.get("errorCode", payload.get("error_code"))
        msg = payload.get("errorMsg", payload.get("error_msg"))
        raise DuoduoPriceError(f"goods list API error: {code} {msg}")
    return payload


def iter_candidate_dicts(value, path="$"):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from iter_candidate_dicts(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_candidate_dicts(child, f"{path}[{index}]")


def is_record(value):
    required = {"goodsId", "goodsName", "totalQuantity", "quantity"}
    return isinstance(value, dict) and required.issubset(value.keys())


def looks_like_broken_record(value):
    keys = {"goodsId", "goodsName", "totalQuantity", "quantity"}
    return isinstance(value, dict) and bool(keys.intersection(value.keys())) and not is_record(value)


def first_list_dict(value, key):
    items = value.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def normalize_record(record):
    try:
        total_quantity = int(record["totalQuantity"])
        quantity = int(record["quantity"])
    except (TypeError, ValueError, KeyError) as exc:
        raise DuoduoPriceError(f"quantity fields are invalid: {exc}") from exc

    activity = first_list_dict(record, "activityIntents")
    warehouse = first_list_dict(record, "warehouseGroupVOList")
    return {
        "goodsId": record["goodsId"],
        "goodsName": record["goodsName"],
        "totalQuantity": total_quantity,
        "quantity": quantity,
        "soldQuantity": total_quantity - quantity,
        "activityPrice": cents_to_yuan(activity.get("supplierPrice", 0)),
        "activityQuantity": activity.get("currentTotalQuantity", 0) or 0,
        "warehouse": warehouse.get("warehouseGroupName", "") or "",
    }


def extract_records(payload):
    records = []
    broken = []
    seen_paths = set()
    for path, value in iter_candidate_dicts(payload):
        if is_record(value):
            if path not in seen_paths:
                records.append((path, value))
                seen_paths.add(path)
        elif looks_like_broken_record(value):
            broken.append((path, sorted(set(["goodsId", "goodsName", "totalQuantity", "quantity"]) - set(value.keys()))))
    return records, broken


def extract_top_level_records(payload):
    records = []
    broken = []
    if isinstance(payload, list):
        candidates = [(f"$[{index}]", value) for index, value in enumerate(payload)]
    else:
        candidates = [("$", payload)]

    for path, value in candidates:
        if is_record(value):
            records.append((path, value))
        elif looks_like_broken_record(value):
            broken.append((path, sorted(set(["goodsId", "goodsName", "totalQuantity", "quantity"]) - set(value.keys()))))
    return records, broken


def extract_record_list_records(payload):
    records = []
    broken = []
    seen_paths = set()

    def walk(value, path="$"):
        if isinstance(value, list):
            if any(is_record(item) for item in value if isinstance(item, dict)):
                for index, item in enumerate(value):
                    item_path = f"{path}[{index}]"
                    if is_record(item):
                        if item_path not in seen_paths:
                            records.append((item_path, item))
                            seen_paths.add(item_path)
                    elif looks_like_broken_record(item):
                        missing = sorted(set(["goodsId", "goodsName", "totalQuantity", "quantity"]) - set(item.keys()))
                        broken.append((item_path, missing))
                return
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}")

    walk(payload)
    return records, broken


def infer_unique_record_value(records, key):
    values = {record.get(key) for _, record in records if record.get(key) is not None}
    if len(values) == 1:
        return next(iter(values))
    return None


def load_price_checkpoint(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    prices = data.get("prices", {})
    if not isinstance(prices, dict):
        raise DuoduoPriceError(f"checkpoint prices must be an object: {path}")
    return {str(goods_id): price for goods_id, price in prices.items()}


def save_price_checkpoint(path, prices):
    if not path:
        return
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".duoduo-price-checkpoint-", suffix=".json", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"prices": prices}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def sleep_between_queries(index, sleep_value, sleep_min=None, sleep_max=None):
    if index <= 0:
        return
    if sleep_min is not None or sleep_max is not None:
        low = sleep_value if sleep_min is None else sleep_min
        high = low if sleep_max is None else sleep_max
        if high < low:
            raise DuoduoPriceError("--sleep-max must be greater than or equal to --sleep-min")
        delay = random.uniform(max(low, 0), max(high, 0))
        print(f"sleeping {delay:.1f}s before next uncached price query", file=sys.stderr, flush=True)
        time.sleep(delay)
        return
    time.sleep(max(sleep_value, 0))


def is_frequency_error(exc):
    text = str(exc)
    return "54001" in text or "操作太过频繁" in text


def query_supplier_price_with_retry(
    cookie,
    goods_id,
    biz_date,
    area_id,
    anti_content=None,
    verify_auth_token=None,
    cooldown=90,
    max_retries=0,
):
    attempts = 0
    while True:
        retry_after_followup = False
        try:
            result = query_supplier_price(
                cookie,
                goods_id,
                biz_date,
                area_id,
                anti_content=anti_content,
                verify_auth_token=verify_auth_token,
            )
            return result
        except Exception as exc:
            attempts += 1
            if is_frequency_error(exc) and max_retries <= 0:
                raise DuoduoPriceError(
                    f"frequency limit for goodsId={goods_id}; stop now and wait for a fresh logged-in "
                    f"request snippet or new cookie. API response: {exc}"
                ) from exc
            if is_frequency_error(exc) and attempts <= max_retries:
                print(
                    f"frequency limit for goodsId={goods_id}; cooling down {cooldown:g}s "
                    f"then retry {attempts}/{max_retries}",
                    file=sys.stderr,
                    flush=True,
                )
                retry_after_followup = True
            else:
                raise
        if retry_after_followup:
            time.sleep(max(cooldown, 0))
            continue
        return result


def write_csv(rows, output_path):
    target_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".duoduo-price-", suffix=".csv", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        os.replace(temp_path, output_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def column_letter(index):
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def worksheet_xml(rows):
    all_rows = [CSV_FIELDNAMES] + [[row.get(field, "") for field in CSV_FIELDNAMES] for row in rows]
    row_xml = []
    for row_index, values in enumerate(all_rows, start=1):
        cells = []
        for column_index, value in enumerate(values, start=1):
            reference = f"{column_letter(column_index)}{row_index}"
            text = escape("" if value is None else str(value))
            cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{text}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    last_cell = f"{column_letter(len(CSV_FIELDNAMES))}{len(all_rows)}"
    widths = {
        "A": 16,
        "B": 34,
        "C": 10,
        "D": 10,
        "E": 10,
        "F": 10,
        "G": 10,
        "H": 10,
        "I": 10,
        "J": 18,
    }
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, (_letter, width) in enumerate(widths.items(), start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last_cell}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols>"
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        '<autoFilter ref="A1:J1"/>'
        "</worksheet>"
    )


def write_xlsx(rows, output_path):
    target_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".duoduo-price-", suffix=".xlsx", dir=target_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
            workbook.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                "</Types>",
            )
            workbook.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="xl/workbook.xml"/>'
                "</Relationships>",
            )
            workbook.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="多多价格" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>",
            )
            workbook.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/>'
                "</Relationships>",
            )
            workbook.writestr("xl/worksheets/sheet1.xml", worksheet_xml(rows))
        os.replace(temp_path, output_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def default_output_path(output_path, biz_date):
    if output_path:
        return output_path
    suffix = biz_date or time.strftime("%Y-%m-%d")
    return f"duoduo_prices_{suffix}.csv"


def default_excel_output_path(excel_output_path, csv_output_path):
    if excel_output_path:
        return excel_output_path
    root, _ext = os.path.splitext(csv_output_path)
    return f"{root}.xlsx"


def default_checkpoint_path(checkpoint_path, output_path):
    if checkpoint_path:
        return checkpoint_path
    root, _ext = os.path.splitext(output_path)
    return f"{root}.checkpoint.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Query Duoduo supplier prices and export CSV/XLSX.")
    parser.add_argument("json_file", nargs="?", help="input JSON file, or '-' for stdin")
    parser.add_argument("-o", "--output", help="output CSV path; defaults to duoduo_prices_<bizDate>.csv when --fetch-goods-list is used")
    parser.add_argument("--excel-output", help="output XLSX path; defaults to the CSV path with .xlsx extension")
    parser.add_argument("--no-excel", action="store_true", help="only write CSV and skip the default XLSX export")
    parser.add_argument("--cookie", default=os.environ.get("DUODUO_COOKIE"), help="Pinduoduo cookie, or DUODUO_COOKIE")
    parser.add_argument("--anti-content", default=os.environ.get("DUODUO_ANTI_CONTENT"), help="anti-content header, or DUODUO_ANTI_CONTENT")
    parser.add_argument("--verify-auth-token", default=os.environ.get("DUODUO_VERIFY_AUTH_TOKEN"), help="verifyauthtoken header, or DUODUO_VERIFY_AUTH_TOKEN")
    parser.add_argument("--request-snippet", help="text file containing a browser fetch/axios/request snippet to extract cookie and headers; use '-' for stdin")
    parser.add_argument(
        "--price-request-snippet",
        help="text file containing a browser fetch/axios/request snippet used only for price-query headers/cookie; use '-' for stdin",
    )
    parser.add_argument("--biz-date", help="business date, e.g. 2026-05-02")
    parser.add_argument("--area-id", type=int, help="areaId for price query")
    parser.add_argument("--top-level-only", action="store_true", help="only parse top-level goods records and ignore nested helper objects")
    parser.add_argument("--record-list-only", action="store_true", help="parse complete goods records from the first matching record lists and ignore nested helper objects")
    parser.add_argument("--fetch-goods-list", action="store_true", help="call the goods list API from --request-snippet and use its JSON response as input")
    parser.add_argument("--preview-no-api", action="store_true", help="write CSV without querying prices; use supplierPrice from JSON when present")
    parser.add_argument("--sleep", type=float, default=1.0, help="seconds to sleep between uncached price calls when --sleep-min/--sleep-max are not used")
    parser.add_argument("--sleep-min", type=float, default=3.0, help="minimum random seconds between uncached price calls")
    parser.add_argument("--sleep-max", type=float, default=5.0, help="maximum random seconds between uncached price calls")
    parser.add_argument("--random-query-order", action="store_true", help="query uncached goods prices in random order, then write CSV in record order")
    parser.add_argument("--random-seed", type=int, help="seed for random query ordering and random sleep delays")
    parser.add_argument("--cooldown", type=float, default=90.0, help="seconds to wait after a 54001 response when --max-retries is greater than 0")
    parser.add_argument("--max-retries", type=int, default=0, help="frequency-limit retries per goodsId before failing; default stops immediately")
    parser.add_argument("--checkpoint", help="JSON file used to cache successful prices for resumable runs; never stores cookies")
    args = parser.parse_args(argv)
    if args.random_seed is not None:
        random.seed(args.random_seed)

    snippet_context = {}
    price_snippet_context = {}
    if args.request_snippet:
        try:
            snippet_text = read_request_snippet(args.request_snippet)
            snippet_context = extract_fetch_request(snippet_text) if args.fetch_goods_list else extract_request_context(snippet_text)
        except Exception as exc:
            print(f"ERROR: cannot read request snippet: {exc}", file=sys.stderr)
            return 2
    if args.price_request_snippet:
        try:
            price_snippet_text = read_request_snippet(args.price_request_snippet)
            price_snippet_context = extract_request_context(price_snippet_text)
        except Exception as exc:
            print(f"ERROR: cannot read price request snippet: {exc}", file=sys.stderr)
            return 2
    cookie = args.cookie or price_snippet_context.get("cookie") or snippet_context.get("cookie")
    anti_content = args.anti_content or price_snippet_context.get("anti_content") or snippet_context.get("anti_content")
    verify_auth_token = (
        args.verify_auth_token
        or price_snippet_context.get("verify_auth_token")
        or snippet_context.get("verify_auth_token")
    )
    biz_date = args.biz_date or price_snippet_context.get("biz_date") or snippet_context.get("biz_date")
    area_id = args.area_id or price_snippet_context.get("area_id") or snippet_context.get("area_id")

    if not args.preview_no_api and not cookie:
        print("ERROR: cookie is required. Pass --cookie, set DUODUO_COOKIE, or provide --request-snippet.", file=sys.stderr)
        return 2
    try:
        if args.fetch_goods_list:
            payload = query_goods_list(snippet_context)
        elif args.json_file == "-":
            payload = json.load(sys.stdin)
        elif args.json_file:
            with open(args.json_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        else:
            print("ERROR: json_file is required unless --fetch-goods-list is used.", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"ERROR: cannot read JSON: {exc}", file=sys.stderr)
        return 2

    if args.fetch_goods_list or args.record_list_only:
        records, broken = extract_record_list_records(payload)
    elif args.top_level_only:
        records, broken = extract_top_level_records(payload)
    else:
        records, broken = extract_records(payload)
    if broken:
        print("ERROR: found entries that look like goods records but are missing required fields:", file=sys.stderr)
        for path, missing in broken[:50]:
            print(f"- {path}: missing {', '.join(missing)}", file=sys.stderr)
        if len(broken) > 50:
            print(f"- ... {len(broken) - 50} more", file=sys.stderr)
        return 3
    if not records:
        print("ERROR: no goods records found.", file=sys.stderr)
        return 3
    if not biz_date:
        biz_date = infer_unique_record_value(records, "bizDate")
    if area_id is None:
        area_id = infer_unique_record_value(records, "areaId")
    if not args.preview_no_api and not biz_date:
        print("ERROR: biz date is required. Pass --biz-date, provide a request snippet body containing bizDate, or use JSON with one unique bizDate.", file=sys.stderr)
        return 2
    if not args.preview_no_api and area_id is None:
        print("ERROR: areaId is required. Pass --area-id, provide a request snippet body containing areaId, or use JSON with one unique areaId.", file=sys.stderr)
        return 2

    output_path = default_output_path(args.output, biz_date)
    excel_output_path = None if args.no_excel else default_excel_output_path(args.excel_output, output_path)
    checkpoint_path = default_checkpoint_path(args.checkpoint, output_path)

    rows = []
    price_cache = {}
    try:
        price_cache = load_price_checkpoint(checkpoint_path)
        if args.preview_no_api:
            for _path, record in records:
                price_cache[str(record["goodsId"])] = optional_cents_to_yuan(record.get("supplierPrice"))
        else:
            missing_records = [
                (index, path, record)
                for index, (path, record) in enumerate(records)
                if str(record["goodsId"]) not in price_cache
            ]
            if args.random_query_order:
                random.shuffle(missing_records)
                if missing_records:
                    print("random price query order:", file=sys.stderr, flush=True)
                    for original_index, _path, record in missing_records:
                        print(
                            f"- original_index={original_index + 1} goodsId={record['goodsId']} "
                            f"goodsName={record.get('goodsName', '')}",
                            file=sys.stderr,
                            flush=True,
                        )
            for query_index, (_original_index, _path, record) in enumerate(missing_records):
                sleep_between_queries(query_index, args.sleep, args.sleep_min, args.sleep_max)
                goods_id = str(record["goodsId"])
                price_cache[goods_id] = query_supplier_price_with_retry(
                    cookie,
                    goods_id,
                    biz_date,
                    area_id,
                    anti_content=anti_content,
                    verify_auth_token=verify_auth_token,
                    cooldown=args.cooldown,
                    max_retries=args.max_retries,
                )
                save_price_checkpoint(checkpoint_path, price_cache)
        for path, record in records:
            normalized = normalize_record(record)
            goods_id = str(normalized["goodsId"])
            if goods_id not in price_cache:
                raise DuoduoPriceError(f"missing price for goodsId={goods_id}")
            rows.append(
                {
                    "商品id": normalized["goodsId"],
                    "商品名称": normalized["goodsName"],
                    "总数": normalized["totalQuantity"],
                    "剩余数量": normalized["quantity"],
                    "销售数量": normalized["soldQuantity"],
                    "价格": price_cache[goods_id],
                    "活动价": normalized["activityPrice"],
                    "销售单价": calculate_sale_unit_price(
                        price_cache[goods_id],
                        normalized["activityPrice"],
                        normalized["activityQuantity"],
                        normalized["soldQuantity"],
                    ),
                    "活动数量": normalized["activityQuantity"],
                    "仓库": normalized["warehouse"],
                }
            )
    except Exception as exc:
        try:
            save_price_checkpoint(checkpoint_path, price_cache)
        except Exception as checkpoint_exc:
            print(f"ERROR: failed to save checkpoint: {checkpoint_exc}", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        if not args.preview_no_api:
            print(
                "PRICE QUERY FAILED: ask the user for a fresh logged-in request snippet or new cookie, "
                "then rerun with the same checkpoint.",
                file=sys.stderr,
            )
        return 4

    write_csv(rows, output_path)
    if excel_output_path:
        write_xlsx(rows, excel_output_path)
    save_price_checkpoint(checkpoint_path, price_cache)
    print(f"Wrote {len(rows)} rows to {output_path}")
    if excel_output_path:
        print(f"Wrote Excel workbook to {excel_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
