from __future__ import annotations

import calendar
import html
import json
import logging
import os
import smtplib
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from math import ceil

import gspread
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from google.oauth2.service_account import Credentials


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("membership-bot")

WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "HoiVien")
DATE_FORMAT = "%Y-%m-%d"
MEMBER_CODE_PREFIX = "HV"

HEADERS = [
    "STT",
    "Họ tên",
    "Nick/Link Facebook",
    "SĐT",
    "Nguồn biết đến",
    "Ngày chuyển khoản",
    "Tên CK/Mã GD",
    "Số tiền",
    "Gói đăng ký",
    "Ngày vào nhóm",
    "Ngày hết hạn",
    "Số ngày còn lại",
    "Trạng thái tự động",
    "Đã nhắc gia hạn?",
    "Telegram ID",
    "Ghi chú",
    "Mã hội viên",
]

HISTORY_WORKSHEET_NAME = "LichSuGiaHan"
HISTORY_HEADERS = [
    "Thời gian",
    "Họ tên",
    "SĐT",
    "Gói cũ",
    "Ngày hết hạn cũ",
    "Gói mới",
    "Ngày vào nhóm mới",
    "Ngày hết hạn mới",
    "Số tiền",
    "Người thao tác",
    "Ghi chú",
]

COLORS = {
    "Còn hạn": {"red": 0.72, "green": 0.88, "blue": 0.64},
    "Sắp hết hạn": {"red": 1.0, "green": 0.92, "blue": 0.55},
    "Hết hạn": {"red": 0.96, "green": 0.62, "blue": 0.57},
    "Đã hủy": {"red": 0.86, "green": 0.88, "blue": 0.91},
}


@dataclass(frozen=True)
class RuntimeConfig:
    google_service_account_json: str
    google_sheet_id: str = ""
    google_spreadsheet_name: str = "ql-hoivien"
    check_interval_minutes: int = 5
    alert_before_days: int = 3
    alert_run_time: datetime_time | None = None
    alert_email_to: tuple[str, ...] = ()
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: tuple[str, ...] = ()
    telegram_add_required_fields: tuple[str, ...] = ("member_name", "months")
    telegram_digest_chat_ids: tuple[str, ...] = ()
    telegram_digest_run_time: datetime_time | None = None
    telegram_digest_days: int = 3
    discord_webhook_url: str = ""
    port: int = 8687


@dataclass(frozen=True)
class MembershipState:
    start_date: date | None
    end_date: date | None
    status: str
    remaining: str
    days_remaining: int | None


@dataclass(frozen=True)
class MemberSnapshot:
    row_index: int
    name: str
    package: str
    phone: str
    facebook: str
    source: str
    amount: str
    start_date: date | None
    end_date: date | None
    status: str
    remaining: str
    days_remaining: int | None
    note: str
    telegram_id: str = ""
    member_code: str = ""


@dataclass(frozen=True)
class TelegramReply:
    text: str
    reply_markup: dict[str, Any] | None = None


@dataclass(frozen=True)
class AddField:
    key: str
    label: str
    prompt: str


@dataclass
class AddSession:
    field_index: int
    values: dict[str, str]


ADD_FIELDS = [
    AddField("member_name", "Họ tên", "Nhập họ tên hội viên:"),
    AddField("facebook", "Nick/Link Facebook", "Nhập nick hoặc link Facebook:"),
    AddField("phone", "SĐT", "Nhập số điện thoại:"),
    AddField("source", "Nguồn biết đến", "Nhập nguồn biết đến:"),
    AddField("amount", "Số tiền", "Nhập số tiền:"),
    AddField("transaction_name", "Tên CK/Mã GD", "Nhập tên chuyển khoản hoặc mã giao dịch:"),
    AddField("months", "Gói đăng ký", "Nhập số tháng đăng ký:"),
    AddField("note", "Ghi chú", "Nhập ghi chú:"),
]
ADD_FIELD_KEYS = {field.key for field in ADD_FIELDS}
RENEW_FIELDS = [
    AddField("member_code", "Mã hội viên", "Nhập mã hội viên cần gia hạn:"),
    AddField("months", "Gói đăng ký", "Nhập số tháng gia hạn:"),
    AddField("amount", "Số tiền", "Nhập số tiền gia hạn:"),
]
SKIP_VALUES = {"", "-", "/skip", "skip", "bo qua", "bỏ qua"}


last_sync: dict[str, Any] = {
    "ok": None,
    "at": None,
    "message": "Bot has not synced yet.",
}
sheet_lock = threading.Lock()
add_sessions: dict[str, AddSession] = {}
add_sessions_lock = threading.Lock()
renew_sessions: dict[str, AddSession] = {}
renew_sessions_lock = threading.Lock()
email_sessions: set[str] = set()
email_sessions_lock = threading.Lock()
chatid_sessions: set[str] = set()
chatid_sessions_lock = threading.Lock()

app = FastAPI(title="Hoi Vien Membership Bot")


def load_config() -> RuntimeConfig:
    return RuntimeConfig(
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
        google_spreadsheet_name=os.getenv("GOOGLE_SPREADSHEET_NAME", "ql-hoivien").strip() or "ql-hoivien",
        google_service_account_json=require_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "5")),
        alert_before_days=int(os.getenv("ALERT_BEFORE_DAYS", "3")),
        alert_run_time=parse_alert_run_time(os.getenv("ALERT_RUN_TIME", "").strip()),
        alert_email_to=parse_csv_env("ALERT_EMAIL_TO"),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_chat_ids=parse_csv_env("TELEGRAM_ALLOWED_CHAT_IDS"),
        telegram_add_required_fields=parse_required_add_fields(os.getenv("TELEGRAM_ADD_REQUIRED_FIELDS", "")),
        telegram_digest_chat_ids=parse_csv_env("TELEGRAM_DIGEST_CHAT_IDS"),
        telegram_digest_run_time=parse_alert_run_time(os.getenv("TELEGRAM_DIGEST_RUN_TIME", "").strip()),
        telegram_digest_days=int(os.getenv("TELEGRAM_DIGEST_DAYS", os.getenv("ALERT_BEFORE_DAYS", "3"))),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        port=int(os.getenv("PORT", "8687")),
    )


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


def read_alert_email_recipients() -> tuple[str, ...]:
    return parse_csv_value(os.getenv("ALERT_EMAIL_TO", ""))


