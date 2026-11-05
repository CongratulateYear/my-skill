"""Conservative 聚水潭 order export client (no cookie acquisition).

Live callbacks use export-log auto_id, never callback RequestId, as task identity.
Unknown envelopes fail closed. Inject session and synthetic responses for tests.
No response body, download URL, or authentication material is persisted or reported.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import stat
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ERP_ORIGIN = "https://www.erp321.com"
EXPORT_URL = ERP_ORIGIN + "/app/order/export/exportorder.aspx?_c=jst-epaas&epaas=true"
LOG_URL = ERP_ORIGIN + "/app/common/export/export_log.aspx"
# Observed ERP `created` values are Shanghai wall time, NOT the host's local
# timezone (which can differ on Windows). China has no DST in these dates.
ERP_TIMEZONE = timezone(timedelta(hours=8))
TASK_CLOCK_SKEW_SECONDS = 120


def _created_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        raise ExportError("新日志 created 缺失或格式未知，无法关联任务")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ERP_TIMEZONE).timestamp()
    except ValueError:
        raise ExportError("新日志 created 时间无效，无法关联任务") from None


class ExportError(RuntimeError):
    """Safe diagnostic; never includes a server body or signed URL."""


class MissingOrdersError(ExportError):
    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__("查询缺失订单: " + ", ".join(self.missing))


@dataclass(frozen=True)
class ResponseSchema:
    """Explicitly adjustable only after checking a real response, never by guessing.

    A row must expose so_id (or configured order_id_field) to prove coverage.
    A URL alone is NOT proof of success. A recognized success status is required.
    Numeric task status meanings intentionally have no defaults.
    """
    order_id_field: str = "so_id"
    internal_id_field: str = "o_id"
    request_id_fields: tuple = ("RequestId",)
    status_fields: tuple = ("status", "Status")
    url_fields: tuple = ("download_url", "DownloadUrl")
    pending_statuses: tuple = ("pending", "queued", "running", "processing")
    success_statuses: tuple = ("success", "completed")
    failure_statuses: tuple = ("failed", "error", "cancelled")
    total_fields: tuple = ("total", "totalCount", "Total")


class _HiddenParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.password = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "input":
            return
        attrs = dict(attrs)
        typ = attrs.get("type", "").lower()
        if typ == "password":
            self.password = True
        if typ == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")


def _checked_response(response):
    if getattr(response, "status_code", 0) != 200:
        raise ExportError("HTTP 响应非 200；可能登录失效或服务异常")
    return response


def _request(session, method, url, **kwargs):
    # Never follow redirects with an authenticated transport.
    try:
        return _checked_response(getattr(session, method)(url, allow_redirects=False, **kwargs))
    except ExportError:
        raise
    except Exception:
        raise ExportError("HTTP transport 调用失败（敏感异常详情已隐藏）") from None


def get_hidden(session, headers, url=EXPORT_URL, timeout=30):
    response = _request(session, "get", url, headers=dict(headers), timeout=timeout)
    parser = _HiddenParser()
    parser.feed(response.text)
    if parser.password or not all(k in parser.fields for k in ("__VIEWSTATE", "__VIEWSTATEGENERATOR")):
        raise ExportError("页面无有效动态 VIEWSTATE，可能是登录页面；停止导出")
    return parser.fields


def _json_decode(text):
    # Decimal preserves non-ID business values; _identifier rejects Decimal IDs.
    return json.loads(text, parse_int=str, parse_float=Decimal)


def _decode(value):
    """Unwrap JSON strings and documented-style callback length framing.

    Supports N|<N chars payload> and N|<N chars eventValidation><JSON>.
    N must match a real boundary; arbitrary preambles/HTML are rejected.
    """
    for _ in range(12):
        if not isinstance(value, str):
            return value
        text = value.strip().lstrip("\ufeff")
        if not text or text.startswith("<"):
            raise ExportError("回调返回 HTML/空内容，可能登录失效")
        try:
            value = _json_decode(text)
            continue
        except (ValueError, TypeError):
            pass
        match = re.match(r"^(\d+)\|", text)
        if match:
            count = int(match.group(1))
            rest = text[match.end():]
            if count > len(rest):
                raise ExportError("回调 length framing 长度不匹配")
            if count == len(rest):
                value = rest
            else:
                # Standard WebForms callback event-validation prefix.
                value = rest[count:]
            continue
        raise ExportError("回调不是可识别 JSON/length framing")
    raise ExportError("回调嵌套层数过深")


def _unwrap(response):
    value = _decode(response.text)
    for _ in range(12):
        value = _decode(value)
        if value is None or isinstance(value, str):
            return value
        if not isinstance(value, dict):
            raise ExportError("回调数据结构无法识别")
        for key in ("Error", "error", "Exception", "exception", "ErrorMessage"):
            if value.get(key):
                raise ExportError("服务器返回错误包（详情已隐藏）")
        for key in ("Success", "success", "IsSuccess"):
            if key in value and value[key] in (False, "false", "False", "0", 0):
                raise ExportError("服务器明确报告失败")
        if "ReturnValue" in value:
            returned = value["ReturnValue"]
            if returned is None:
                if value.get("IsSuccess") is not True:
                    raise ExportError("空 ReturnValue 未明确报告 IsSuccess=true")
                return None
            if isinstance(returned, str) and returned.startswith("https://"):
                if value.get("IsSuccess") is not True:
                    raise ExportError("下载回调未明确报告成功")
                return returned
            value = returned
        elif "d" in value and len(value) == 1:
            value = value["d"]
        else:
            return value
    raise ExportError("回调 envelope 嵌套过深")


def _callback(session, headers, url, hidden, method, args, *, callback_id="JTable1",
              control=None, timeout=30, poll=False):
    form = dict(hidden)
    payload = {"Method": method, "Args": args}
    if control is not None:
        payload["CallControl"] = control
    form["__CALLBACKID"] = callback_id
    form["__CALLBACKPARAM"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if poll:
        form["__VIEWSTATE_DEL"] = form.get("__VIEWSTATE", "")
        form["__VIEWSTATE"] = ""
    response = _request(session, "post", url, headers=dict(headers), data=form,
                        params={"ts___": str(time.time_ns() // 1_000_000), "am___": method}, timeout=timeout)
    return _unwrap(response)


def _identifier(value, label):
    # bool is int in Python; floats are forbidden even if integral.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ExportError(label + "必须是字符串或精确整数，不允许浮点数")
    value = str(value).strip()
    if not value or any(x in value for x in (",", "\r", "\n", "\x00")):
        raise ExportError(label + "为空或包含非法分隔符")
    return value


def normalize_order_ids(order_ids):
    if isinstance(order_ids, str):
        order_ids = order_ids.split(",")
    result = list(dict.fromkeys(_identifier(x, "订单号") for x in order_ids))
    if not result:
        raise ExportError("至少需要一个订单号")
    return result


def _filter(key, ids):
    return json.dumps([{ "k": key, "v": ",".join(ids), "c": "@="}], ensure_ascii=False, separators=(",", ":"))


def query_orders(session, headers, order_ids, hidden, *, schema=ResponseSchema(), max_pages=1000, timeout=30,
                 allow_missing=False, return_details=False):
    ids = normalize_order_ids(order_ids)
    found, internal, seen, pages = set(), [], set(), set()
    total_seen = None
    for page in range(1, max_pages + 1):
        data = _callback(session, headers, EXPORT_URL, hidden, "LoadDataToJSON",
                         [str(page), _filter("so_id", ids), "{}"], timeout=timeout)
        if not isinstance(data, dict):
            raise ExportError("LoadDataToJSON 返回值不是对象")
        rows = _decode(data.get("datas"))
        if not isinstance(rows, list):
            raise ExportError("LoadDataToJSON 缺少 datas 数组")
        pagination = _decode(data["dp"]) if "dp" in data else data
        if not isinstance(pagination, dict):
            raise ExportError("dp 分页结构无效")
        if "PageIndex" in pagination and str(pagination["PageIndex"]) != str(page):
            raise ExportError("dp PageIndex 与请求页不一致")
        last = pagination.get("IsLast")
        if last is not None and last not in (True, False, "true", "false", "True", "False"):
            raise ExportError("dp IsLast 无效")
        for field in ("DataCount",) + schema.total_fields:
            if field in pagination:
                value = pagination[field]
                if not str(value).isdigit():
                    raise ExportError("分页总数不是非负整数")
                total = int(value)
                if total_seen is not None and total_seen != total:
                    raise ExportError("分页总数变化，无法保证完整性")
                total_seen = total
                break
        fingerprint = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
        if rows and fingerprint in pages:
            raise ExportError("分页重复，停止以避免无限循环")
        pages.add(fingerprint)
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                raise ExportError("订单行不是对象")
            so_id = _identifier(row.get(schema.order_id_field), "响应订单号")
            o_id = _identifier(row.get(schema.internal_id_field), "内部订单号")
            if so_id not in ids:
                raise ExportError("响应包含筛选范围外订单，停止导出")
            found.add(so_id)
            if o_id not in seen:
                seen.add(o_id)
                internal.append(o_id)
                added += 1
        if total_seen is not None and len(seen) > total_seen:
            raise ExportError("去重行数超过服务端总数")
        if last in (True, "true", "True"):
            if total_seen is not None and len(seen) != total_seen:
                raise ExportError("末页行数与 DataCount 不一致")
            break
        if total_seen is not None and len(seen) == total_seen:
            if last in (False, "false", "False"):
                raise ExportError("DataCount 与 IsLast 互相矛盾")
            break
        if not rows:
            if total_seen is not None and len(seen) < total_seen:
                raise ExportError("分页提前结束，未取得服务端声明的所有订单")
            break
        if not added:
            raise ExportError("分页未产生新订单，停止以避免无限循环")
    else:
        raise ExportError("超过最大分页数，未确认完整查询")
    missing = [x for x in ids if x not in found]
    if missing and not allow_missing:
        raise MissingOrdersError(missing)
    if return_details:
        return {"internal_ids": internal, "found_ids": [x for x in ids if x in found], "missing_ids": missing}
    return internal


def _field(data, fields, label):
    values = [data[k] for k in fields if k in data and data[k] is not None]
    if not values or any(v != values[0] for v in values):
        raise ExportError(label + "缺失或候选字段互相矛盾；需配置真实响应 schema")
    return values[0]


def create_export(session, headers, hidden, internal_ids, *, schema=ResponseSchema(), timeout=30,
                  before_create=None):
    """Check only, durably mark uncertain creation, then issue the live create call.

    ReturnValue=null is normal for both callbacks; RequestId is transport metadata.
    before_create MUST persist intent before ExportBySync can reach the network.
    """
    count = str(len(internal_ids))
    args = [_filter("o_id", internal_ids), count, "true", "false", "false"]
    checked = _callback(session, headers, EXPORT_URL, hidden, "ExportCheckConfirm", args,
                        control="{page}", timeout=timeout)
    if checked is not None:
        raise ExportError("ExportCheckConfirm 返回未知结构；未创建任务")
    if before_create is None:
        raise ExportError("创建任务必须先持久化状态")
    before_create()
    created = _callback(session, headers, EXPORT_URL, hidden, "ExportBySync", args + [None],
                        control="{page}", timeout=timeout)
    if created is not None:
        raise ExportError("ExportBySync 返回未知结构；创建结果不确定，请续跑")


def _task_id(value):
    value = _identifier(value, "导出日志 auto_id")
    if not value.isascii() or not value.isdigit():
        raise ExportError("导出日志 auto_id 必须是十进制整数")
    return str(int(value))


def list_export_tasks(session, headers, *, timeout=30, max_pages=1000):
    hidden = get_hidden(session, headers, LOG_URL, timeout)
    result, seen = [], set()
    for page in range(1, max_pages + 1):
        data = _callback(session, headers, LOG_URL, hidden, "LoadDataToJSON",
                         [str(page), "[]", "{}"], callback_id="JTable1", timeout=timeout)
        if not isinstance(data, dict):
            raise ExportError("导出日志返回值不是对象")
        rows = _decode(data.get("datas"))
        if not isinstance(rows, list):
            raise ExportError("导出日志缺少 datas")
        for row in rows:
            if not isinstance(row, dict):
                raise ExportError("导出日志行无效")
            task = _task_id(row.get("auto_id"))
            if task in seen:
                raise ExportError("导出日志分页重复")
            seen.add(task)
            result.append(row)
        # Live samples may omit dp on this table: one visible snapshot is safe
        # for new tasks; missing candidates never cause a replacement create.
        if "dp" not in data:
            return result
        dp = _decode(data["dp"])
        if not isinstance(dp, dict) or str(dp.get("PageIndex")) != str(page):
            raise ExportError("导出日志 dp 无效")
        if dp.get("IsLast") in (True, "true", "True"):
            return result
        if dp.get("IsLast") not in (False, "false", "False") or not rows:
            raise ExportError("导出日志分页未确认完整")
    raise ExportError("导出日志超过最大分页数")


def poll_export(session, headers, request_id, *, schema=ResponseSchema(), max_polls=120,
                poll_interval=2, sleep=time.sleep, timeout=30):
    """Compatibility argument name request_id; value MUST be a log auto_id."""
    task = _task_id(request_id)
    hidden = get_hidden(session, headers, LOG_URL, timeout)
    for index in range(max_polls):
        data = _callback(session, headers, LOG_URL, hidden, "GetExportData", [task],
                         callback_id="ACall1", control="{page}", poll=True, timeout=timeout)
        if isinstance(data, str):
            return validate_download_url(data) if urlsplit(data).hostname in ("www.erp321.com", "erp321.com") else data
        if data is not None:
            raise ExportError("GetExportData 返回未知结构；未猜测状态或 URL")
        if index + 1 < max_polls:
            sleep(poll_interval)
    raise ExportError("导出任务轮询超时；状态已保存，请续跑")


def validate_download_url(url, extra_domains=()):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ExportError("下载 URL 无效") from None
    if (parts.scheme != "https" or not host or parts.username or parts.password or
            parts.fragment or port not in (None, 443) or "\\" in url or any(ord(x) < 33 for x in url)):
        raise ExportError("下载只允许无用户凭据的标准 HTTPS URL")
    allowed = ("erp321.com", "jushuitan.com") + tuple(extra_domains)
    if not any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed if d):
        raise ExportError("下载域不在显式允许列表")
    return url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public_download(url, headers, timeout, max_bytes):
    # A new stdlib opener has no CookieJar, netrc, authentication, or caller session.
    opener = build_opener(_NoRedirect())
    try:
        response = opener.open(Request(url, headers=headers), timeout=timeout)
    except HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, exc.headers, b""
        raise ExportError("下载 HTTP 失败") from None
    try:
        content = response.read(max_bytes + 1)
        return response.status, response.headers, content
    finally:
        response.close()


def download_export(session, headers, url, *, extra_domains=(), timeout=60,
                    max_bytes=100 * 1024 * 1024, public_download=None):
    """External hosts NEVER receive the authenticated session, even when allowed.

    public_download is an optional test/transport hook (url, safe_headers, timeout,
    max_bytes) -> (status, response_headers, bytes). It must not add credentials.
    """
    public_download = public_download or _public_download
    for _ in range(6):
        validate_download_url(url, extra_domains)
        origin = (urlsplit(url).scheme, urlsplit(url).hostname, urlsplit(url).port or 443)
        if origin == ("https", "www.erp321.com", 443):
            try:
                response = session.get(url, headers=dict(headers), timeout=timeout, allow_redirects=False)
                status, response_headers = response.status_code, getattr(response, "headers", {})
                content = response.content
            except Exception:
                raise ExportError("下载 transport 失败（详情已隐藏）") from None
        else:
            safe_headers = {k: v for k, v in headers.items() if k.lower() in ("user-agent", "accept")}
            try:
                status, response_headers, content = public_download(url, safe_headers, timeout, max_bytes)
            except ExportError:
                raise
            except Exception:
                raise ExportError("无凭据下载 transport 失败（详情已隐藏）") from None
        if status in (301, 302, 303, 307, 308):
            location = response_headers.get("Location") or response_headers.get("location")
            if not location:
                raise ExportError("下载重定向无 Location")
            url = urljoin(url, location)
            continue
        if status != 200:
            raise ExportError("下载响应非 200")
        if not isinstance(content, bytes) or len(content) > max_bytes:
            raise ExportError("下载文件超过大小限制或内容类型无效")
        return content
    raise ExportError("下载重定向次数超限")


def _check_zip(content, *, max_unpacked, max_members, max_ratio):
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        infos = archive.infolist()
        if not infos or len(infos) > max_members:
            raise ExportError("ZIP 文件数量无效/超限")
        names = set()
        total = 0
        for info in infos:
            name = info.filename
            path = PurePosixPath(name)
            if (name in names or name.startswith("/") or "\\" in name or ":" in name or
                    ".." in path.parts or "\x00" in name or
                    stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ExportError("ZIP 包含重复、危险路径、链接或加密成员")
            names.add(name)
            total += info.file_size
            if total > max_unpacked or info.file_size / max(info.compress_size, 1) > max_ratio:
                raise ExportError("ZIP 解压体积/压缩比超限，疑似 ZIP bomb")
        if archive.testzip() is not None:
            raise ExportError("ZIP CRC 校验失败")
        return archive, names
    except ExportError:
        raise
    except Exception:
        raise ExportError("文件不是有效、安全的 ZIP/XLSX") from None


def _validate_xlsx(archive, names):
    if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
        raise ExportError("缺少 XLSX 必需成员")
    if any(n.lower().endswith(("vbaproject.bin", ".exe", ".dll")) for n in names):
        raise ExportError("XLSX 包含可执行/宏内容")
    try:
        for name in names:
            if not name.lower().endswith((".xml", ".rels")):
                continue
            content = archive.read(name)
            # ElementTree itself does not fetch external entities; reject declarations
            # as well, including UTF-16 spelling, to avoid entity expansion payloads.
            probe = content.replace(b"\x00", b"").lower()
            if b"<!doctype" in probe or b"<!entity" in probe:
                raise ExportError("XLSX XML 含禁止的 DTD/entity")
            root = ET.fromstring(content)
            if name == "xl/workbook.xml" and root.tag != "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook":
                raise ExportError("XLSX workbook XML 无效")
            if name == "[Content_Types].xml":
                main_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
                if not any(x.attrib.get("PartName") == "/xl/workbook.xml" and
                           x.attrib.get("ContentType") == main_type for x in root):
                    raise ExportError("文件不是标准无宏 XLSX")
            if name.lower().endswith(".rels"):
                if any(x.attrib.get("TargetMode", "").lower() == "external" for x in root):
                    raise ExportError("XLSX 含外部链接；安全策略拒绝")
    except ExportError:
        raise
    except Exception:
        raise ExportError("XLSX 内 XML 无效") from None


def validate_archive(content, *, max_unpacked=300 * 1024 * 1024, max_members=10000, max_ratio=200):
    """Validate without extraction; ZIP bundles must contain only valid XLSX files."""
    archive, names = _check_zip(content, max_unpacked=max_unpacked, max_members=max_members, max_ratio=max_ratio)
    try:
        if {"[Content_Types].xml", "xl/workbook.xml"} <= names:
            _validate_xlsx(archive, names)
            return ".xlsx"
        files = [x for x in archive.infolist() if not x.is_dir()]
        if not files or any(not x.filename.lower().endswith(".xlsx") for x in files):
            raise ExportError("ZIP 导出包只允许包含 XLSX 文件")
        nested_total = 0
        for info in files:
            nested, nested_names = _check_zip(archive.read(info), max_unpacked=max_unpacked,
                                              max_members=max_members, max_ratio=max_ratio)
            try:
                nested_total += sum(item.file_size for item in nested.infolist())
                if nested_total > max_unpacked:
                    raise ExportError("ZIP 内 XLSX 累积解压体积超限")
                _validate_xlsx(nested, nested_names)
            finally:
                nested.close()
        return ".zip"
    finally:
        archive.close()


def _atomic_create(output, content):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ExportError("输出路径已存在，禁止覆盖")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".jst-export-", dir=str(output.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard link is atomic and fails if any concurrent writer created output.
        try:
            os.link(temporary, output)
        except FileExistsError:
            raise ExportError("输出路径已存在，禁止覆盖") from None
    finally:
        os.unlink(temporary)


def unpack_export(path, *, max_bytes=100 * 1024 * 1024):
    """Return validated XLSX paths; ZIP gets a unique sibling extraction directory.

    Keeps the original ZIP. Does not use ZipFile.extract() or overwrite any file.
    Validates every member before creating the unique output directory.
    """
    path = Path(path)
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ExportError("导出文件超过大小限制")
    kind = validate_archive(content)
    if kind == ".xlsx":
        return [path]
    directory = Path(tempfile.mkdtemp(prefix=path.stem + "-unpacked-", dir=str(path.parent)))
    outputs = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            # Traversal/symlink/duplicates have already been rejected. Flattening
            # via numbered generated names also avoids platform path ambiguities.
            output = directory / (str(len(outputs) + 1).zfill(4) + ".xlsx")
            _atomic_create(output, archive.read(info))
            outputs.append(output)
    return outputs


def validate_order_coverage(content, expected_ids):
    """Require exact text-ID coverage in 原始线上订单号 across all workbooks.

    Archive security is checked before openpyxl parses anything. Multiple product
    rows for one order are normal; foreign or absent orders are never accepted.
    """
    from openpyxl import load_workbook
    kind = validate_archive(content)
    books = [content]
    if kind == ".zip":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            books = [archive.read(x) for x in archive.infolist() if not x.is_dir()]
    actual = set()
    for book in books:
        wb = None
        try:
            wb = load_workbook(io.BytesIO(book), read_only=True, data_only=False, keep_links=False)
            identified = False
            for sheet in wb.worksheets:
                # Vendor workbooks may have absent/stale worksheet dimensions.
                # Iterating actual XML prevents a forged dimension hiding orders.
                sheet.reset_dimensions()
                column = None
                for row in sheet.iter_rows():
                    if column is None:
                        matches = [i for i, cell in enumerate(row) if cell.value == "原始线上订单号"]
                        if len(matches) > 1:
                            raise ExportError("工作簿订单号列重复")
                        if matches:
                            column = matches[0]
                            identified = True
                        continue
                    cell = row[column] if column < len(row) else None
                    value = cell.value if cell is not None else None
                    if value is None or value == "":
                        if any(c.value not in (None, "") for c in row):
                            raise ExportError("工作簿数据行缺少原始线上订单号")
                        continue
                    if cell.data_type == "f" or isinstance(value, bool) or isinstance(value, float):
                        raise ExportError("工作簿订单号不是可靠文本/整数")
                    if isinstance(value, int) and len(str(abs(value))) > 15:
                        raise ExportError("工作簿长订单号为数值，可能丢失精度")
                    actual.add(_identifier(value, "工作簿订单号"))
            if not identified:
                raise ExportError("工作簿缺少原始线上订单号列")
        except ExportError:
            raise
        except Exception:
            raise ExportError("无法安全读取导出工作簿") from None
        finally:
            if wb is not None:
                wb.close()
    expected = set(expected_ids)
    if actual != expected:
        raise ExportError("导出工作簿订单范围/覆盖不匹配；未接受任务文件")
    return kind


def _save_state(path, state, *, initial=False):
    content = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    path = Path(path)
    if initial:
        _atomic_create(path, content)
    else:
        if path.is_symlink() or not path.is_file():
            raise ExportError("状态路径不是普通文件")
        fd, name = tempfile.mkstemp(prefix=".jst-state-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    # Persist directory entries before any task creation can occur.
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        if os.name != "nt":
            raise ExportError("状态目录无法持久化；停止创建任务") from None


def _load_state(path):
    try:
        if Path(path).is_symlink() or Path(path).stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        required = {"version", "phase", "requested_ids", "found_ids", "missing_ids", "internal_ids",
                    "baseline_ids", "candidate_ids", "export_type", "output"}
        optional = {"creation_window", "task_total_count"}
        if (not isinstance(state, dict) or not required <= set(state) or
                set(state) - required - optional or state["version"] != 1):
            raise ValueError()
        if "creation_window" in state:
            window = state["creation_window"]
            if (not isinstance(window, list) or len(window) != 2 or
                    any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                        not math.isfinite(x) or x <= 0 for x in window) or
                    window[0] > window[1]):
                raise ValueError()
        if "task_total_count" in state and (state["task_total_count"] is not None and
                (type(state["task_total_count"]) is not int or state["task_total_count"] < 0)):
            raise ValueError()
        if state["phase"] not in ("prepared", "creating", "created", "candidate", "complete"):
            raise ValueError()
        for key in ("requested_ids", "found_ids", "missing_ids", "internal_ids", "baseline_ids", "candidate_ids"):
            if not isinstance(state[key], list) or any(not isinstance(x, str) for x in state[key]):
                raise ValueError()
        if normalize_order_ids(state["requested_ids"]) != state["requested_ids"]:
            raise ValueError()
        if (set(state["found_ids"]) & set(state["missing_ids"]) or
                set(state["found_ids"]) | set(state["missing_ids"]) != set(state["requested_ids"])):
            raise ValueError()
        if not state["found_ids"] or not state["internal_ids"]:
            raise ValueError()
        for key in ("baseline_ids", "candidate_ids"):
            if any(_task_id(x) != x for x in state[key]):
                raise ValueError()
        if len(state["candidate_ids"]) > 1:
            raise ValueError()
        return state
    except Exception:
        raise ExportError("状态文件无效，禁止创建或猜测任务") from None


def _candidate_tasks(rows, state):
    """Associate by new auto_id, explicit type and (when recorded) creation time.

    total_count counts product lines on live ERP logs, not internal orders. It
    cannot select/exclude candidates. Legacy v1 journals have no clock evidence:
    retain unique-new-task + exact-workbook verification, never invent a window
    from mtime, resume time or the timestamps of unrelated baseline tasks.
    """
    candidates = []
    window = state.get("creation_window")
    for row in rows:
        task = _task_id(row.get("auto_id"))
        if task in state["baseline_ids"]:
            continue
        if window is not None:
            created = _created_timestamp(row.get("created"))
            if not window[0] - TASK_CLOCK_SKEW_SECONDS <= created <= window[1] + TASK_CLOCK_SKEW_SECONDS:
                continue
        typ = row.get("export_type")
        if (not isinstance(typ, (str, int)) or isinstance(typ, bool) or
                str(typ).strip().lower() in ("", "unknown", "null", "none")):
            raise ExportError("新日志缺少或未知 export_type，无法关联任务")
        if state["export_type"] is not None and str(typ) != state["export_type"]:
            continue
        candidates.append((task, str(typ)))
    if len(candidates) > 1:
        raise ExportError("多个新导出任务符合类型/创建时间，关联有歧义；停止且不重建")
    return candidates


def _run_export(session, headers, order_ids, output, *, schema=ResponseSchema(),
               max_pages=1000, max_polls=120, poll_interval=2, sleep=time.sleep,
               extra_domains=(), timeout=30, public_download=None, state_path=None,
               resume=False, allow_missing=False, export_type=None):
    """Live export with a credential-free durable journal and exact XLSX coverage.

    Default journal: str(output)+".state.json". Resume MUST use identical requested
    orders/output/options. A creating/created journal NEVER issues another create,
    even if no matching log appears. Callers may pass the known export_type;
    otherwise a unique new typed log supplies it (workbook proves scope).
    New creation attempts record a bounded UTC epoch window before the request;
    interrupted attempts keep that original window, never extend it on resume.
    allow_missing=True exports found orders but returns ok=False, complete=False
    and explicit missing_ids; no missing order is represented as exported.
    """
    output = Path(output).absolute()
    state_path = Path(state_path or (str(output) + ".state.json")).absolute()
    ids = normalize_order_ids(order_ids)
    typ = None if export_type is None else str(export_type)
    if state_path == output or state_path in (output.with_suffix(".xlsx"), output.with_suffix(".zip")):
        raise ExportError("状态文件与输出路径冲突")
    if resume:
        state = _load_state(state_path)
        if (state["requested_ids"] != ids or state["output"] != str(output) or
                (typ is not None and state["export_type"] not in (None, typ))):
            raise ExportError("续跑参数与状态订单范围/输出/类型不一致")
        if state["missing_ids"] and not allow_missing:
            raise MissingOrdersError(state["missing_ids"])
    else:
        if state_path.exists() or state_path.is_symlink():
            raise ExportError("状态文件已存在；必须显式 resume，禁止重复创建任务")
        state = None
    paths = [output] if output.suffix else [output, output.with_suffix(".xlsx"), output.with_suffix(".zip")]
    if any(p.exists() or p.is_symlink() for p in paths):
        # A crash after output commit but before complete journal can be resumed
        # only after revalidating its exact scope and security, never overwritten.
        existing = [p for p in paths if p.is_file() and not p.is_symlink()]
        if not resume or state["phase"] not in ("candidate", "complete") or len(existing) != 1:
            raise ExportError("输出路径已存在，禁止覆盖")
        with existing[0].open("rb") as handle:
            content = handle.read(100 * 1024 * 1024 + 1)
        if len(content) > 100 * 1024 * 1024:
            raise ExportError("现有输出过大")
        suffix = validate_order_coverage(content, state["found_ids"])
        if existing[0].suffix.lower() != suffix:
            raise ExportError("现有输出扩展名与内容不符")
        state["phase"] = "complete"
        _save_state(state_path, state)
        return _export_report(state, existing[0], suffix, len(content), state_path)
    if state is None:
        hidden = get_hidden(session, headers, timeout=timeout)
        details = query_orders(session, headers, ids, hidden, schema=schema, max_pages=max_pages,
                               timeout=timeout, allow_missing=allow_missing, return_details=True)
        if not details["internal_ids"]:
            raise MissingOrdersError(ids)
        baseline = list_export_tasks(session, headers, timeout=timeout, max_pages=max_pages)
        state = dict(version=1, phase="prepared", requested_ids=ids, **details,
                     baseline_ids=[_task_id(row["auto_id"]) for row in baseline], candidate_ids=[],
                     export_type=typ, output=str(output))
        _save_state(state_path, state, initial=True)
    if state["phase"] == "complete":
        raise ExportError("已完成状态的输出缺失；禁止重新创建任务")
    if resume:
        hidden = get_hidden(session, headers, timeout=timeout)
        current = query_orders(session, headers, ids, hidden, schema=schema, max_pages=max_pages,
                               timeout=timeout, allow_missing=True, return_details=True)
        if (current["found_ids"] != state["found_ids"] or
                current["missing_ids"] != state["missing_ids"] or
                set(current["internal_ids"]) != set(state["internal_ids"])):
            raise ExportError("续跑会话的订单/内部编号与状态不一致；可能账号已切换，停止")
    if state["phase"] == "prepared":
        hidden = get_hidden(session, headers, timeout=timeout)
        def before_create():
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ExportError("创建请求 timeout 必须为有限正数")
            started = time.time()
            # Persist the entire allowed request interval before any side effect.
            # A timeout/crash cannot leave an open-ended window or cause resume to
            # include unrelated exports created hours later.
            state["creation_window"] = [started, started + timeout]
            state["phase"] = "creating"
            _save_state(state_path, state)
        create_export(session, headers, hidden, state["internal_ids"], schema=schema,
                      timeout=timeout, before_create=before_create)
        state["phase"] = "created"
        _save_state(state_path, state)
    if not state["candidate_ids"]:
        for index in range(max_polls):
            rows = list_export_tasks(session, headers, timeout=timeout, max_pages=max_pages)
            candidates = _candidate_tasks(rows, state)
            if candidates:
                task, candidate_type = candidates[0]
                state["candidate_ids"] = [task]
                state["export_type"] = candidate_type
                selected = next(row for row in rows if _task_id(row.get("auto_id")) == task)
                count = str(selected.get("total_count", ""))
                state["task_total_count"] = int(count) if count.isascii() and count.isdigit() and len(count) <= 20 else None
                state["phase"] = "candidate"
                _save_state(state_path, state)
                break
            if index + 1 < max_polls:
                sleep(poll_interval)
        else:
            raise ExportError("未发现唯一新任务；创建结果不确定，保留状态续跑，绝不重建")
    task = state["candidate_ids"][0]
    url = poll_export(session, headers, task, schema=schema, max_polls=max_polls,
                      poll_interval=poll_interval, sleep=sleep, timeout=timeout)
    content = download_export(session, headers, url, extra_domains=extra_domains,
                              timeout=timeout, public_download=public_download)
    suffix = validate_order_coverage(content, state["found_ids"])
    if not output.suffix:
        output = output.with_suffix(suffix)
    elif output.suffix.lower() != suffix:
        raise ExportError("输出扩展名与下载内容不一致；请使用 " + suffix)
    _atomic_create(output, content)
    state["phase"] = "complete"
    _save_state(state_path, state)
    return _export_report(state, output, suffix, len(content), state_path)


def _export_report(state, output, suffix, byte_count, state_path):
    complete = not state["missing_ids"]
    return {"ok": complete, "complete": complete, "output": str(output),
            "requested_orders": len(state["requested_ids"]), "matched_orders": len(state["found_ids"]),
            "found_ids": state["found_ids"], "missing_ids": state["missing_ids"],
            "exported_internal_orders": len(state["internal_ids"]), "task_id": state["candidate_ids"][0],
            "task_total_count": state.get("task_total_count"),
            "task_count_matches_internal_orders": (None if state.get("task_total_count") is None else
                                                   state["task_total_count"] == len(state["internal_ids"])),
            "task_association_time_checked": "creation_window" in state,
            "state_path": str(state_path), "file_type": suffix[1:], "bytes": byte_count}


@contextmanager
def _journal_lock(path):
    """OS advisory locks release on crash; no stale lock can cause a re-create."""
    path = Path(str(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ExportError("状态锁路径不允许符号链接")
    handle = path.open("a+b")
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise ExportError("同一导出状态正在被其他进程使用；停止避免重复创建") from None
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def run_export(session, headers, order_ids, output, *, schema=ResponseSchema(),
               max_pages=1000, max_polls=120, poll_interval=2, sleep=time.sleep,
               extra_domains=(), timeout=30, public_download=None, state_path=None,
               resume=False, allow_missing=False, export_type=None):
    """Serialize a durable real-flow export; see _run_export for resume semantics."""
    state_path = Path(state_path or (str(Path(output).absolute()) + ".state.json")).absolute()
    with _journal_lock(state_path):
        return _run_export(session, headers, order_ids, output, schema=schema,
                           max_pages=max_pages, max_polls=max_polls, poll_interval=poll_interval,
                           sleep=sleep, extra_domains=extra_domains, timeout=timeout,
                           public_download=public_download, state_path=state_path,
                           resume=resume, allow_missing=allow_missing, export_type=export_type)
