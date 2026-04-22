#!/usr/bin/env python3
"""在指定时间发送短信（支持 Twilio 或免费邮箱网关）。

示例 1：Twilio（付费，稳定）
python scripts/scheduled_sms.py \
  --method twilio \
  --to +14155552671 \
  --from-number +15017122661 \
  --message "Meeting in 10 minutes" \
  --at "2026-04-23 09:30" \
  --timezone "America/Los_Angeles" \
  --account-sid "$TWILIO_ACCOUNT_SID" \
  --auth-token "$TWILIO_AUTH_TOKEN"

示例 2：邮箱短信网关（通常免费，但不保证送达）
python scripts/scheduled_sms.py \
  --method email-gateway \
  --to +14155552671 \
  --carrier verizon \
  --message "Free reminder" \
  --at "2026-04-23 09:30" \
  --timezone "America/Los_Angeles" \
  --smtp-host smtp.gmail.com \
  --smtp-port 587 \
  --smtp-user "you@example.com" \
  --smtp-password "$SMTP_PASSWORD" \
  --smtp-from "you@example.com"
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from zoneinfo import ZoneInfo


TWILIO_API_TEMPLATE = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

# 美国常见运营商邮箱网关；可按需扩展
CARRIER_GATEWAYS = {
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
    "verizon": "vtext.com",
    "visible": "vtext.com",
    "sprint": "messaging.sprintpcs.com",
    "boost": "sms.myboostmobile.com",
    "cricket": "sms.cricketwireless.net",
    "googlefi": "msg.fi.google.com",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在指定时间发送短信到手机")

    parser.add_argument("--method", choices=["twilio", "email-gateway"], default="twilio", help="发送方式")
    parser.add_argument("--to", required=True, help="接收方手机号（推荐 E.164 格式，如 +14155552671）")
    parser.add_argument("--message", required=True, help="短信内容")
    parser.add_argument("--at", required=True, help='发送时间，格式 "YYYY-MM-DD HH:MM"')
    parser.add_argument("--timezone", default=None, help="可选时区（IANA 名称）")

    # Twilio 参数
    parser.add_argument("--from-number", default=None, help="Twilio 发信号码（仅 method=twilio 需要）")
    parser.add_argument("--account-sid", default=os.getenv("TWILIO_ACCOUNT_SID"), help="Twilio Account SID")
    parser.add_argument("--auth-token", default=os.getenv("TWILIO_AUTH_TOKEN"), help="Twilio Auth Token")

    # Email gateway 参数
    parser.add_argument("--carrier", choices=sorted(CARRIER_GATEWAYS.keys()), default=None, help="运营商（仅 method=email-gateway 需要）")
    parser.add_argument("--smtp-host", default=os.getenv("SMTP_HOST"), help="SMTP 服务器地址")
    parser.add_argument("--smtp-port", type=int, default=int(os.getenv("SMTP_PORT", "587")), help="SMTP 端口")
    parser.add_argument("--smtp-user", default=os.getenv("SMTP_USER"), help="SMTP 用户名")
    parser.add_argument("--smtp-password", default=os.getenv("SMTP_PASSWORD"), help="SMTP 密码或应用专用密码")
    parser.add_argument("--smtp-from", default=os.getenv("SMTP_FROM"), help="发件人邮箱")

    return parser.parse_args()


def normalize_phone_digits(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


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


def send_sms_twilio(account_sid: str, auth_token: str, from_number: str, to_number: str, body: str) -> dict:
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


def send_sms_via_email_gateway(
    phone: str,
    carrier: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_from: str,
) -> str:
    digits = normalize_phone_digits(phone)
    if len(digits) != 10:
        raise ValueError("email-gateway 模式下手机号应为美国 10 位号码（可带 +1）")

    to_addr = f"{digits}@{CARRIER_GATEWAYS[carrier]}"

    msg = EmailMessage()
    msg["From"] = smtp_from
    msg["To"] = to_addr
    msg["Subject"] = ""
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

    return to_addr


def validate_args(args: argparse.Namespace) -> None:
    if args.method == "twilio":
        if not args.from_number:
            raise ValueError("method=twilio 时必须提供 --from-number")
        if not args.account_sid or not args.auth_token:
            raise ValueError("method=twilio 时必须提供 Twilio 凭据")
        return

    # email-gateway
    missing = [
        name
        for name, value in [
            ("--carrier", args.carrier),
            ("--smtp-host", args.smtp_host),
            ("--smtp-user", args.smtp_user),
            ("--smtp-password", args.smtp_password),
            ("--smtp-from", args.smtp_from),
        ]
        if not value
    ]
    if missing:
        raise ValueError(f"method=email-gateway 缺少参数: {', '.join(missing)}")


def main() -> int:
    args = parse_args()

    try:
        validate_args(args)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
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
    print(f"发送方式：{args.method}")
    print("到点后将发送短信...", flush=True)

    wait_until(target)

    try:
        if args.method == "twilio":
            result = send_sms_twilio(
                account_sid=args.account_sid,
                auth_token=args.auth_token,
                from_number=args.from_number,
                to_number=args.to,
                body=args.message,
            )
            sid = result.get("sid", "<unknown>")
            status = result.get("status", "<unknown>")
            print(f"发送成功（Twilio），消息 SID: {sid}，状态: {status}")
        else:
            to_addr = send_sms_via_email_gateway(
                phone=args.to,
                carrier=args.carrier,
                body=args.message,
                smtp_host=args.smtp_host,
                smtp_port=args.smtp_port,
                smtp_user=args.smtp_user,
                smtp_password=args.smtp_password,
                smtp_from=args.smtp_from,
            )
            print(f"发送成功（Email Gateway）：{to_addr}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"发送失败（HTTP {exc.code}）：{body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误：{exc}", file=sys.stderr)
        return 1
    except (smtplib.SMTPException, ValueError) as exc:
        print(f"发送失败：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
