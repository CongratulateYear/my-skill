#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
from typing import List, Optional

# ── 匹配规则 ──
LOCATION_RE = re.compile(r'(东苑|西区\d+|金星|七一)')
PICKUP_RE = re.compile(r'\d+-\d+-\d+')
COURIER_PREFIX_RE = re.compile(r'^【([^】]+)】')
DOOR_DELIVERY_KW = ('送货上门', '送货上门服务')
TRIGGER_KW = ('快递', '取件码', '取件', '家门口', '取货')


def run_cmd(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def get_clipboard() -> Optional[str]:
    p = run_cmd(["/usr/local/bin/apple-clipboard", "get", "-q"])
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    return None


def ensure_list_exists(list_name: str) -> bool:
    p = run_cmd(["/usr/local/bin/apple-reminders", "list", "--list", list_name, "--limit", "1", "--compact"])
    if p.returncode != 0:
        return False
    return True


def is_door_delivery(text: str) -> bool:
    return any(kw in text for kw in DOOR_DELIVERY_KW)


def is_courier_message(text: str) -> bool:
    return any(kw in text for kw in TRIGGER_KW)


def get_courier_prefix(text: str) -> Optional[str]:
    m = COURIER_PREFIX_RE.search(text.strip())
    return m.group(1).strip() if m else None


def build_title(text: str) -> str:
    location_match = LOCATION_RE.search(text)
    loc = location_match.group(1) if location_match else get_courier_prefix(text)
    if not loc:
        return text.strip()

    codes = PICKUP_RE.findall(text)

    if codes:
        return f"{loc}：{', '.join(codes)}"
    if is_door_delivery(text):
        return f"{loc}：家门口"
    return text.strip()


def create_reminder(title: str, list_name: str, notes: Optional[str]) -> dict:
    cmd = ["/usr/local/bin/apple-reminders", "create", "--title", title, "--list", list_name, "--compact"]
    if notes:
        cmd.extend(["--notes", notes])
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "创建失败")
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"raw": p.stdout.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="解析快递短信并创建提醒事项")
    parser.add_argument("--text", help="直接传入短信文本")
    parser.add_argument("--text-file", help="从文件读取短信文本")
    parser.add_argument("--stdin", action="store_true", help="从剪贴板读取（默认行为）")
    parser.add_argument("--list", default="快递", help="提醒事项列表名（默认：快递）")
    parser.add_argument("--notes", action="store_true", help="在提醒事项备注中保存原文")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    text = None
    if args.text is not None:
        text = args.text
    elif args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read()
    elif True:
        text = get_clipboard()

    if text is None or not text.strip():
        msg = "没有获取到文本。请先复制短信到剪贴板。"
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if args.json else msg,
              file=sys.stderr if not args.json else sys.stdout)
        return 2

    text = text.strip()

    if not is_courier_message(text):
        msg = "未识别为快递信息，已跳过。需要包含关键词：快递 / 取件码 / 家门口 / 取货"
        print(json.dumps({"ok": False, "skipped": True, "error": msg}, ensure_ascii=False) if args.json else msg,
              file=sys.stderr if not args.json else sys.stdout)
        return 0

    title = build_title(text)
    location = LOCATION_RE.search(text)
    codes = PICKUP_RE.findall(text)

    if not ensure_list_exists(args.list):
        msg = f"提醒事项列表「{args.list}」不存在。请先在提醒事项 App 中创建名为「{args.list}」的列表。"
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if args.json else msg,
              file=sys.stderr if not args.json else sys.stdout)
        return 3

    notes = text if args.notes else None
    try:
        result = create_reminder(title, args.list, notes)
    except Exception as e:
        msg = f"创建提醒失败: {e}"
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if args.json else msg,
              file=sys.stderr if not args.json else sys.stdout)
        return 4

    out = {
        "ok": True,
        "title": title,
        "location": location.group(1) if location else None,
        "codes": codes,
        "list": args.list,
    }
    print(json.dumps(out, ensure_ascii=False) if args.json else f"✅ {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
