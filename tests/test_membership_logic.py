from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory

from bot import (
    MemberSnapshot,
    RuntimeConfig,
    add_email_recipient_to_env,
    add_chatid_recipient_to_env,
    add_months,
    build_admin_expiring_email_body,
    build_admin_expiring_subject,
    build_alert_message,
    build_history_note,
    build_telegram_expiring_digest,
    calculate_state,
    classify_package_change,
    extract_note_value,
    extract_telegram_chat_id,
    filter_expiring_members,
    format_telegram_member_table,
    format_member_list,
    render_history_table_body,
    is_alert_time,
    is_valid_email,
    is_valid_chat_id,
    is_skip_input,
    merge_csv_values,
    parse_alert_run_time,
    parse_pipe_payload,
    parse_required_add_fields,
    paginate_members,
    sort_member_snapshots,
    should_alert,
    split_telegram_message,
    start_add_session,
    telegram_help_text,
    telegram_start_text,
)


def test_add_months_clamps_to_last_day_of_month():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_start_date_is_filled_when_member_name_exists():
    state = calculate_state("Nguyen Van A", "", "", date(2026, 5, 29), 3)
    assert state.start_date == date(2026, 5, 29)
    assert state.end_date is None


def test_end_date_is_calculated_from_start_date_and_months():
    state = calculate_state("Nguyen Van A", "2026-05-29", "2", date(2026, 5, 29), 3)
    assert state.end_date == date(2026, 7, 29)
    assert state.status == "Còn hạn"


def test_status_is_warning_when_remaining_days_are_under_threshold():
    state = calculate_state("Nguyen Van A", "2026-04-01", "2", date(2026, 5, 29), 3)
    assert state.status == "Sắp hết hạn"
    assert state.remaining == "3 ngày"


def test_status_is_expired_after_end_date():
    state = calculate_state("Nguyen Van A", "2026-04-01", "1", date(2026, 5, 29), 3)
    assert state.status == "Hết hạn"
    assert state.remaining == "Đã hết hạn 28 ngày"


def test_remaining_is_months_when_more_than_one_month_left():
    state = calculate_state("Nguyen Van A", "2026-05-29", "3", date(2026, 5, 29), 3)
    assert state.remaining == "3 tháng"


def test_parse_pipe_payload_trims_fields():
    assert parse_pipe_payload("Nguyen Van A | 3 | a@example.com") == [
        "Nguyen Van A",
        "3",
        "a@example.com",
    ]


def test_parse_alert_run_time_supports_empty_or_hh_mm():
    assert parse_alert_run_time("") is None
    assert parse_alert_run_time("09:30") == time(9, 30)


def test_is_alert_time_respects_configured_time():
    config = RuntimeConfig(
        google_service_account_json="credentials.json",
        alert_run_time=time(9, 0),
    )
    assert not is_alert_time(config, time(8, 59))
    assert is_alert_time(config, time(9, 0))
    assert is_alert_time(config, time(10, 0))


def test_should_alert_only_once_for_same_end_date():
    config = RuntimeConfig(google_service_account_json="credentials.json")
    state = calculate_state("Nguyen Van A", "2026-04-01", "2", date(2026, 5, 29), 3)
    assert should_alert(config, state, "")
    assert not should_alert(config, state, "2026-05-29 telegram END=2026-06-01")


def test_extract_note_value_reads_channel_tokens():
    note = "khach vip telegram:123456 email:a@example.com discord:https://example.com/hook"
    assert extract_note_value(note, "telegram") == "123456"
    assert extract_note_value(note, "email") == "a@example.com"
    assert extract_note_value(note, "discord") == "https://example.com/hook"


def test_extract_telegram_chat_id_prefers_dedicated_column():
    row = {"Telegram ID": "999", "Ghi chú": "telegram:123"}
    assert extract_telegram_chat_id(row) == "999"
    assert extract_telegram_chat_id({"Telegram ID": "", "Ghi chú": "telegram:123"}) == "123"


def test_build_alert_message_changes_for_expired_members():
    state = calculate_state("Nguyen Van A", "2026-04-01", "1", date(2026, 5, 29), 3)
    message = build_alert_message("Nguyen Van A", state)
    assert "đã hết hạn" in message