def parse_csv_value(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_required_add_fields(value: str) -> tuple[str, ...]:
    fields = tuple(item.strip() for item in value.split(",") if item.strip())
    if not fields:
        return ("member_name", "months")
    valid_fields = tuple(field for field in fields if field in ADD_FIELD_KEYS)
    return valid_fields or ("member_name", "months")


def merge_csv_values(existing: str, new_value: str) -> str:
    items: list[str] = []
    for value in parse_csv_value(existing) + (new_value.strip(),):
        if value and value not in items:
            items.append(value)
    return ",".join(items)


def parse_alert_run_time(value: str) -> datetime_time | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise RuntimeError(f"ALERT_RUN_TIME must use HH:MM format, got: {value}") from exc


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (DATE_FORMAT, "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    logger.warning("Ignoring invalid date value: %s", text)
    return None


def parse_months(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.lower().replace("tháng", "").replace("thang", "").strip()
    try:
        months = int(float(text.replace(",", ".")))
    except ValueError:
        logger.warning("Ignoring invalid month value: %s", text)
        return None
    return months if months > 0 else None


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def format_date(value: date | None) -> str:
    return value.strftime(DATE_FORMAT) if value else ""


def normalize_member_code(value: Any) -> str:
    return str(value or "").strip().upper()


def generate_member_code(seed_number: int, used_codes: set[str]) -> str:
    number = max(1, seed_number)
    while True:
        code = f"{MEMBER_CODE_PREFIX}{number:06d}"
        if code not in used_codes:
            return code
        number += 1


def sheet_range(start_row: int, end_row: int | None = None) -> str:
    end_row = end_row or start_row
    return f"A{start_row}:Q{end_row}"


def calculate_state(
    member_name: str,
    start_value: Any,
    months_value: Any,
    today: date,
    alert_before_days: int,
) -> MembershipState:
    start_date = parse_date(start_value)
    months = parse_months(months_value)

    if member_name.strip() and start_date is None:
        start_date = today

    end_date = add_months(start_date, months) if start_date and months else None
    if end_date is None:
        return MembershipState(start_date, None, "", "", None)

    days_remaining = (end_date - today).days
    if days_remaining < 0:
        status = "Hết hạn"
        remaining = f"Đã hết hạn {abs(days_remaining)} ngày"
    elif 0 <= days_remaining <= alert_before_days:
        status = "Sắp hết hạn"
        remaining = f"{days_remaining} ngày"
    else:
        status = "Còn hạn"
        if days_remaining > 30:
            remaining = f"{max(1, days_remaining // 30)} tháng"
        else:
            remaining = f"{days_remaining} ngày"

    return MembershipState(start_date, end_date, status, remaining, days_remaining)


def credentials_from_env(service_account_json: str) -> Credentials:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if os.path.exists(service_account_json):
        return Credentials.from_service_account_file(service_account_json, scopes=scopes)
    return Credentials.from_service_account_info(json.loads(service_account_json), scopes=scopes)


def get_worksheet(config: RuntimeConfig) -> gspread.Worksheet:
    credentials = credentials_from_env(config.google_service_account_json)
    client = gspread.authorize(credentials)
    spreadsheet = get_spreadsheet(client, config)
    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=200, cols=len(HEADERS))

    first_row = worksheet.row_values(1)
    if first_row[: len(HEADERS)] != HEADERS:
        worksheet.update([HEADERS], "A1:Q1")
        worksheet.format(
            "A1:Q1",
            {
                "horizontalAlignment": "CENTER",
                "backgroundColor": {"red": 0.75, "green": 0.0, "blue": 0.0},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            },
        )
        worksheet.freeze(rows=1)
    worksheet.format(f"A2:Q{worksheet.row_count}", {"horizontalAlignment": "RIGHT"})
    return worksheet


def get_history_worksheet(config: RuntimeConfig) -> gspread.Worksheet:
    credentials = credentials_from_env(config.google_service_account_json)
    client = gspread.authorize(credentials)
    spreadsheet = get_spreadsheet(client, config)
    try:
        worksheet = spreadsheet.worksheet(HISTORY_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=HISTORY_WORKSHEET_NAME, rows=200, cols=len(HISTORY_HEADERS))

    first_row = worksheet.row_values(1)
    if first_row[: len(HISTORY_HEADERS)] != HISTORY_HEADERS:
        worksheet.update([HISTORY_HEADERS], "A1:K1")
        worksheet.format(
            "A1:K1",
            {
                "horizontalAlignment": "CENTER",
                "backgroundColor": {"red": 0.14, "green": 0.24, "blue": 0.42},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            },
        )
        worksheet.freeze(rows=1)
    worksheet.format(f"A2:K{worksheet.row_count}", {"horizontalAlignment": "RIGHT"})
    return worksheet


def format_member_row_right(worksheet: gspread.Worksheet, row_index: int) -> None:
    worksheet.format(
        f"A{row_index}:Q{row_index}",
        {
            "horizontalAlignment": "RIGHT",
        },
    )


def format_history_row_right(worksheet: gspread.Worksheet, row_index: int) -> None:
    worksheet.format(
        f"A{row_index}:K{row_index}",
        {
            "horizontalAlignment": "RIGHT",
        },
    )


def get_spreadsheet(client: gspread.Client, config: RuntimeConfig) -> gspread.Spreadsheet:
    if config.google_sheet_id:
        return client.open_by_key(config.google_sheet_id)
    try:
        return client.open(config.google_spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        logger.info("Creating spreadsheet named %s", config.google_spreadsheet_name)
        return client.create(config.google_spreadsheet_name)


def sync_sheet(config: RuntimeConfig) -> dict[str, Any]:
    with sheet_lock:
        return sync_sheet_unlocked(config)


def sync_sheet_unlocked(config: RuntimeConfig) -> dict[str, Any]:
    worksheet = get_worksheet(config)
    rows = worksheet.get_all_records(expected_headers=HEADERS)
    today = date.today()
    changed_rows = 0
    alerted_rows = 0
    used_member_codes = {
        normalize_member_code(row.get("Mã hội viên"))
        for row in rows
        if normalize_member_code(row.get("Mã hội viên"))
    }

    for index, row in enumerate(rows, start=2):
        member_name = str(row.get("Họ tên", "")).strip()
        if not member_name:
            continue
        member_code = normalize_member_code(row.get("Mã hội viên"))
        if not member_code:
            member_code = generate_member_code(index - 1, used_member_codes)
        used_member_codes.add(member_code)

        existing_status = str(row.get("Trạng thái tự động", "") or "").strip()
        if existing_status == "Đã hủy":
            updated_values = [
                str(row.get("STT", "") or index - 1).strip(),
                member_name,
                str(row.get("Nick/Link Facebook", "") or "").strip(),
                str(row.get("SĐT", "") or "").strip(),
                str(row.get("Nguồn biết đến", "") or "").strip(),
                str(row.get("Ngày chuyển khoản", "") or "").strip(),
                str(row.get("Tên CK/Mã GD", "") or "").strip(),
                str(row.get("Số tiền", "") or "").strip(),
                "",
                "",
                "",
                "",
                "Đã hủy",
                "",
                str(row.get("Telegram ID", "") or extract_telegram_chat_id(row) or "").strip(),
                str(row.get("Ghi chú", "") or "").strip(),
                member_code,
            ]
            current_values = [str(row.get(header, "") or "").strip() for header in HEADERS]
            if current_values != updated_values:
                worksheet.update([updated_values], sheet_range(index))
                changed_rows += 1
            worksheet.format(f"M{index}", {"backgroundColor": COLORS["Đã hủy"]})
            continue

        old_end_date = parse_date(row.get("Ngày hết hạn"))
        state = calculate_state(
            member_name,
            row.get("Ngày vào nhóm"),
            row.get("Gói đăng ký"),
            today,
            config.alert_before_days,
        )

        alert_value = str(row.get("Đã nhắc gia hạn?", "") or "").strip()
        if old_end_date and state.end_date and old_end_date != state.end_date:
            alert_value = ""

        sent_channels: list[str] = []
        if should_alert(config, state, alert_value):
            sent_channels = send_alerts(config, row, state)
            if sent_channels:
                alert_value = f"{format_date(today)} {','.join(sent_channels)} END={format_date(state.end_date)}"
                alerted_rows += 1

        updated_values = [
            str(row.get("STT", "") or index - 1).strip(),
            member_name,
            str(row.get("Nick/Link Facebook", "") or "").strip(),
            str(row.get("SĐT", "") or "").strip(),
            str(row.get("Nguồn biết đến", "") or "").strip(),
            str(row.get("Ngày chuyển khoản", "") or "").strip(),
            str(row.get("Tên CK/Mã GD", "") or "").strip(),
            str(row.get("Số tiền", "") or "").strip(),
            str(row.get("Gói đăng ký", "") or "").strip(),
            format_date(state.start_date),
            format_date(state.end_date),
            state.remaining,
            state.status,
            alert_value,
            str(row.get("Telegram ID", "") or extract_telegram_chat_id(row) or "").strip(),
            str(row.get("Ghi chú", "") or "").strip(),
            member_code,
        ]

        current_values = [str(row.get(header, "") or "").strip() for header in HEADERS]
        if current_values != updated_values:
            worksheet.update([updated_values], sheet_range(index))
            changed_rows += 1

        if state.status:
            worksheet.format(f"M{index}", {"backgroundColor": COLORS[state.status]})

    return {
        "rows": len(rows),
        "changed_rows": changed_rows,
        "alerted_rows": alerted_rows,
    }


def load_member_snapshots(config: RuntimeConfig) -> list[MemberSnapshot]:
    with sheet_lock:
        worksheet = get_worksheet(config)
        rows = worksheet.get_all_records(expected_headers=HEADERS)
    today = date.today()
    snapshots: list[MemberSnapshot] = []
    for index, row in enumerate(rows, start=2):
        member_name = str(row.get("Họ tên", "")).strip()
        if not member_name:
            continue
        state = calculate_state(
            member_name,
            row.get("Ngày vào nhóm"),
            row.get("Gói đăng ký"),
            today,
            config.alert_before_days,
        )
        member_code = normalize_member_code(row.get("Mã hội viên"))
        stored_status = str(row.get("Trạng thái tự động", "") or "").strip()
        status = "Đã hủy" if stored_status == "Đã hủy" else state.status
        remaining = "" if status == "Đã hủy" else state.remaining
        snapshots.append(
            MemberSnapshot(
                row_index=index,
                name=member_name,
                package=str(row.get("Gói đăng ký", "") or "").strip(),
                phone=str(row.get("SĐT", "") or "").strip(),
                facebook=str(row.get("Nick/Link Facebook", "") or "").strip(),
                source=str(row.get("Nguồn biết đến", "") or "").strip(),
                amount=str(row.get("Số tiền", "") or "").strip(),
                start_date=state.start_date,
                end_date=state.end_date,
                status=status,
                remaining=remaining,
                days_remaining=state.days_remaining,
                note=str(row.get("Ghi chú", "") or "").strip(),
                telegram_id=extract_telegram_chat_id(row),
                member_code=member_code,
            )
        )
    return snapshots


def sort_member_snapshots(members: list[MemberSnapshot]) -> list[MemberSnapshot]:
    return sorted(
        members,
        key=lambda member: (
            member.end_date is None,
            member.end_date or date.max,
            member.days_remaining is None,
            member.days_remaining if member.days_remaining is not None else 999999,
            member.name.casefold(),
        ),
    )


def paginate_members(members: list[MemberSnapshot], page: int, per_page: int = 100) -> tuple[list[MemberSnapshot], int]:
    total_pages = max(1, ceil(len(members) / per_page)) if members else 1
    safe_page = min(max(1, page), total_pages)
    start = (safe_page - 1) * per_page
    end = start + per_page
    return members[start:end], total_pages


def add_member_from_telegram(
    config: RuntimeConfig,
    member_name: str,
    months: str,
    phone: str,
    facebook: str,
    source: str,
    amount: str,
    transaction_name: str,
    telegram_chat_id: str,
    note: str,
) -> tuple[MembershipState, str]:
    today = date.today()
    state = calculate_state(member_name, "", months, today, config.alert_before_days)
    with sheet_lock:
        worksheet = get_worksheet(config)
        rows = worksheet.get_all_records(expected_headers=HEADERS)
        used_member_codes = {
            normalize_member_code(row.get("Mã hội viên"))
            for row in rows
            if normalize_member_code(row.get("Mã hội viên"))
        }
        row_number = len(worksheet.get_all_values())
        member_code = generate_member_code(row_number, used_member_codes)
        values = [
            "",
            member_name.strip(),
            facebook.strip(),
            phone.strip(),
            source.strip(),
            format_date(today),
            transaction_name.strip(),
            amount.strip(),
            months.strip(),
            format_date(state.start_date),
            format_date(state.end_date),
            state.remaining,
            state.status,
            "",
            telegram_chat_id.strip(),
            note.strip(),
            member_code,
        ]
        values[0] = str(row_number)
        worksheet.append_row(values, value_input_option="USER_ENTERED")
        row_index = len(worksheet.get_all_values())
        format_member_row_right(worksheet, row_index)
        if state.status:
            worksheet.format(f"M{row_index}", {"backgroundColor": COLORS[state.status]})
    return state, member_code


def renew_member_from_telegram(
    config: RuntimeConfig,
    member_code: str,
    months: str,
    amount: str = "",
    actor: str = "",
    note: str = "",
) -> MembershipState | None:
    today = date.today()
    with sheet_lock:
        worksheet = get_worksheet(config)
        rows = worksheet.get_all_records(expected_headers=HEADERS)
        for index, row in enumerate(rows, start=2):
            current_code = normalize_member_code(row.get("Mã hội viên"))
            current_name = str(row.get("Họ tên", "")).strip()
            if current_code != normalize_member_code(member_code):
                continue

            old_package = str(row.get("Gói đăng ký", "") or "").strip()
            old_end_date = format_date(parse_date(row.get("Ngày hết hạn")))
            state = calculate_state(current_name, format_date(today), months, today, config.alert_before_days)
            values = [
                str(row.get("STT", "") or index - 1).strip(),
                current_name,
                str(row.get("Nick/Link Facebook", "") or "").strip(),
                str(row.get("SĐT", "") or "").strip(),
                str(row.get("Nguồn biết đến", "") or "").strip(),
                str(row.get("Ngày chuyển khoản", "") or "").strip(),
                str(row.get("Tên CK/Mã GD", "") or "").strip(),
                str(row.get("Số tiền", "") or "").strip(),
                months.strip(),
                format_date(state.start_date),
                format_date(state.end_date),
                state.remaining,
                state.status,
                "",
                str(row.get("Telegram ID", "") or extract_telegram_chat_id(row) or "").strip(),
                str(row.get("Ghi chú", "") or "").strip(),
                current_code,
            ]
            worksheet.update([values], sheet_range(index))
            format_member_row_right(worksheet, index)
            if state.status:
                worksheet.format(f"M{index}", {"backgroundColor": COLORS[state.status]})
            append_renew_history_unlocked(
                config,
                current_name,
                str(row.get("SĐT", "") or "").strip(),
                old_package,
                old_end_date,
                months.strip(),
                format_date(state.start_date),
                format_date(state.end_date),
                amount,
                actor,
                build_history_note(classify_package_change(old_package, months), note),
            )
            return state
    return None


def cancel_member_from_telegram(
    config: RuntimeConfig,
    member_code: str,
    actor: str = "",
    note: str = "",
) -> bool:
    with sheet_lock:
        worksheet = get_worksheet(config)
        rows = worksheet.get_all_records(expected_headers=HEADERS)
        for index, row in enumerate(rows, start=2):
            current_code = normalize_member_code(row.get("Mã hội viên"))
            current_name = str(row.get("Họ tên", "")).strip()
            if current_code != normalize_member_code(member_code):
                continue

            old_package = str(row.get("Gói đăng ký", "") or "").strip()
            old_end_date = format_date(parse_date(row.get("Ngày hết hạn")))
            values = [
                str(row.get("STT", "") or index - 1).strip(),
                current_name,
                str(row.get("Nick/Link Facebook", "") or "").strip(),
                str(row.get("SĐT", "") or "").strip(),
                str(row.get("Nguồn biết đến", "") or "").strip(),
                str(row.get("Ngày chuyển khoản", "") or "").strip(),
                str(row.get("Tên CK/Mã GD", "") or "").strip(),
                str(row.get("Số tiền", "") or "").strip(),
                "",
                "",
                "",
                "",
                "Đã hủy",
                "",
                str(row.get("Telegram ID", "") or extract_telegram_chat_id(row) or "").strip(),
                str(row.get("Ghi chú", "") or "").strip(),
                current_code,
            ]
            worksheet.update([values], sheet_range(index))
            format_member_row_right(worksheet, index)
            worksheet.format(f"M{index}", {"backgroundColor": COLORS["Đã hủy"]})
            append_renew_history_unlocked(
                config,
                current_name,
                str(row.get("SĐT", "") or "").strip(),
                old_package,
                old_end_date,
                "Hủy",
                "",
                "",
                "",
                actor,
                build_history_note("hủy", note),
            )
            return True
    return False


def classify_package_change(old_package: str, new_package: str) -> str:
    old_months = parse_months(old_package)
    new_months = parse_months(new_package)
    if old_months is None or new_months is None:
        return "gia hạn"
    if new_months > old_months:
        return "nâng cấp"
    if new_months < old_months:
        return "hạ cấp"
    return "gia hạn"


def build_history_note(action: str, note: str) -> str:
    clean_action = action.strip() or "gia hạn"
    clean_note = note.strip()
    if clean_note:
        return f"Hành động: {clean_action}; {clean_note}"
    return f"Hành động: {clean_action}"


def append_renew_history_unlocked(
    config: RuntimeConfig,
    member_name: str,
    phone: str,
    old_package: str,
    old_end_date: str,
    new_package: str,
    new_start_date: str,
    new_end_date: str,
    amount: str,
    actor: str,
    note: str,
) -> None:
    worksheet = get_history_worksheet(config)
    worksheet.append_row(
        [
            datetime.now().isoformat(timespec="seconds"),
            member_name,
            phone,
            old_package,
            old_end_date,
            new_package,
            new_start_date,
            new_end_date,
            amount.strip(),
            actor.strip(),
            note.strip(),
        ],
        value_input_option="USER_ENTERED",
    )
    format_history_row_right(worksheet, len(worksheet.get_all_values()))


def load_renew_history(config: RuntimeConfig) -> list[dict[str, Any]]:
    with sheet_lock:
        worksheet = get_history_worksheet(config)
        return worksheet.get_all_records(expected_headers=HISTORY_HEADERS)


def handle_history_command(config: RuntimeConfig, payload: str) -> str:
    query = payload.strip()
    if not query:
        return "Dùng: /history <tên hoặc SĐT>"

    rows = load_renew_history(config)
    needle = query.casefold()
    matched = [
        row
        for row in rows
        if needle in str(row.get("Họ tên", "") or "").casefold()
        or needle in str(row.get("SĐT", "") or "").casefold()
    ]
    if not matched:
        return f"Không có lịch sử gia hạn cho: {query}"

    matched = matched[-10:]
    table_lines = [f"{'Ngày':<16} {'Tên':<18} {'Cũ':<4} {'Mới':<4} {'Hết hạn mới':<10} {'Tiền':<10}"]
    table_lines.append(f"{'-' * 16} {'-' * 18} {'-' * 4} {'-' * 4} {'-' * 10} {'-' * 10}")
    for row in matched:
        table_lines.append(
            f"{fixed_width(str(row.get('Thời gian', '') or '-'), 16)} "
            f"{fixed_width(str(row.get('Họ tên', '') or '-'), 18)} "
            f"{fixed_width(str(row.get('Gói cũ', '') or '-'), 4)} "
            f"{fixed_width(str(row.get('Gói mới', '') or '-'), 4)} "
            f"{fixed_width(str(row.get('Ngày hết hạn mới', '') or '-'), 10)} "
            f"{fixed_width(str(row.get('Số tiền', '') or '-'), 10)}"
        )
    return f"Lịch sử gia hạn: {query} ({len(matched)})\n<pre>{html.escape(chr(10).join(table_lines))}</pre>"


def should_alert(config: RuntimeConfig, state: MembershipState, alert_value: str) -> bool:
    if state.end_date is None or state.days_remaining is None:
        return False
    if state.days_remaining < 0:
        return False
    if state.status != "Sắp hết hạn":
        return False
    if not is_alert_time(config, datetime.now().time()):
        return False
    return f"END={format_date(state.end_date)}" not in alert_value


def is_alert_time(config: RuntimeConfig, current_time: datetime_time) -> bool:
    if config.alert_run_time is None:
        return True
    return current_time >= config.alert_run_time


def send_alerts(config: RuntimeConfig, row: dict[str, Any], state: MembershipState) -> list[str]:
    member_name = str(row.get("Họ tên", "")).strip()
    message = build_alert_message(member_name, state)
    sent_channels: list[str] = []
    note = str(row.get("Ghi chú", "") or "")

    for recipient in read_alert_email_recipients():
        if config.smtp_user and config.smtp_password:
            try:
                send_email(config, recipient, member_name, message)
                sent_channels.append("alert_email")
            except Exception:
                logger.exception("Failed to send admin email alert to %s", recipient)

    email = extract_note_value(note, "email")
    if email and config.smtp_user and config.smtp_password:
        try:
            send_email(config, email, member_name, message)
            sent_channels.append("email")
        except Exception:
            logger.exception("Failed to send email alert to %s", email)

    chat_id = extract_telegram_chat_id(row)
    if chat_id and config.telegram_bot_token:
        try:
            send_telegram(config, chat_id, message)
            sent_channels.append("telegram")
        except Exception:
            logger.exception("Failed to send Telegram alert to %s", chat_id)

    discord_target = extract_note_value(note, "discord")
    webhook_url = discord_target if discord_target.startswith("https://") else config.discord_webhook_url
    if webhook_url:
        try:
            content = f"{discord_target} {message}" if discord_target and not discord_target.startswith("https://") else message
            send_discord(webhook_url, content)
            sent_channels.append("discord")
        except Exception:
            logger.exception("Failed to send Discord alert")

    if not sent_channels:
        logger.info("No alert channel configured for member %s", member_name)
    return sent_channels


def handle_test_alert_command(config: RuntimeConfig, chat_id: str, payload: str) -> str:
    days = parse_optional_limit(payload, default=config.alert_before_days)
    recipients = read_alert_email_recipients()
    if not recipients:
        return "Chưa có email quản trị. Dùng /addemail để thêm người nhận vào ALERT_EMAIL_TO."
    if not config.smtp_user or not config.smtp_password:
        return "Chưa cấu hình SMTP_USER hoặc SMTP_PASSWORD nên chưa gửi email được."

    snapshots = load_member_snapshots(config)
    expiring_members = filter_expiring_members(snapshots, days)
    subject = build_admin_expiring_subject(expiring_members, days)
    body = build_admin_expiring_email_body(expiring_members, days)
    sent: list[str] = []
    for recipient in recipients:
        try:
            send_email_with_subject(config, recipient, subject, body)
            sent.append(recipient)
        except Exception:
            logger.exception("Failed to send admin expiring digest to %s", recipient)

    if not sent:
        return "Không gửi được email cảnh báo thử. Kiểm tra SMTP_USER, SMTP_PASSWORD và log bot."
    return (
        "Đã gửi email danh sách hội viên sắp hết hạn cho quản trị.\n"
        f"Người nhận: {', '.join(sent)}\n"
        f"Số hội viên trong danh sách: {len(expiring_members)}"
    )


def extract_note_value(note: str, key: str) -> str:
    prefix = f"{key}:"
    for part in note.replace("\n", " ").split():
        if part.lower().startswith(prefix):
            return part[len(prefix) :].strip()
    return ""


def extract_telegram_chat_id(row: dict[str, Any]) -> str:
    telegram_id = str(row.get("Telegram ID", "") or "").strip()
    if telegram_id:
        return telegram_id
    note = str(row.get("Ghi chú", "") or "")
    return extract_note_value(note, "telegram")


def build_alert_message(member_name: str, state: MembershipState) -> str:
    if state.status == "Hết hạn":
        return (
            f"Hội viên {member_name} đã hết hạn. "
            f"Ngày hết hạn: {format_date(state.end_date)}. "
            f"Thời hạn còn lại: {state.remaining}."
        )
    return (
        f"Hội viên {member_name} sắp hết hạn. "
        f"Ngày hết hạn: {format_date(state.end_date)}. "
        f"Thời hạn còn lại: {state.remaining}."
    )


def filter_expiring_members(members: list[MemberSnapshot], days: int) -> list[MemberSnapshot]:
    filtered = [
        member
        for member in members
        if member.days_remaining is not None and 0 <= member.days_remaining <= days
    ]
    filtered.sort(key=lambda member: (member.days_remaining if member.days_remaining is not None else 9999, member.name.casefold()))
    return filtered


def build_admin_expiring_subject(members: list[MemberSnapshot], days: int) -> str:
    return f"Danh sách hội viên sắp hết hạn trong {days} ngày ({len(members)})"


def build_admin_expiring_email_body(members: list[MemberSnapshot], days: int) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Danh sách hội viên sắp hết hạn trong {days} ngày.",
        f"Thời điểm tạo: {generated_at}",
        f"Tổng số: {len(members)}",
        "",
    ]
    if not members:
        lines.append("Hiện không có hội viên nào sắp hết hạn trong ngưỡng này.")
        return "\n".join(lines)

    lines.append("STT | Mã HV | Họ tên | Gói | Ngày hết hạn | Còn lại | SĐT")
    lines.append("-" * 82)
    for index, member in enumerate(members, start=1):
        lines.append(
            " | ".join(
                [
                    str(index),
                    member.member_code or "-",
                    member.name,
                    member.package or "-",
                    format_date(member.end_date) or "-",
                    member.remaining or "-",
                    member.phone or "-",
                ]
            )
        )
    return "\n".join(lines)


def build_telegram_expiring_digest(members: list[MemberSnapshot], days: int) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Danh sách hội viên sắp hết hạn trong {days} ngày",
        f"Cập nhật: {generated_at}",
        f"Tổng số: {len(members)}",
        "",
    ]
    if not members:
        lines.append("Không có hội viên nào sắp hết hạn trong ngưỡng này.")
        return "\n".join(lines)

    lines.append(format_telegram_member_table(members, limit=len(members)))
    return "\n".join(lines)


def send_telegram_expiring_digest(config: RuntimeConfig, chat_ids: tuple[str, ...]) -> tuple[list[str], int]:
    snapshots = load_member_snapshots(config)
    members = filter_expiring_members(snapshots, config.telegram_digest_days)
    message = build_telegram_expiring_digest(members, config.telegram_digest_days)
    sent: list[str] = []
    for chat_id in chat_ids:
        try:
            telegram_reply(config, chat_id, message)
            sent.append(chat_id)
        except Exception:
            logger.exception("Failed to send Telegram digest to chat_id=%s", chat_id)
    return sent, len(members)


def send_email(config: RuntimeConfig, to_address: str, member_name: str, body: str) -> None:
    send_email_with_subject(config, to_address, f"Nhắc hạn hội viên: {member_name}", body)


def send_email_with_subject(config: RuntimeConfig, to_address: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.smtp_user
    message["To"] = to_address
    message.set_content(body)

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)


def send_telegram(config: RuntimeConfig, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=30,
    )
    response.raise_for_status()


def telegram_api(config: RuntimeConfig, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/{method}",
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def telegram_set_commands(config: RuntimeConfig) -> None:
    commands = [
        {"command": "menu", "description": "Mở menu quản lý"},
        {"command": "help", "description": "Xem hướng dẫn sử dụng"},
        {"command": "add", "description": "Thêm hội viên"},
        {"command": "list", "description": "Xem danh sách hội viên"},
        {"command": "active", "description": "Xem hội viên còn hạn"},
        {"command": "expiring", "description": "Xem hội viên sắp hết hạn"},
        {"command": "expired", "description": "Xem hội viên đã hết hạn"},
        {"command": "cancelled", "description": "Xem hội viên đã hủy"},
        {"command": "search", "description": "Tìm hội viên"},
        {"command": "renew", "description": "Gia hạn theo mã hội viên"},
        {"command": "huy", "description": "Hủy theo mã hội viên"},
        {"command": "id", "description": "Xem Telegram chat ID"},
        {"command": "addchatid", "description": "Thêm group chat ID"},
        {"command": "addemail", "description": "Thêm email nhận cảnh báo"},
        {"command": "senddigest", "description": "Gửi danh sách sắp hết hạn vào group"},
        {"command": "testalert", "description": "Gửi cảnh báo thử"},
        {"command": "history", "description": "Xem lịch sử gia hạn"},
        {"command": "health", "description": "Kiểm tra trạng thái bot"},
        {"command": "stats", "description": "Xem thống kê hội viên"},
    ]
    telegram_api(config, "setMyCommands", {"commands": commands})


def telegram_get_updates(config: RuntimeConfig, offset: int | None) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": 50, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    data = telegram_api(config, "getUpdates", payload)
    return data.get("result", [])


def telegram_loop(config: RuntimeConfig) -> None:
    if not config.telegram_bot_token:
        logger.info("Telegram bot token is empty; Telegram input is disabled.")
        return

    offset: int | None = None
    logger.info("Telegram input polling started")
    telegram_set_commands(config)
    while True:
        try:
            updates = telegram_get_updates(config, offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = str(message.get("text", "") or "")
                if not chat_id or not text:
                    continue
                if config.telegram_allowed_chat_ids and chat_id not in config.telegram_allowed_chat_ids:
                    logger.warning("Ignoring Telegram message from unauthorized chat_id=%s", chat_id)
                    continue
                reply = handle_telegram_command(config, chat_id, text)
                telegram_reply(config, chat_id, reply.text, reply.reply_markup)
        except Exception:
            logger.exception("Telegram polling failed")
            time.sleep(10)


def handle_telegram_command(config: RuntimeConfig, chat_id: str, text: str) -> TelegramReply:
    command_text = text.lstrip()
    command, _, raw_payload = command_text.partition(" ")
    command = command.split("@", 1)[0].lower()
    payload = raw_payload.strip()

    if command == "/cancel":
        if has_email_session(chat_id):
            return TelegramReply(cancel_email_session(chat_id), telegram_menu_markup())
        if has_chatid_session(chat_id):
            return TelegramReply(cancel_chatid_session(chat_id), telegram_menu_markup())
        if has_renew_session(chat_id):
            return TelegramReply(cancel_renew_session(chat_id), telegram_menu_markup())
        return TelegramReply(cancel_add_session(chat_id), telegram_menu_markup())
    if has_email_session(chat_id):
        return TelegramReply(handle_email_session_input(config, chat_id, text), telegram_menu_markup())
    if has_chatid_session(chat_id):
        return TelegramReply(handle_chatid_session_input(config, chat_id, text), telegram_menu_markup())
    if has_renew_session(chat_id):
        return TelegramReply(handle_renew_session_input(config, chat_id, text), renew_session_markup(config, chat_id))
    if has_add_session(chat_id):
        return TelegramReply(handle_add_session_input(config, chat_id, text), add_session_markup(config, chat_id))
    if command == "/start":
        return TelegramReply(telegram_start_text(chat_id), telegram_menu_markup())
    if command in ("/help", "/menu"):
        return TelegramReply(telegram_help_text(chat_id), telegram_menu_markup())
    if command == "/id":
        return TelegramReply(f"Telegram Chat ID của cuộc trò chuyện này: {chat_id}")
    if command in ("/health", "/status"):
        return TelegramReply(format_telegram_health_text())
    if command in ("/add", "/them"):
        return TelegramReply(start_add_session(config, chat_id), add_session_markup(config, chat_id))
    if command in ("/addemail", "/email", "/addalertemail"):
        return TelegramReply(start_email_session(chat_id), email_session_markup())
    if command in ("/addchatid", "/addgroupid", "/groupid"):
        return TelegramReply(start_chatid_session(chat_id), chatid_session_markup())
    if command in ("/testalert", "/test", "/testnotify"):
        return TelegramReply(handle_test_alert_command(config, chat_id, payload))
    if command in ("/senddigest", "/testdigest", "/digest"):
        return TelegramReply(handle_send_digest_command(config))
    if command in ("/renew", "/giahan"):
        return TelegramReply(handle_renew_command(config, chat_id, payload), renew_session_markup(config, chat_id) if has_renew_session(chat_id) else None)
    if command in ("/huy", "/cancelmember", "/cancelmembership"):
        return TelegramReply(handle_cancel_member_command(config, chat_id, payload))
    if command == "/history":
        return TelegramReply(handle_history_command(config, payload))
    if command in ("/list", "/members", "/ds", "/danhsach"):
        return TelegramReply(handle_list_command(config, payload))
    if command in ("/expiring", "/soon", "/saphethan"):
        return TelegramReply(handle_expiring_command(config, payload))
    if command in ("/expired", "/het", "/hethan"):
        return TelegramReply(handle_status_list_command(config, "Hết hạn"))
    if command in ("/cancelled", "/dahuy"):
        return TelegramReply(handle_status_list_command(config, "Đã hủy"))
    if command in ("/active", "/conhan"):
        return TelegramReply(handle_status_list_command(config, "Còn hạn"))
    if command == "/search":
        return TelegramReply(handle_search_command(config, payload))
    if command == "/stats":
        return TelegramReply(handle_stats_command(config))
    return TelegramReply("Lệnh không hợp lệ. Gửi /menu để xem các chức năng.")


def telegram_help_text(chat_id: str) -> str:
    return (
        "Các lệnh quản lý hội viên:\n"
        "/menu hoặc /help - xem menu\n"
        "/add - thêm hội viên theo từng bước\n"
        "/list - danh sách hội viên\n"
        "/active - hội viên còn hạn\n"
        "/expiring [ngày] - hội viên sắp hết hạn\n"
        "/expired - hội viên đã hết hạn\n"
        "/cancelled - hội viên đã hủy\n"
        "/search <từ khóa> - tìm hội viên\n"
        "/renew Mã hội viên | Gói đăng ký | Số tiền - gia hạn hội viên\n"
        "/huy Mã hội viên - hủy hội viên\n"
        "/history <tên hoặc SĐT> - xem lịch sử gia hạn\n"
        "/id - xem Telegram chat ID\n"
        "\nLệnh cấu hình và test:\n"
        "/addchatid - thêm group chat ID vào .env\n"
        "/addemail - thêm email nhận cảnh báo vào .env\n"
        "/senddigest - gửi danh sách sắp hết hạn vào group ngay\n"
        "/testalert - gửi email thử cho quản trị\n"
        "/health - trạng thái bot và sync\n"
        "/stats - thống kê nhanh\n\n"
        "Cách nhập hội viên:\n"
        "/add - bot sẽ hỏi từng trường để lưu vào Google Sheet.\n"
        "Mặc định bắt buộc Họ tên và Gói đăng ký. Các trường khác có thể gõ dấu chấm (.) để bỏ qua.\n"
        "Có thể đổi trường bắt buộc bằng TELEGRAM_ADD_REQUIRED_FIELDS trong .env.\n\n"
        "Bot tự lưu chat ID vào cột Telegram ID để nhắc gia hạn.\n\n"
        "Ví dụ: /renew HV000001 | 6 | 300000 hoặc /huy HV000001\n\n"
        f"Chat hiện tại: {chat_id}"
    )


def telegram_start_text(chat_id: str) -> str:
    return (
        "Bot quản lý hội viên đã sẵn sàng.\n"
        "Dùng /add để thêm mới, /renew để gia hạn/nâng cấp/hạ cấp, /huy để hủy hội viên, hoặc /menu để mở các lệnh quản lý.\n\n"
        f"Chat ID hiện tại: {chat_id}"
    )


def telegram_menu_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "/add"}, {"text": "/renew "}],
            [{"text": "/huy "}],
            [{"text": "/list"}, {"text": "/active"}],
            [{"text": "/expiring"}, {"text": "/expired"}],
            [{"text": "/cancelled"}],
            [{"text": "/search "}, {"text": "/history "}],
            [{"text": "/id"}],
            [{"text": "/addchatid"}, {"text": "/addemail"}],
            [{"text": "/senddigest"}, {"text": "/testalert"}],
            [{"text": "/health"}, {"text": "/stats"}],
            [{"text": "/help"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def format_telegram_health_text() -> str:
    status = last_sync.get("message")
    at = last_sync.get("at") or "chưa có lần sync nào"
    ok = last_sync.get("ok")
    bot_status = "OK" if ok is True else "Lỗi" if ok is False else "Chưa sync"
    return (
        f"Bot: {bot_status}\n"
        f"Sync gần nhất: {at}\n"
        f"Trạng thái: {status}"
    )


def has_add_session(chat_id: str) -> bool:
    with add_sessions_lock:
        return chat_id in add_sessions


def has_renew_session(chat_id: str) -> bool:
    with renew_sessions_lock:
        return chat_id in renew_sessions


def has_email_session(chat_id: str) -> bool:
    with email_sessions_lock:
        return chat_id in email_sessions


def has_chatid_session(chat_id: str) -> bool:
    with chatid_sessions_lock:
        return chat_id in chatid_sessions


def start_add_session(config: RuntimeConfig, chat_id: str) -> str:
    with add_sessions_lock:
        add_sessions[chat_id] = AddSession(field_index=0, values={})
    return (
        "Bắt đầu thêm hội viên mới.\n"
        "Bot sẽ hỏi từng trường theo Google Sheet. Gõ /cancel để hủy.\n\n"
        f"{current_add_prompt(config, chat_id)}"
    )


def cancel_add_session(chat_id: str) -> str:
    with add_sessions_lock:
        existed = add_sessions.pop(chat_id, None) is not None
    return "Đã hủy nhập hội viên." if existed else "Không có phiên nhập hội viên nào đang mở."


def start_renew_session(chat_id: str) -> str:
    with renew_sessions_lock:
        renew_sessions[chat_id] = AddSession(field_index=0, values={})
    return (
        "Bắt đầu gia hạn hội viên.\n"
        "Bot sẽ hỏi từng trường và lưu lịch sử gia hạn. Gõ /cancel để hủy.\n\n"
        f"{current_renew_prompt(chat_id)}"
    )


def cancel_renew_session(chat_id: str) -> str:
    with renew_sessions_lock:
        existed = renew_sessions.pop(chat_id, None) is not None
    return "Đã hủy gia hạn hội viên." if existed else "Không có phiên gia hạn nào đang mở."


def start_email_session(chat_id: str) -> str:
    with email_sessions_lock:
        email_sessions.add(chat_id)
    return (
        "Nhập email người nhận cảnh báo:\n"
        "Gửi một địa chỉ email hợp lệ, hoặc /cancel để hủy."
    )


def cancel_email_session(chat_id: str) -> str:
    with email_sessions_lock:
        existed = chat_id in email_sessions
        email_sessions.discard(chat_id)
    return "Đã hủy thêm email." if existed else "Không có phiên thêm email nào đang mở."


def start_chatid_session(chat_id: str) -> str:
    with chatid_sessions_lock:
        chatid_sessions.add(chat_id)
    return (
        "Nhập group chat ID nhận digest:\n"
        "Gửi một chat ID hợp lệ, hoặc /cancel để hủy."
    )


def cancel_chatid_session(chat_id: str) -> str:
    with chatid_sessions_lock:
        existed = chat_id in chatid_sessions
        chatid_sessions.discard(chat_id)
    return "Đã hủy thêm group chat ID." if existed else "Không có phiên thêm group chat ID nào đang mở."


def current_add_prompt(config: RuntimeConfig, chat_id: str) -> str:
    with add_sessions_lock:
        session = add_sessions.get(chat_id)
        if session is None:
            return "Gõ /add để bắt đầu thêm hội viên."
        field = ADD_FIELDS[session.field_index]
    skip_note = "" if field.key in config.telegram_add_required_fields else "\nNếu không nhập, hãy gõ dấu chấm (.) để bỏ qua."
    return f"{field.prompt}{skip_note}"


def current_renew_prompt(chat_id: str) -> str:
    with renew_sessions_lock:
        session = renew_sessions.get(chat_id)
        if session is None:
            return "Gõ /renew để bắt đầu gia hạn hội viên."
        field = RENEW_FIELDS[session.field_index]
    skip_note = "" if field.key in ("member_code", "months") else "\nNếu không nhập, hãy gõ dấu chấm (.) để bỏ qua."
    return f"{field.prompt}{skip_note}"


def add_session_markup(config: RuntimeConfig, chat_id: str) -> dict[str, Any] | None:
    with add_sessions_lock:
        session = add_sessions.get(chat_id)
        if session is None:
            return telegram_menu_markup()
        field = ADD_FIELDS[session.field_index]
    buttons = [[{"text": "/cancel"}]]
    return {"keyboard": buttons, "resize_keyboard": True, "one_time_keyboard": False}


def renew_session_markup(config: RuntimeConfig, chat_id: str) -> dict[str, Any] | None:
    with renew_sessions_lock:
        session = renew_sessions.get(chat_id)
        if session is None:
            return telegram_menu_markup()
    return {"keyboard": [[{"text": "/cancel"}]], "resize_keyboard": True, "one_time_keyboard": False}


def email_session_markup() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": "/cancel"}]],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def chatid_session_markup() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": "/cancel"}]],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def handle_email_session_input(config: RuntimeConfig, chat_id: str, text: str) -> str:
    email = text.strip()
    if not is_valid_email(email):
        return "Email không hợp lệ. Nhập lại hoặc /cancel để hủy."

    updated = add_email_recipient_to_env(email)
    with email_sessions_lock:
        email_sessions.discard(chat_id)
    os.environ["ALERT_EMAIL_TO"] = updated
    return (
        f"Đã thêm email nhận cảnh báo: {email}\n"
        f"ALERT_EMAIL_TO hiện tại: {updated}\n"
        "Lệnh /testalert có thể dùng ngay nếu SMTP đã được cấu hình."
    )


