#!/usr/bin/env python3
"""在指定时间发送短信（支持 Twilio、邮箱网关、阿里云短信）。

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

示例 2：邮箱短信网关（通常免费，但不保证送达；主要适用于美国运营商）
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

示例 3：阿里云短信（中国大陆常用，模板短信）
python scripts/scheduled_sms.py \
  --method aliyun-sms \
  --to 13800138000 \
  --at "2026-04-23 09:30" \
  --timezone "Asia/Shanghai" \
  --aliyun-access-key-id "$ALIYUN_ACCESS_KEY_ID" \
  --aliyun-access-key-secret "$ALIYUN_ACCESS_KEY_SECRET" \
  --aliyun-sign-name "你的短信签名" \
  --aliyun-template-code "SMS_123456789" \
  --aliyun-template-param '{"code":"9527"}'
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from zoneinfo import ZoneInfo


TWILIO_API_TEMPLATE = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
ALIYUN_SMS_ENDPOINT = "https://dysmsapi.aliyuncs.com/"

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

    parser.add_argument("--method", choices=["twilio", "email-gateway", "aliyun-sms"], default="twilio", help="发送方式")
    parser.add_argument("--to", required=True, help="接收方手机号（推荐 E.164，如 +14155552671 或中国 13800138000）")
    parser.add_argument("--message", default=None, help="短信内容（twilio/email-gateway 使用）")
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

    # 阿里云短信参数（中国大陆常用）
    parser.add_argument("--aliyun-access-key-id", default=os.getenv("ALIYUN_ACCESS_KEY_ID"), help="阿里云 AccessKey ID")
    parser.add_argument("--aliyun-access-key-secret", default=os.getenv("ALIYUN_ACCESS_KEY_SECRET"), help="阿里云 AccessKey Secret")
    parser.add_argument("--aliyun-sign-name", default=os.getenv("ALIYUN_SMS_SIGN_NAME"), help="阿里云短信签名")
    parser.add_argument("--aliyun-template-code", default=os.getenv("ALIYUN_SMS_TEMPLATE_CODE"), help="阿里云短信模板代码")
    parser.add_argument("--aliyun-template-param", default=os.getenv("ALIYUN_SMS_TEMPLATE_PARAM", "{}"), help="模板变量 JSON，如 '{\"code\":\"9527\"}'")
    parser.add_argument("--aliyun-region", default=os.getenv("ALIYUN_REGION_ID", "cn-hangzhou"), help="阿里云地域，默认 cn-hangzhou")

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


def _aliyun_percent_encode(text: str) -> str:
    return urllib.parse.quote(text, safe="~")


def _build_aliyun_signature(parameters: dict[str, str], access_key_secret: str) -> str:
    sorted_items = sorted(parameters.items(), key=lambda x: x[0])
    canonicalized_query = "&".join(f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(v)}" for k, v in sorted_items)
    string_to_sign = f"POST&%2F&{_aliyun_percent_encode(canonicalized_query)}"
    digest = hmac.new(f"{access_key_secret}&".encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_sms_aliyun(
    access_key_id: str,
    access_key_secret: str,
    region_id: str,
    phone_number: str,
    sign_name: str,
    template_code: str,
    template_param_json: str,
) -> dict:
    # 仅校验是合法 JSON，不改写内容，确保与你在阿里云模板中定义的键名一致
    json.loads(template_param_json)

    parameters: dict[str, str] = {
        "AccessKeyId": access_key_id,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone_number,
        "RegionId": region_id,
        "SignName": sign_name,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "TemplateCode": template_code,
        "TemplateParam": template_param_json,
        "Timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
    }
    parameters["Signature"] = _build_aliyun_signature(parameters, access_key_secret)

    payload = urllib.parse.urlencode(parameters).encode("utf-8")
    request = urllib.request.Request(
        ALIYUN_SMS_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response_data = response.read().decode("utf-8")

    return json.loads(response_data)


def validate_args(args: argparse.Namespace) -> None:
    if args.method == "twilio":
        if not args.message:
            raise ValueError("method=twilio 时必须提供 --message")
        if not args.from_number:
            raise ValueError("method=twilio 时必须提供 --from-number")
        if not args.account_sid or not args.auth_token:
            raise ValueError("method=twilio 时必须提供 Twilio 凭据")
        return

    if args.method == "email-gateway":
        if not args.message:
            raise ValueError("method=email-gateway 时必须提供 --message")
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
        return

    # aliyun-sms
    missing = [
        name
        for name, value in [
            ("--aliyun-access-key-id", args.aliyun_access_key_id),
            ("--aliyun-access-key-secret", args.aliyun_access_key_secret),
            ("--aliyun-sign-name", args.aliyun_sign_name),
            ("--aliyun-template-code", args.aliyun_template_code),
            ("--aliyun-template-param", args.aliyun_template_param),
        ]
        if not value
    ]
    if missing:
        raise ValueError(f"method=aliyun-sms 缺少参数: {', '.join(missing)}")

    try:
        json.loads(args.aliyun_template_param)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--aliyun-template-param 不是合法 JSON: {exc}") from exc


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
        elif args.method == "email-gateway":
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
        else:
            result = send_sms_aliyun(
                access_key_id=args.aliyun_access_key_id,
                access_key_secret=args.aliyun_access_key_secret,
                region_id=args.aliyun_region,
                phone_number=args.to,
                sign_name=args.aliyun_sign_name,
                template_code=args.aliyun_template_code,
                template_param_json=args.aliyun_template_param,
            )
            code = result.get("Code", "<unknown>")
            biz_id = result.get("BizId", "<unknown>")
            message = result.get("Message", "<unknown>")
            if code != "OK":
                print(f"发送失败（Aliyun）：Code={code}, Message={message}, Response={result}", file=sys.stderr)
                return 1
            print(f"发送成功（Aliyun），BizId: {biz_id}，Message: {message}")
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