def test_admin_expiring_email_body_lists_members():
    member = MemberSnapshot(2, "Nguyen Van A", "3", "0909", "", "", "", date(2026, 5, 29), date(2026, 6, 1), "Sắp hết hạn", "3 ngày", 3, "")
    config = RuntimeConfig(google_service_account_json="credentials.json", google_sheet_url="https://docs.google.com/spreadsheets/d/test/edit")
    subject = build_admin_expiring_subject([member], 3)
    body = build_admin_expiring_email_body(config, [member], 3)
    assert subject == "Bao cao hoi vien sap het han - 1 nguoi trong 3 ngay"
    assert "Nguyen Van A" in body
    assert "2026-06-01" in body
    assert "https://docs.google.com/spreadsheets/d/test/edit" in body


def test_filter_expiring_members_only_keeps_members_in_range():
    members = [
        MemberSnapshot(2, "A", "1", "", "", "", "", None, date(2026, 6, 1), "Sắp hết hạn", "3 ngày", 3, ""),
        MemberSnapshot(3, "B", "1", "", "", "", "", None, date(2026, 6, 10), "Còn hạn", "12 ngày", 12, ""),
        MemberSnapshot(4, "C", "1", "", "", "", "", None, date(2026, 5, 1), "Hết hạn", "Đã hết hạn 28 ngày", -28, ""),
    ]
    filtered = filter_expiring_members(members, 3)
    assert [member.name for member in filtered] == ["A"]


def test_sort_member_snapshots_orders_by_end_date():
    members = [
        MemberSnapshot(2, "B", "1", "", "", "", "", None, date(2026, 6, 10), "Còn hạn", "12 ngày", 12, ""),
        MemberSnapshot(3, "A", "1", "", "", "", "", None, date(2026, 6, 1), "Sắp hết hạn", "3 ngày", 3, ""),
        MemberSnapshot(4, "C", "1", "", "", "", "", None, None, "", "", None, ""),
    ]
    ordered = sort_member_snapshots(members)
    assert [member.name for member in ordered] == ["A", "B", "C"]


def test_paginate_members_returns_100_rows_per_page():
    members = [
        MemberSnapshot(i, f"M{i}", "1", "", "", "", "", None, date(2026, 6, 1), "Còn hạn", "3 ngày", 3, "")
        for i in range(1, 205)
    ]
    page_1, total_pages = paginate_members(members, 1, 100)
    page_3, _ = paginate_members(members, 3, 100)
    assert total_pages == 3
    assert len(page_1) == 100
    assert len(page_3) == 4


def test_telegram_expiring_digest_contains_deadlines():
    members = [
        MemberSnapshot(2, "Nguyen Van A", "1", "0909", "", "", "", None, date(2026, 6, 1), "Sắp hết hạn", "3 ngày", 3, ""),
    ]
    text = build_telegram_expiring_digest(members, 3)
    assert "Danh sách hội viên sắp hết hạn trong 3 ngày" in text
    assert "Nguyen Van A" in text
    assert "2026-06-01" in text


def test_telegram_start_text_mentions_basic_commands():
    text = telegram_start_text("123")
    assert "/add" in text
    assert "/renew" in text
    assert "123" in text


def test_telegram_help_text_shows_minimal_add_format():
    text = telegram_help_text("123")
    assert "/add - bot sẽ hỏi từng trường" in text
    assert "TELEGRAM_ADD_REQUIRED_FIELDS" in text


def test_parse_required_add_fields_defaults_to_name_and_months():
    assert parse_required_add_fields("") == ("member_name", "months")
    assert parse_required_add_fields("member_name,bad_field") == ("member_name",)


def test_merge_csv_values_deduplicates_and_appends():
    assert merge_csv_values("a@example.com,b@example.com", "b@example.com") == "a@example.com,b@example.com"
    assert merge_csv_values("", "a@example.com") == "a@example.com"


def test_is_valid_email_checks_basic_shape():
    assert is_valid_email("a@example.com")
    assert not is_valid_email("bad-email")