def handle_chatid_session_input(config: RuntimeConfig, chat_id: str, text: str) -> str:
    group_chat_id = text.strip()
    if not is_valid_chat_id(group_chat_id):
        return "Chat ID không hợp lệ. Nhập lại hoặc /cancel để hủy."

    updated = add_chatid_recipient_to_env(group_chat_id)
    with chatid_sessions_lock:
        chatid_sessions.discard(chat_id)
    os.environ["TELEGRAM_DIGEST_CHAT_IDS"] = updated
    return (
        f"Đã thêm group chat ID: {group_chat_id}\n"
        f"TELEGRAM_DIGEST_CHAT_IDS hiện tại: {updated}\n"
        "Bạn có thể dùng /senddigest để test ngay."
    )


def is_valid_email(value: str) -> bool:
    text = value.strip()
    return "@" in text and "." in text and " " not in text


def is_valid_chat_id(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text[0] == "-":
        text = text[1:]
    return text.isdigit()


def add_email_recipient_to_env(email: str) -> str:
    return add_csv_value_to_env("ALERT_EMAIL_TO", email)


def add_chatid_recipient_to_env(chat_id: str) -> str:
    return add_csv_value_to_env("TELEGRAM_DIGEST_CHAT_IDS", chat_id)


def add_csv_value_to_env(key: str, value: str) -> str:
    path = Path(".env")
    existing_value = os.getenv(key, "")
    if path.exists():
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        updated_lines: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}="):
                current_value = stripped.split("=", 1)[1]
                merged_value = merge_csv_values(current_value, value)
                updated_lines.append(f"{key}={merged_value}\n")
                replaced = True
            else:
                updated_lines.append(line)
        if not replaced:
            if updated_lines and not updated_lines[-1].endswith(("\n", "\r")):
                updated_lines[-1] = f"{updated_lines[-1]}\n"
            updated_lines.append(f"{key}={merge_csv_values(existing_value, value)}\n")
        path.write_text("".join(updated_lines), encoding="utf-8")
    else:
        path.write_text(f"{key}={merge_csv_values(existing_value, value)}\n", encoding="utf-8")
    return merge_csv_values(existing_value, value)


