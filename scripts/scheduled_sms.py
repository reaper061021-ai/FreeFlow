#!/usr/bin/env python3
"""在指定时间通过 Twilio 发送短信。

使用示例:
python scripts/scheduled_sms.py \
  --to +8613812345678 \
  --from-number +15017122661 \
  --message "提醒：开会" \
  --at "2026-04-23 09:30" \
  --timezone "Asia/Shanghai" \
  --account-sid "$TWILIO_ACCOUNT_SID" \
  --auth-token "$TWILIO_AUTH_TOKEN"
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


TWILIO_API_TEMPLATE = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在指定时间发送短信到手机")
    parser.add_argument("--to", required=True, help="接收方手机号（E.164 格式，如 +8613812345678）")
    parser.add_argument("--from-number", required=True, help="Twilio 购买的发信号码（E.164 格式）")
    parser.add_argument("--message", required=True, help="短信内容")
    parser.add_argument(
        "--at",
        required=True,
        help='发送时间，格式 "YYYY-MM-DD HH:MM"（默认按本机时区解释）',
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="可选时区（IANA 名称，如 Asia/Shanghai、America/Los_Angeles）",
    )
    parser.add_argument("--account-sid", default=os.getenv("TWILIO_ACCOUNT_SID"), help="Twilio Account SID")
    parser.add_argument("--auth-token", default=os.getenv("TWILIO_AUTH_TOKEN"), help="Twilio Auth Token")
    return parser.parse_args()


def get_target_datetime(at_text: str, timezone_name: str | None) -> dt.datetime:
    naive = dt.datetime.strptime(at_text, "%Y-%m-%d %H:%M")

    if timezone_name:
        tz = ZoneInfo(timezone_name)
    else:
        tz = dt.datetime.now().astimezone().tzinfo
        if tz is None:
            raise ValueError("无法自动检测本机时区，请使用 --timezone 显式指定")

    return naive.replace(tzinfo=tz)


def wait_until(target: dt.datetime) -> None:
    while True:
        now = dt.datetime.now(target.tzinfo)
        delta = (target - now).total_seconds()
        if delta <= 0:
            return

        sleep_seconds = min(delta, 30)
        print(f"等待中：剩余 {int(delta)} 秒", flush=True)
        time.sleep(sleep_seconds)


def send_sms(account_sid: str, auth_token: str, from_number: str, to_number: str, body: str) -> dict:
    url = TWILIO_API_TEMPLATE.format(account_sid=account_sid)
    payload = urllib.parse.urlencode({"From": from_number, "To": to_number, "Body": body}).encode("utf-8")

    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        response_data = response.read().decode("utf-8")

    return json.loads(response_data)


def main() -> int:
    args = parse_args()

    if not args.account_sid or not args.auth_token:
        print("错误：缺少 Twilio 凭据。请传入 --account-sid / --auth-token 或设置环境变量。", file=sys.stderr)
        return 2

    try:
        target = get_target_datetime(args.at, args.timezone)
    except ValueError as exc:
        print(f"时间解析失败：{exc}", file=sys.stderr)
        return 2

    now = dt.datetime.now(target.tzinfo)
    if target <= now:
        print(f"错误：发送时间必须晚于当前时间。当前：{now.isoformat()}，目标：{target.isoformat()}", file=sys.stderr)
        return 2

    print(f"当前时间：{now.isoformat()}")
    print(f"目标时间：{target.isoformat()}")
    print("到点后将发送短信...", flush=True)

    wait_until(target)

    try:
        result = send_sms(
            account_sid=args.account_sid,
            auth_token=args.auth_token,
            from_number=args.from_number,
            to_number=args.to,
            body=args.message,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"发送失败（HTTP {exc.code}）：{body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误：{exc}", file=sys.stderr)
        return 1

    sid = result.get("sid", "<unknown>")
    status = result.get("status", "<unknown>")
    print(f"发送成功，消息 SID: {sid}，状态: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