def test_is_valid_chat_id_accepts_group_ids():
    assert is_valid_chat_id("-1001234567890")
    assert is_valid_chat_id("123456789")
    assert not is_valid_chat_id("abc")


def test_is_skip_input_accepts_dot_only_for_empty_optional_fields():
    assert is_skip_input(".")
    assert not is_skip_input(" ")
    assert not is_skip_input("abc")


def test_start_add_session_asks_for_member_name():
    config = RuntimeConfig(google_service_account_json="credentials.json")
    text = start_add_session(config, "chat-test")
    assert "Nhập họ tên hội viên" in text


def test_add_email_recipient_to_env_updates_file(monkeypatch):
    with TemporaryDirectory(dir="C:\\tmp") as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("ALERT_EMAIL_TO=a@example.com\n", encoding="utf-8")
        monkeypatch.chdir(temp_dir)
        monkeypatch.setenv("ALERT_EMAIL_TO", "a@example.com")
        updated = add_email_recipient_to_env("b@example.com")
        assert updated == "a@example.com,b@example.com"
        assert env_file.read_text(encoding="utf-8") == "ALERT_EMAIL_TO=a@example.com,b@example.com\n"


def test_add_chatid_recipient_to_env_updates_file(monkeypatch):
    with TemporaryDirectory(dir="C:\\tmp") as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("TELEGRAM_DIGEST_CHAT_IDS=-1001\n", encoding="utf-8")
        monkeypatch.chdir(temp_dir)
        monkeypatch.setenv("TELEGRAM_DIGEST_CHAT_IDS", "-1001")
        updated = add_chatid_recipient_to_env("-1002")
        assert updated == "-1001,-1002"
        assert env_file.read_text(encoding="utf-8") == "TELEGRAM_DIGEST_CHAT_IDS=-1001,-1002\n"


def test_format_member_list_limits_rows_and_shows_total():
    members = [
        MemberSnapshot(2, "Nguyen Van A", "3", "0909", "", "", "", date(2026, 5, 29), date(2026, 8, 29), "Còn hạn", "3 tháng", 92, ""),
        MemberSnapshot(3, "Nguyen Van B", "1", "", "", "", "", date(2026, 5, 29), date(2026, 6, 29), "Còn hạn", "1 tháng", 31, ""),
    ]
    text = format_member_list("Danh sách hội viên", members, limit=1)
    assert "Danh sách hội viên (2)" in text
    assert "Nguyen Van A" in text
    assert "Nguyen Van B" not in text
    assert "còn 1 hội viên nữa" in text


def test_format_telegram_member_table_uses_mobile_cards():
    members = [
        MemberSnapshot(2, "Nguyen Van A", "3", "0909", "", "", "", date(2026, 5, 29), date(2026, 8, 29), "Còn hạn", "3 tháng", 92, ""),
    ]
    text = format_telegram_member_table(members, limit=1)
    assert text.startswith("<b>1. Nguyen Van A</b>")
    assert "Hạn: 2026-08-29" in text
    assert "Nguyen Van A" in text


def test_classify_package_change_tracks_upgrade_downgrade_and_renewal():
    assert classify_package_change("3", "6") == "nâng cấp"
    assert classify_package_change("6", "3") == "hạ cấp"
    assert classify_package_change("6", "6") == "gia hạn"
    assert build_history_note("hủy", "nghỉ tập") == "Hành động: hủy; nghỉ tập"


def test_render_history_table_body_renders_rows():
    html = render_history_table_body(
        [
            {
                "Thời gian": "2026-05-29T10:00:00",
                "Họ tên": "Nguyen Van A",
                "SĐT": "0909",
                "Gói cũ": "3",
                "Ngày hết hạn cũ": "2026-06-01",
                "Gói mới": "6",
                "Ngày vào nhóm mới": "2026-05-29",
                "Ngày hết hạn mới": "2026-11-29",
                "Số tiền": "300000",
                "Người thao tác": "telegram:1",
                "Ghi chú": "gia hạn",
            }
        ]
    )
    assert "Nguyen Van A" in html
    assert "2026-11-29" in html


def test_split_telegram_message_preserves_short_text():
    assert split_telegram_message("abc", limit=10) == ["abc"]