def handle_add_session_input(config: RuntimeConfig, chat_id: str, text: str) -> str:
    raw_value = text
    with add_sessions_lock:
        session = add_sessions.get(chat_id)
        if session is None:
            return "Gõ /add để bắt đầu thêm hội viên."
        field = ADD_FIELDS[session.field_index]

    value = "" if is_skip_input(raw_value) else raw_value.strip()
    if field.key in config.telegram_add_required_fields and not value:
        return f"{field.label} là bắt buộc.\n\n{current_add_prompt(config, chat_id)}"
    if field.key == "months" and value and parse_months(value) is None:
        return f"Gói đăng ký phải là số tháng hợp lệ.\n\n{current_add_prompt(config, chat_id)}"

    with add_sessions_lock:
        session = add_sessions.get(chat_id)
        if session is None:
            return "Gõ /add để bắt đầu thêm hội viên."
        session.values[field.key] = value
        session.field_index += 1
        if session.field_index < len(ADD_FIELDS):
            next_field = ADD_FIELDS[session.field_index]
            skip_note = "" if next_field.key in config.telegram_add_required_fields else "\nNếu không nhập, hãy gõ dấu chấm (.) để bỏ qua."
            return f"Đã lưu {field.label}.\n\n{next_field.prompt}{skip_note}"
        values = session.values.copy()
        add_sessions.pop(chat_id, None)

    member_name = values.get("member_name", "")
    months = values.get("months", "")
    state, member_code = add_member_from_telegram(
        config,
        member_name,
        months,
        values.get("phone", ""),
        values.get("facebook", ""),
        values.get("source", ""),
        values.get("amount", ""),
        values.get("transaction_name", ""),
        chat_id,
        values.get("note", ""),
    )
    return (
        f"Đã thêm hội viên: {member_name}\n"
        f"Mã hội viên: {member_code}\n"
        f"Bắt đầu: {format_date(state.start_date)}\n"
        f"Kết thúc: {format_date(state.end_date)}\n"
        f"Tình trạng: {state.status}\n"
        f"Còn lại: {state.remaining}"
    )


