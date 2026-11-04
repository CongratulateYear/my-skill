"""读取当前浏览器登录的天猫供应商身份（公司名 / supplierIdSec / ERP 手机号）。

用途：多店串行时，cookie 里的 tracknick 可能残留上一个账号（旧值不刷新），
不能据此判断当前登录的是哪家店。这里直接问平台「我是谁」，作为换号后的
人工/脚本核对依据。只读，不落盘任何凭据。

用法：
  python supplier_identity.py [--transport bridge] [--bridge-url http://127.0.0.1:8899]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import tmall_session  # noqa: E402

ACCOUNT_INFO_URL = "https://tgc.tmall.com/api/v1/supplierHome/accountInfo"


def read_identity(transport: str = "auto", bridge_url: str | None = None) -> dict:
    built = tmall_session.build_session(transport, "auto", tmall_session.DEFAULT_DOMAIN, bridge_url)
    session = built["session"]
    headers = {k: v for k, v in built["headers"].items() if k in {"cookie", "user-agent", "referer"}}
    request = Request(ACCOUNT_INFO_URL, headers=headers)
    with session.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    sub = payload.get("subCompanyList") or []
    first = sub[0] if sub else {}
    ding = (payload.get("dingdingList") or [{}])[0]
    return {
        "transport": built["transport"],
        "companyName": payload.get("companyName"),
        "supplierName": first.get("supplierName"),
        "supplierIdSec": first.get("supplierIdSec"),
        "supplierId": ding.get("supplierId"),
        "erpMobile": ding.get("dingdingMobile"),
        "subCompanyCount": len(sub),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取当前登录的天猫供应商身份")
    parser.add_argument("--transport", default="bridge", choices=tmall_session.TRANSPORTS)
    parser.add_argument("--bridge-url", default="")
    args = parser.parse_args(argv)
    try:
        identity = read_identity(args.transport, args.bridge_url or None)
    except Exception as exc:  # 桥未接入 / 会话过期都会走到这里
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **identity}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