def handle_renew_session_input(config: RuntimeConfig, chat_id: str, text: str) -> str:
    raw_value = text
    with renew_sessions_lock:
        session = renew_sessions.get(chat_id)
        if session is None:
            return "Gõ /renew để bắt đầu gia hạn hội viên."
        field = RENEW_FIELDS[session.field_index]

    value = "" if is_skip_input(raw_value) else raw_value.strip()
    if field.key in ("member_code", "months") and not value:
        return f"{field.label} là bắt buộc.\n\n{current_renew_prompt(chat_id)}"
    if field.key == "months" and value and parse_months(value) is None:
        return f"Gói đăng ký phải là số tháng hợp lệ.\n\n{current_renew_prompt(chat_id)}"

    with renew_sessions_lock:
        session = renew_sessions.get(chat_id)
        if session is None:
            return "Gõ /renew để bắt đầu gia hạn hội viên."
        session.values[field.key] = value
        session.field_index += 1
        if session.field_index < len(RENEW_FIELDS):
            next_field = RENEW_FIELDS[session.field_index]
            skip_note = "" if next_field.key in ("member_code", "months") else "\nNếu không nhập, hãy gõ dấu chấm (.) để bỏ qua."
            return f"Đã lưu {field.label}.\n\n{next_field.prompt}{skip_note}"
        values = session.values.copy()
        renew_sessions.pop(chat_id, None)

    state = renew_member_from_telegram(
        config,
        values.get("member_code", ""),
        values.get("months", ""),
        values.get("amount", ""),
        f"telegram:{chat_id}",
        "",
    )
    if state is None:
        return f"Không tìm thấy mã hội viên: {values.get('member_code', '')}"
    return (
        f"Đã gia hạn hội viên mã: {normalize_member_code(values.get('member_code', ''))}\n"
        f"Bắt đầu mới: {format_date(state.start_date)}\n"
        f"Kết thúc mới: {format_date(state.end_date)}\n"
        f"Tình trạng: {state.status}\n"
        f"Còn lại: {state.remaining}\n"
        "Đã lưu lịch sử gia hạn."
    )


def handle_renew_command(config: RuntimeConfig, chat_id: str, payload: str) -> str:
    if not payload.strip():
        return start_renew_session(chat_id)

    parts = parse_pipe_payload(payload)
    if len(parts) < 2:
        return "Sai cú pháp. Dùng: /renew Mã hội viên | Gói đăng ký | Số tiền, hoặc gõ /renew để nhập từng bước."

    member_code = normalize_member_code(parts[0])
    months = parts[1]
    if not member_code or parse_months(months) is None:
        return "Mã hội viên hoặc số tháng không hợp lệ."

    amount = parts[2] if len(parts) > 2 else ""
    state = renew_member_from_telegram(config, member_code, months, amount, f"telegram:{chat_id}", "")
    if state is None:
        return f"Không tìm thấy mã hội viên: {member_code}"
    return (
        f"Đã gia hạn hội viên mã: {member_code}\n"
        f"Bắt đầu mới: {format_date(state.start_date)}\n"
        f"Kết thúc mới: {format_date(state.end_date)}\n"
        f"Tình trạng: {state.status}\n"
        f"Còn lại: {state.remaining}\n"
        "Đã lưu lịch sử gia hạn."
    )


def handle_cancel_member_command(config: RuntimeConfig, chat_id: str, payload: str) -> str:
    parts = parse_pipe_payload(payload)
    member_code = normalize_member_code(parts[0]) if parts else ""
    if not member_code:
        return "Dùng: /huy Mã hội viên. Ví dụ: /huy HV000001"

    is_cancelled = cancel_member_from_telegram(config, member_code, f"telegram:{chat_id}", "")
    if not is_cancelled:
        return f"Không tìm thấy mã hội viên: {member_code}"
    return (
        f"Đã hủy hội viên mã: {member_code}\n"
        "Trạng thái trên sheet: Đã hủy\n"
        "Đã lưu lịch sử hủy."
    )


def parse_pipe_payload(payload: str) -> list[str]:
    return [part.strip() for part in payload.split("|")]


def is_skip_input(value: str) -> bool:
    text = value.strip()
    return text in {".", "-", "/skip", "skip", "bo qua", "bỏ qua"}


def handle_list_command(config: RuntimeConfig, payload: str) -> str:
    snapshots = load_member_snapshots(config)
    limit = parse_optional_limit(payload, default=20)
    return format_member_list("Danh sách hội viên", snapshots, limit)


def handle_expiring_command(config: RuntimeConfig, payload: str) -> str:
    snapshots = load_member_snapshots(config)
    days = parse_optional_limit(payload, default=config.alert_before_days)
    filtered = filter_expiring_members(snapshots, days)
    return format_member_list(f"Sắp hết hạn trong {days} ngày", filtered, 20)


def handle_status_list_command(config: RuntimeConfig, status: str) -> str:
    snapshots = load_member_snapshots(config)
    filtered = [item for item in snapshots if item.status == status]
    filtered.sort(key=lambda item: (item.days_remaining if item.days_remaining is not None else 9999, item.name.casefold()))
    return format_member_list(f"Hội viên {status.lower()}", filtered, 20)


def handle_search_command(config: RuntimeConfig, payload: str) -> str:
    query = payload.strip()
    if not query:
        return "Dùng: /search <từ khóa>"
    snapshots = load_member_snapshots(config)
    needle = query.casefold()
    filtered = [
        item
        for item in snapshots
        if needle in item.name.casefold()
        or needle in item.member_code.casefold()
        or needle in item.phone.casefold()
        or needle in item.package.casefold()
        or needle in item.note.casefold()
    ]
    return format_member_list(f"Kết quả tìm: {query}", filtered, 20)


def handle_stats_command(config: RuntimeConfig) -> str:
    snapshots = load_member_snapshots(config)
    total = len(snapshots)
    active = sum(1 for item in snapshots if item.status == "Còn hạn")
    expiring = sum(1 for item in snapshots if item.status == "Sắp hết hạn")
    expired = sum(1 for item in snapshots if item.status == "Hết hạn")
    cancelled = sum(1 for item in snapshots if item.status == "Đã hủy")
    return (
        "Thống kê nhanh:\n"
        f"- Tổng hội viên: {total}\n"
        f"- Còn hạn: {active}\n"
        f"- Sắp hết hạn: {expiring}\n"
        f"- Hết hạn: {expired}\n"
        f"- Đã hủy: {cancelled}"
    )


def handle_send_digest_command(config: RuntimeConfig) -> str:
    if not config.telegram_bot_token:
        return "Chưa cấu hình TELEGRAM_BOT_TOKEN nên chưa gửi được Telegram digest."
    if not config.telegram_digest_chat_ids:
        return "Chưa cấu hình TELEGRAM_DIGEST_CHAT_IDS. Vào group Telegram, gửi /id để lấy chat ID rồi điền vào .env."

    sent, member_count = send_telegram_expiring_digest(config, config.telegram_digest_chat_ids)
    if not sent:
        return "Không gửi được digest vào group. Kiểm tra TELEGRAM_DIGEST_CHAT_IDS và log bot."
    return (
        "Đã gửi danh sách hội viên sắp hết hạn vào group.\n"
        f"Group đã gửi: {', '.join(sent)}\n"
        f"Số hội viên trong danh sách: {member_count}"
    )


def parse_optional_limit(payload: str, default: int) -> int:
    text = payload.strip()
    if not text:
        return default
    try:
        value = int(float(text))
    except ValueError:
        return default
    return max(1, value)


def format_member_list(title: str, members: list[MemberSnapshot], limit: int) -> str:
    total = len(members)
    if total == 0:
        return f"{title}: không có dữ liệu."

    lines = [f"{title} ({total})", format_telegram_member_table(members, limit)]

    if total > limit:
        lines.append(f"... còn {total - limit} hội viên nữa")
    return "\n".join(lines)


def format_telegram_member_table(members: list[MemberSnapshot], limit: int) -> str:
    rows = [
        f"{'STT':>3} {'Mã':<8} {'Tên':<16} {'Gói':<4} {'Hết hạn':<10} {'Còn lại':<14} {'SĐT':<12}"
        f"\n{'-' * 3} {'-' * 8} {'-' * 16} {'-' * 4} {'-' * 10} {'-' * 14} {'-' * 12}"
    ]
    for index, item in enumerate(members[:limit], start=1):
        rows.append(
            f"{index:>3} "
            f"{fixed_width(item.member_code or '-', 8)} "
            f"{fixed_width(item.name, 16)} "
            f"{fixed_width(item.package or '-', 4)} "
            f"{fixed_width(format_date(item.end_date) or '-', 10)} "
            f"{fixed_width(item.remaining or '-', 14)} "
            f"{fixed_width(item.phone or '-', 12)}"
        )
    return "<pre>" + html.escape("\n".join(rows)) + "</pre>"


def render_history_table_body(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="11">Chưa có lịch sử gia hạn.</td></tr>'
    parts = []
    for row in rows:
        parts.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('Thời gian', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Họ tên', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('SĐT', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Gói cũ', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Ngày hết hạn cũ', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Gói mới', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Ngày vào nhóm mới', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Ngày hết hạn mới', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Số tiền', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Người thao tác', '') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('Ghi chú', '') or '-'))}</td>"
            "</tr>"
        )
    return "".join(parts)


def fixed_width(value: str, width: int) -> str:
    text = " ".join(str(value or "-").split())
    if len(text) > width:
        return text[: max(1, width - 1)] + "…"
    return text.ljust(width)


def send_discord(webhook_url: str, message: str) -> None:
    response = requests.post(webhook_url, json={"content": message}, timeout=30)
    response.raise_for_status()


def telegram_reply(config: RuntimeConfig, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    for index, chunk in enumerate(split_telegram_message(text)):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if "<pre>" in chunk:
            payload["parse_mode"] = "HTML"
        if index == 0 and reply_markup:
            payload["reply_markup"] = reply_markup
        telegram_api(config, "sendMessage", payload)


def split_telegram_message(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(line) <= limit:
            current = line
            continue
        for start in range(0, len(line), limit):
            chunks.append(line[start : start + limit])
        current = ""
    if current:
        chunks.append(current)
    return chunks


def worker_loop(config: RuntimeConfig) -> None:
    while True:
        try:
            result = sync_sheet(config)
            last_sync.update({"ok": True, "at": datetime.now().isoformat(timespec="seconds"), "message": result})
            logger.info("Sync completed: %s", result)
        except Exception as exc:
            last_sync.update({"ok": False, "at": datetime.now().isoformat(timespec="seconds"), "message": str(exc)})
            logger.exception("Sync failed")
        time.sleep(max(1, config.check_interval_minutes) * 60)


def telegram_digest_loop(config: RuntimeConfig) -> None:
    if not config.telegram_bot_token:
        logger.info("Telegram bot token is empty; Telegram digest is disabled.")
        return
    if not config.telegram_digest_chat_ids:
        logger.info("TELEGRAM_DIGEST_CHAT_IDS is empty; Telegram digest is disabled.")
        return
    if config.telegram_digest_run_time is None:
        logger.info("TELEGRAM_DIGEST_RUN_TIME is empty; scheduled Telegram digest is disabled.")
        return

    last_sent_date: date | None = None
    logger.info(
        "Telegram digest scheduled at %s for chat IDs: %s",
        config.telegram_digest_run_time.strftime("%H:%M"),
        ",".join(config.telegram_digest_chat_ids),
    )
    while True:
        try:
            now = datetime.now()
            if now.time() >= config.telegram_digest_run_time and last_sent_date != now.date():
                sent, member_count = send_telegram_expiring_digest(config, config.telegram_digest_chat_ids)
                last_sent_date = now.date()
                logger.info("Telegram digest sent to %s with %s members", sent, member_count)
        except Exception:
            logger.exception("Telegram digest job failed")
        time.sleep(60)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "last_sync": last_sync}


@app.get("/", response_class=HTMLResponse)
def root(page: int = 1) -> HTMLResponse:
    return HTMLResponse(render_member_dashboard(page))


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(page: int = 1) -> HTMLResponse:
    return HTMLResponse(render_member_dashboard(page))


def render_admin_dashboard() -> str:
    ok = last_sync.get("ok")
    synced_at = str(last_sync.get("at") or "Chưa có lần sync nào")
    message = last_sync.get("message")
    status_text = "Đang chờ sync" if ok is None else "Hoạt động" if ok else "Có lỗi"
    status_class = "waiting" if ok is None else "ok" if ok else "error"
    page_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime_hint = "Auto refresh 30s"

    rows = changed_rows = alerted_rows = "-"
    last_error = "-"
    status_source = "Chưa có lần sync nào"
    detail = ""
    if isinstance(message, dict):
        rows = str(message.get("rows", "-"))
        changed_rows = str(message.get("changed_rows", "-"))
        alerted_rows = str(message.get("alerted_rows", "-"))
        detail = html.escape(json.dumps(message, ensure_ascii=False, indent=2))
        status_source = "Sync worker"
    else:
        detail = html.escape(str(message))
        if ok is False:
            last_error = html.escape(str(message))

    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Quản lý bot hội viên</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef2f7;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --text: #0f172a;
      --muted: #64748b;
      --border: #d7dee8;
      --border-strong: #c7d2e2;
      --ok: #166534;
      --ok-bg: #ecfdf3;
      --warn: #92400e;
      --warn-bg: #fffbeb;
      --error: #b91c1c;
      --error-bg: #fef2f2;
      --accent: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      width: min(1240px, calc(100% - 32px));
      margin: 24px auto 28px;
    }}
    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 18px;
      padding: 18px 20px;
      background: linear-gradient(180deg, #fff 0%, #f8fbff 100%);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
      line-height: 1.2;
    }}
    p {{ margin: 0; color: var(--muted); line-height: 1.45; }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 38px;
      padding: 8px 14px;
      border-radius: 10px;
      font-weight: 700;
      white-space: nowrap;
      border: 1px solid transparent;
    }}
    .badge.ok {{ color: var(--ok); background: var(--ok-bg); border-color: #bbf7d0; }}
    .badge.waiting {{ color: var(--warn); background: var(--warn-bg); border-color: #fde68a; }}
    .badge.error {{ color: var(--error); background: var(--error-bg); border-color: #fecaca; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 1px 1px rgba(15, 23, 42, 0.03);
      min-height: 112px;
    }}
    .card.metric {{
      grid-column: span 3;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 10px;
    }}
    .value {{
      font-size: 26px;
      font-weight: 700;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .subvalue {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .split {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(320px, 0.85fr);
      gap: 12px;
      margin-top: 12px;
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      box-shadow: 0 1px 1px rgba(15, 23, 42, 0.03);
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    pre {{
      margin: 0;
      padding: 14px;
      border-radius: 10px;
      background: #0b1220;
      color: #e5eefc;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.45;
      font-size: 13px;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 700;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      min-height: 38px;
      padding: 8px 14px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
    }}
    .list {{
      display: grid;
      gap: 10px;
    }}
    .event {{
      display: grid;
      gap: 6px;
      padding: 12px 14px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      border-radius: 10px;
    }}
    .event-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-weight: 700;
    }}
    .event-body {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}
    @media (max-width: 760px) {{
      main {{ width: min(100% - 20px, 1240px); margin: 12px auto 20px; }}
      header {{ display: block; }}
      .split {{ grid-template-columns: 1fr; }}
      .badge {{ margin-top: 12px; }}
      .card.metric {{ grid-column: span 6; }}
    }}
    @media (max-width: 460px) {{
      .card.metric {{ grid-column: span 12; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Bot quản lý hội viên</h1>
        <p>Dashboard vận hành cho admin, tự làm mới mỗi 30 giây.</p>
        <div class="meta">
          <span class="pill">Render: {html.escape(page_updated_at)}</span>
          <span class="pill">Nguồn: {html.escape(status_source)}</span>
          <span class="pill">{html.escape(uptime_hint)}</span>
        </div>
      </div>
      <span class="badge {status_class}">{html.escape(status_text)}</span>
    </header>

    <section class="grid" aria-label="Thống kê sync">
      <div class="card metric">
        <div class="label">Lần sync gần nhất</div>
        <div class="value">{html.escape(synced_at)}</div>
        <div class="subvalue">Thời điểm worker cập nhật Google Sheet gần nhất.</div>
      </div>
      <div class="card metric">
        <div class="label">Số dòng đọc</div>
        <div class="value">{html.escape(rows)}</div>
        <div class="subvalue">Số dòng dữ liệu phát hiện trong worksheet.</div>
      </div>
      <div class="card metric">
        <div class="label">Dòng đã cập nhật</div>
        <div class="value">{html.escape(changed_rows)}</div>
        <div class="subvalue">Các dòng có thay đổi sau lần sync gần nhất.</div>
      </div>
      <div class="card metric">
        <div class="label">Cảnh báo đã gửi</div>
        <div class="value">{html.escape(alerted_rows)}</div>
        <div class="subvalue">Số hội viên vừa được nhắc hạn trong lần sync gần nhất.</div>
      </div>
    </section>

    <section class="split">
      <div class="panel">
        <h2>Chi tiết lần sync</h2>
        <pre>{detail}</pre>
      </div>
      <div class="panel">
        <h2>Điều khiển nhanh</h2>
        <div class="list">
          <div class="event">
            <div class="event-title">Trạng thái hiện tại</div>
            <div class="event-body">{html.escape(status_text)}</div>
          </div>
          <div class="event">
            <div class="event-title">Lỗi gần nhất</div>
            <div class="event-body">{last_error}</div>
          </div>
          <div class="event">
            <div class="event-title">Endpoints</div>
            <div class="event-body">
              <a href="/health">/health</a> JSON kỹ thuật<br>
              <a href="/">/</a> dashboard admin<br>
              <a href="/admin">/admin</a> dashboard admin
            </div>
          </div>
        </div>
        <div class="actions">
          <a class="button" href="/">Làm mới</a>
          <a class="button" href="/health">Xem JSON</a>
        </div>
      </div>
    </section>
  </main>
</body>
</html>"""


def render_member_dashboard(page: int = 1) -> str:
    config = load_config()
    load_error = ""
    try:
        members = sort_member_snapshots(load_member_snapshots(config))
    except Exception as exc:
        logger.exception("Failed to load member dashboard")
        members = []
        load_error = str(exc)
    history_rows: list[dict[str, Any]] = []
    history_error = ""
    try:
        history_rows = load_renew_history(config)
    except Exception as exc:
        logger.exception("Failed to load renew history for dashboard")
        history_error = str(exc)
    page_members, total_pages = paginate_members(members, page, 100)
    current_page = min(max(1, page), total_pages)
    total_members = len(members)
    active_members = sum(1 for member in members if member.status == "Còn hạn")
    expiring_members = sum(1 for member in members if member.status == "Sắp hết hạn")
    expired_members = sum(1 for member in members if member.status == "Hết hạn")
    cancelled_members = sum(1 for member in members if member.status == "Đã hủy")

    ok = last_sync.get("ok")
    synced_at = str(last_sync.get("at") or "Chưa có lần sync nào")
    status_text = "Đang chờ sync" if ok is None else "Hoạt động" if ok else "Có lỗi"
    status_class = "waiting" if ok is None else "ok" if ok else "error"
    uptime_hint = "Auto refresh 30s"
    message = last_sync.get("message")
    rows = changed_rows = alerted_rows = "-"
    last_error = "-"
    if isinstance(message, dict):
        rows = str(message.get("rows", "-"))
        changed_rows = str(message.get("changed_rows", "-"))
        alerted_rows = str(message.get("alerted_rows", "-"))
    elif ok is False:
        last_error = html.escape(str(message))
    if load_error:
        last_error = html.escape(load_error)

    def status_badge(text: str) -> str:
        slug = text.lower().replace(" ", "-")
        return f'<span class="status {slug}">{html.escape(text)}</span>'

    page_rows = []
    start_number = (current_page - 1) * 100 + 1
    for index, member in enumerate(page_members, start=start_number):
        page_rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{member.row_index}</td>"
            f"<td>{html.escape(member.member_code or '-')}</td>"
            f"<td>{html.escape(member.name)}</td>"
            f"<td>{html.escape(member.package or '-')}</td>"
            f"<td>{html.escape(format_date(member.start_date) or '-')}</td>"
            f"<td>{html.escape(format_date(member.end_date) or '-')}</td>"
            f"<td>{html.escape(member.remaining or '-')}</td>"
            f"<td>{status_badge(member.status or '-')}</td>"
            f"<td>{html.escape(member.phone or '-')}</td>"
            f"<td>{html.escape(member.telegram_id or '-')}</td>"
            f"<td>{html.escape(member.note or '-')}</td>"
            "</tr>"
        )

    table_body = "".join(page_rows) if page_rows else '<tr><td colspan="12">Không có hội viên nào.</td></tr>'
    prev_page = max(1, current_page - 1)
    next_page = min(total_pages, current_page + 1)
    page_label = f"Trang {current_page}/{total_pages}"
    page_slice_label = f"Hiển thị {len(page_members)} / {total_members} hội viên"
    page_link = lambda p: f"/?page={p}"
    history_rows = sorted(
        history_rows,
        key=lambda row: str(row.get("Thời gian", "") or ""),
        reverse=True,
    )[:20]
    history_table_body = render_history_table_body(history_rows)

    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Quản lý hội viên</title>
  <style>
    :root {{
      --bg: #eef2f7;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --text: #0f172a;
      --muted: #64748b;
      --border: #d7dee8;
      --ok: #166534;
      --ok-bg: #ecfdf3;
      --warn: #92400e;
      --warn-bg: #fffbeb;
      --error: #b91c1c;
      --error-bg: #fef2f2;
      --accent: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Segoe UI, Arial, sans-serif; background: var(--bg); color: var(--text); }}
    main {{ width: min(1400px, calc(100% - 24px)); margin: 16px auto 24px; }}
    .header {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; margin-bottom: 12px; }}
    .header-top {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    .muted {{ color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .meta {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
    .pill, .badge, .button, .pager a, .pager span {{ border-radius: 8px; }}
    .pill {{ padding: 5px 10px; border: 1px solid var(--border); background: #fff; color: var(--muted); font-size: 12px; font-weight: 700; }}
    .badge {{ padding: 8px 12px; font-weight: 700; border: 1px solid transparent; }}
    .badge.ok {{ color: var(--ok); background: var(--ok-bg); border-color: #bbf7d0; }}
    .badge.waiting {{ color: var(--warn); background: var(--warn-bg); border-color: #fde68a; }}
    .badge.error {{ color: var(--error); background: var(--error-bg); border-color: #fecaca; }}
    .grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; }}
    .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; min-height: 108px; }}
    .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin-bottom: 10px; }}
    .value {{ font-size: 24px; font-weight: 700; line-height: 1.2; }}
    .subvalue {{ margin-top: 8px; color: var(--muted); font-size: 13px; }}
    .table-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
    .table-head, .table-foot {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 14px 16px; }}
    .table-head {{ border-bottom: 1px solid var(--border); }}
    .table-foot {{ border-top: 1px solid var(--border); background: #fbfdff; }}
    .pager {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .pager a, .pager span {{ display: inline-flex; align-items: center; min-height: 32px; padding: 6px 10px; border: 1px solid var(--border); background: #fff; color: var(--text); text-decoration: none; font-weight: 700; }}
    .pager .current {{ background: #eaf1ff; color: var(--accent); border-color: #bfd0ff; }}
    .scroll {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1280px; }}
    thead th {{ position: sticky; top: 0; background: #f8fafc; z-index: 1; padding: 12px 14px; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: #334155; text-align: left; border-bottom: 1px solid var(--border); }}
    tbody td {{ padding: 12px 14px; border-bottom: 1px solid #edf2f7; vertical-align: top; font-size: 13px; line-height: 1.4; overflow-wrap: anywhere; }}
    tbody tr:nth-child(even) {{ background: #fbfdff; }}
    .status {{ display: inline-flex; padding: 4px 8px; font-size: 12px; font-weight: 700; border-radius: 999px; white-space: nowrap; }}
    .status.còn-hạn {{ background: var(--ok-bg); color: var(--ok); }}
    .status.sắp-hết-hạn {{ background: var(--warn-bg); color: var(--warn); }}
    .status.hết-hạn {{ background: var(--error-bg); color: var(--error); }}
    .status.đã-hủy {{ background: #e2e8f0; color: #475569; }}
    .status.unknown {{ background: #eef2ff; color: #4338ca; }}
    .side {{ display: grid; gap: 12px; margin-top: 12px; grid-template-columns: minmax(0, 1.3fr) minmax(300px, .7fr); }}
    .panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
    pre {{ margin: 0; padding: 12px; background: #0b1220; color: #e5eefc; border-radius: 10px; overflow: auto; white-space: pre-wrap; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }}
    .button {{ display: inline-flex; align-items: center; min-height: 34px; padding: 6px 10px; border: 1px solid var(--border); background: #fff; color: var(--accent); text-decoration: none; font-weight: 700; }}
    @media (max-width: 1100px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .side {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 640px) {{ .grid {{ grid-template-columns: 1fr; }} .header-top {{ flex-direction: column; }} }}
  </style>
</head>
<body>
  <main>
    <section class="header">
      <div class="header-top">
        <div>
          <h1>Quản lý hội viên</h1>
          <div class="muted">Dashboard sort theo ngày hết hạn tăng dần, mỗi trang 100 hội viên.</div>
          <div class="meta">
            <span class="pill">Render {html.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</span>
            <span class="pill">Sync {html.escape(synced_at)}</span>
            <span class="pill">{html.escape(uptime_hint)}</span>
          </div>
        </div>
        <span class="badge {status_class}">{html.escape(status_text)}</span>
      </div>
    </section>

    <section class="grid">
      <div class="card"><div class="label">Tổng hội viên</div><div class="value">{total_members}</div><div class="subvalue">Toàn bộ dữ liệu trong sheet.</div></div>
      <div class="card"><div class="label">Còn hạn</div><div class="value">{active_members}</div><div class="subvalue">Đang ở trạng thái an toàn.</div></div>
      <div class="card"><div class="label">Sắp hết hạn</div><div class="value">{expiring_members}</div><div class="subvalue">Theo ngưỡng hiện tại.</div></div>
      <div class="card"><div class="label">Hết hạn</div><div class="value">{expired_members}</div><div class="subvalue">Cần gia hạn hoặc xử lý.</div></div>
      <div class="card"><div class="label">Đã hủy</div><div class="value">{cancelled_members}</div><div class="subvalue">Không còn tính hạn.</div></div>
    </section>

    <section class="table-card">
      <div class="table-head">
        <div>
          <div style="font-size:16px;font-weight:700;">Danh sách hội viên</div>
          <div class="muted">Mỗi trang 100 dòng, sắp theo hạn gần nhất lên trước.</div>
        </div>
        <div class="muted">{html.escape(page_label)} · {html.escape(page_slice_label)}</div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>#</th><th>Dòng</th><th>Mã HV</th><th>Họ tên</th><th>Gói</th><th>Ngày vào</th><th>Ngày hết hạn</th><th>Còn lại</th><th>Trạng thái</th><th>SĐT</th><th>Telegram ID</th><th>Ghi chú</th>
            </tr>
          </thead>
          <tbody>{table_body}</tbody>
        </table>
      </div>
      <div class="table-foot">
        <div class="muted">Trang {current_page}/{total_pages}</div>
        <div class="pager">
          <a href="{page_link(1)}">Đầu</a>
          <a href="{page_link(prev_page)}">Trước</a>
          <span class="current">{html.escape(page_label)}</span>
          <a href="{page_link(next_page)}">Sau</a>
          <a href="{page_link(total_pages)}">Cuối</a>
        </div>
      </div>
    </section>

    <section class="side">
      <div class="panel">
        <div style="font-size:16px;font-weight:700;margin-bottom:10px;">Trạng thái sync</div>
        <pre>{html.escape(json.dumps(message, ensure_ascii=False, indent=2)) if not load_error else html.escape(load_error)}</pre>
      </div>
      <div class="panel">
        <div style="font-size:16px;font-weight:700;margin-bottom:10px;">Điều khiển nhanh</div>
        <div class="muted">Lỗi gần nhất: {last_error}</div>
        <div class="actions">
          <a class="button" href="/?page={current_page}">Làm mới</a>
          <a class="button" href="/health">JSON</a>
          <a class="button" href="/admin?page={current_page}">Admin</a>
        </div>
      </div>
    </section>

    <section class="table-card" style="margin-top:12px;">
      <div class="table-head">
        <div>
          <div style="font-size:16px;font-weight:700;">Lịch sử gia hạn</div>
          <div class="muted">20 lần gần nhất, có thể dùng worksheet riêng để đối soát.</div>
        </div>
        <div class="muted">{html.escape(history_error or "Sắp xếp theo thời gian giảm dần")}</div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th>Thời gian</th><th>Họ tên</th><th>SĐT</th><th>Gói cũ</th><th>Ngày hết hạn cũ</th><th>Gói mới</th><th>Ngày vào mới</th><th>Ngày hết hạn mới</th><th>Số tiền</th><th>Người thao tác</th><th>Ghi chú</th>
            </tr>
          </thead>
          <tbody>{history_table_body}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""


def main() -> None:
    config = load_config()
    worker = threading.Thread(target=worker_loop, args=(config,), daemon=True)
    worker.start()
    telegram_worker = threading.Thread(target=telegram_loop, args=(config,), daemon=True)
    telegram_worker.start()
    telegram_digest_worker = threading.Thread(target=telegram_digest_loop, args=(config,), daemon=True)
    telegram_digest_worker.start()
    uvicorn.run(app, host="127.0.0.1", port=config.port)


if __name__ == "__main__":
    main()
