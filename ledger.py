"""Subscription storage and reminder calculations."""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHARED = "__shared__"
CYCLES = {0, 1, 3, 6, 12}
SETTINGS = {"storage_scope": "separate", "reminder_owner_key": "", "timezone": "Asia/Shanghai",
            "currency_mode": "separate", "base_currency": "CNY", "selected_owner_key": "", "theme": "dark"}
FIELDS = ("name", "amount", "currency", "renewal_date", "cycle_months", "reminder_days", "notes",
          "renewal_mode", "status", "icon_url", "service_domain", "category", "payment_method",
          "payment_other", "reminder_enabled", "reminder_mode", "is_trial", "trial_end_date")


def make_owner_key(platform: str, bot_id: str, sender_id: str) -> str:
    if not platform or not bot_id or not sender_id:
        raise ValueError("无法确定当前用户身份")
    return json.dumps([platform, bot_id, sender_id], ensure_ascii=False, separators=(",", ":"))


def add_months(anchor: date, months: int) -> date:
    index = anchor.year * 12 + anchor.month - 1 + months
    year, zero_month = divmod(index, 12)
    return date(year, zero_month + 1, min(anchor.day, calendar.monthrange(year, zero_month + 1)[1]))


def next_due(anchor: date, cycle_months: int, today: date) -> date | None:
    if cycle_months == 0:
        return anchor if anchor >= today else None
    elapsed = max(0, (today.year - anchor.year) * 12 + today.month - anchor.month)
    offset = (elapsed // cycle_months) * cycle_months
    candidate = add_months(anchor, offset)
    while candidate < today:
        offset += cycle_months
        candidate = add_months(anchor, offset)
    return candidate


def as_bool(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1, "0", "1"):
        return int(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "on", "off"):
        return int(value.lower() in ("true", "on"))
    raise ValueError("开关值只能是真或假")


def domain_of(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = parsed.hostname or ""
    except ValueError:
        raise ValueError("服务网站须为公开域名，例如 openai.com") from None
    if (len(raw) > 255 or not host or parsed.username or parsed.password or
            host == "localhost" or "." not in host or
            not all(c.isascii() and (c.isalnum() or c in ".-") for c in host)):
        raise ValueError("服务网站须为公开域名，例如 openai.com")
    return host.removeprefix("www.")


def clean_fields(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("数据格式错误")
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("订阅名称须为 1–80 个字符")
    try:
        amount = Decimal(str(payload.get("amount", "0")))
    except (InvalidOperation, ValueError):
        raise ValueError("金额格式错误") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99") or amount.as_tuple().exponent < -2:
        raise ValueError("金额须为非负数，最多两位小数")
    currency = str(payload.get("currency") or "CNY").strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ValueError("币种须为三个英文字母")
    try:
        anchor = date.fromisoformat(str(payload.get("renewal_date") or ""))
    except ValueError:
        raise ValueError("续费日期须为 YYYY-MM-DD") from None
    try:
        cycle, lead = int(payload.get("cycle_months", 1)), int(payload.get("reminder_days", 3))
    except (TypeError, ValueError):
        raise ValueError("周期或提前天数格式错误") from None
    if cycle not in CYCLES or not 0 <= lead <= 30:
        raise ValueError("周期仅支持单次、月、季度、半年、年；提前天数为 0–30")
    notes, category = str(payload.get("notes") or "").strip(), str(payload.get("category") or "").strip()
    if len(notes) > 500 or len(category) > 32:
        raise ValueError("备注最多 500 字，标签最多 32 字")
    renewal_mode, status = str(payload.get("renewal_mode") or "manual"), str(payload.get("status") or "active")
    if renewal_mode not in ("manual", "auto") or status not in ("active", "paused"):
        raise ValueError("续费方式或状态无效")
    icon_url = str(payload.get("icon_url") or "").strip()
    if icon_url:
        try:
            parsed = urlsplit(icon_url)
            host = parsed.hostname
        except ValueError:
            raise ValueError("图标链接须为有效 HTTP(S) 地址") from None
        if len(icon_url) > 2048 or parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            raise ValueError("图标链接须为不含账号密码的 HTTP(S) 地址")
    method, other = str(payload.get("payment_method") or "unknown"), str(payload.get("payment_other") or "").strip()
    if method not in ("unknown", "alipay", "wechat", "bank_card", "credit_card", "paypal", "apple_pay", "google_pay", "other"):
        raise ValueError("付款方式无效")
    if len(other) > 80 or (method == "other" and not other):
        raise ValueError("其它付款方式须填写名称，最多 80 字")
    reminder_enabled, is_trial = as_bool(payload.get("reminder_enabled", 1)), as_bool(payload.get("is_trial", 0))
    reminder_mode = str(payload.get("reminder_mode") or "before")
    if reminder_mode not in ("before", "day", "both"):
        raise ValueError("提醒方式无效")
    trial_end = str(payload.get("trial_end_date") or "").strip() if is_trial else ""
    if is_trial:
        try:
            end = date.fromisoformat(trial_end)
        except ValueError:
            raise ValueError("试用结束日期须为 YYYY-MM-DD") from None
        if end > anchor:
            raise ValueError("首次扣款日期不能早于试用结束日期")
    return {"name": name, "amount": str(amount), "currency": currency, "renewal_date": anchor.isoformat(),
            "cycle_months": cycle, "reminder_days": lead, "notes": notes, "renewal_mode": renewal_mode,
            "status": status, "icon_url": icon_url, "service_domain": domain_of(payload.get("service_domain")),
            "category": category, "payment_method": method, "payment_other": other if method == "other" else "",
            "reminder_enabled": reminder_enabled, "reminder_mode": reminder_mode,
            "is_trial": is_trial, "trial_end_date": trial_end}


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.path.parent.chmod(0o700)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owners (owner_key TEXT PRIMARY KEY,label TEXT NOT NULL,
                    private_umo TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL REFERENCES owners(owner_key),name TEXT NOT NULL,
                    amount TEXT NOT NULL,currency TEXT NOT NULL,renewal_date TEXT NOT NULL,
                    cycle_months INTEGER NOT NULL,reminder_days INTEGER NOT NULL,notes TEXT NOT NULL,
                    last_reminded_for TEXT,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_subscriptions_owner ON subscriptions(owner_key);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(subscriptions)")}
            for name, definition in (
                ("renewal_mode", "TEXT NOT NULL DEFAULT 'manual'"), ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("icon_url", "TEXT NOT NULL DEFAULT ''"), ("service_domain", "TEXT NOT NULL DEFAULT ''"),
                ("category", "TEXT NOT NULL DEFAULT ''"), ("payment_method", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("payment_other", "TEXT NOT NULL DEFAULT ''"), ("reminder_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("reminder_mode", "TEXT NOT NULL DEFAULT 'before'"), ("is_trial", "INTEGER NOT NULL DEFAULT 0"),
                ("trial_end_date", "TEXT NOT NULL DEFAULT ''"), ("last_reminder_key", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}")
            for key, value in SETTINGS.items():
                db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))
            existing = db.execute("SELECT owner_key FROM owners ORDER BY updated_at DESC LIMIT 1").fetchone()
            if existing:
                for key in ("reminder_owner_key", "selected_owner_key"):
                    db.execute("UPDATE settings SET value=? WHERE key=? AND value=''", (existing["owner_key"], key))
        if os.name == "posix":
            self.path.chmod(0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self) -> dict:
        with self._db() as db:
            values = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}
        return {key: values.get(key, default) for key, default in SETTINGS.items()}

    def native_config_synced(self) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM settings WHERE key='native_config_synced'").fetchone() is not None

    def mark_native_config_synced(self) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('native_config_synced','1')")

    def save_settings(self, payload: dict) -> dict:
        allowed = {"storage_scope": ("separate", "unified"),
                   "timezone": ("server", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Singapore", "UTC", "America/New_York", "Europe/London"),
                   "currency_mode": ("separate", "convert"),
                   "base_currency": ("CNY", "USD", "HKD", "SGD", "EUR", "JPY", "GBP"),
                   "theme": ("dark", "light")}
        updates = {}
        for key, choices in allowed.items():
            if key in payload:
                value = str(payload[key])
                if value not in choices:
                    raise ValueError(f"{key} 设置无效")
                updates[key] = value
        for key in ("reminder_owner_key", "selected_owner_key"):
            if key in payload:
                value = str(payload[key])
                if value and not any(row["owner_key"] == value for row in self.owners()):
                    raise ValueError("账户不存在")
                updates[key] = value
        future = {**self.settings(), **updates}
        if future["storage_scope"] == "unified" and self.owners() and not future["reminder_owner_key"]:
            raise ValueError("统一模式须指定接收提醒的私聊账户")
        with self._db() as db:
            for key, value in updates.items():
                db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        return self.settings()

    def today(self) -> date:
        tz = self.settings()["timezone"]
        if tz == "server":
            return datetime.now().date()
        try:
            return datetime.now(ZoneInfo(tz)).date()
        except ZoneInfoNotFoundError:
            return datetime.now().date()

    def touch_owner(self, owner_key: str, label: str, private_umo: str) -> None:
        with self._db() as db:
            db.execute("""INSERT INTO owners(owner_key,label,private_umo,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(owner_key) DO UPDATE SET label=excluded.label,
                private_umo=excluded.private_umo,updated_at=excluded.updated_at""",
                (owner_key, label[:80], private_umo, datetime.now().isoformat()))
            db.execute("UPDATE settings SET value=? WHERE key='reminder_owner_key' AND value=''", (owner_key,))
            db.execute("UPDATE settings SET value=? WHERE key='selected_owner_key' AND value=''", (owner_key,))

    def owners(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("""SELECT o.owner_key,o.label,o.updated_at,COUNT(s.id) AS count
                FROM owners o LEFT JOIN subscriptions s ON s.owner_key=o.owner_key
                GROUP BY o.owner_key ORDER BY o.updated_at DESC""").fetchall()
        return [dict(row) for row in rows]

    def _scope(self, owner_key: str) -> tuple[str, tuple]:
        if self.settings()["storage_scope"] == "unified":
            return "1=1", ()
        if owner_key == SHARED:
            raise ValueError("当前为分账户模式")
        return "owner_key=?", (owner_key,)

    def _decorate(self, row: sqlite3.Row | dict, today: date) -> dict:
        item = dict(row)
        due = next_due(date.fromisoformat(item["renewal_date"]), item["cycle_months"], today)
        item["next_due_date"] = due.isoformat() if due else None
        item["trial_active"] = bool(item["is_trial"] and item["trial_end_date"] >= today.isoformat())
        return item

    def list(self, owner_key: str) -> list[dict]:
        where, args = self._scope(owner_key)
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM subscriptions WHERE {where} ORDER BY name COLLATE NOCASE", args).fetchall()
        today = self.today()
        return [self._decorate(row, today) for row in rows]

    def get(self, owner_key: str, item_id: str) -> dict | None:
        where, args = self._scope(owner_key)
        with self._db() as db:
            row = db.execute(f"SELECT * FROM subscriptions WHERE id=? AND {where}", (item_id, *args)).fetchone()
        return self._decorate(row, self.today()) if row else None

    def save(self, owner_key: str, payload: dict, item_id: str | None = None) -> dict:
        with self._db() as db:
            if item_id:
                where, args = self._scope(owner_key)
                row = db.execute(f"SELECT * FROM subscriptions WHERE id=? AND {where}", (item_id, *args)).fetchone()
                if not row:
                    raise ValueError("订阅不存在或不在当前账本")
                fields = clean_fields({**dict(row), **payload})
                schedule_keys = ("renewal_date", "cycle_months", "reminder_days", "status", "reminder_enabled", "reminder_mode", "is_trial", "trial_end_date")
                changed = any(fields[key] != row[key] for key in schedule_keys)
                db.execute(f"UPDATE subscriptions SET {','.join(f'{key}=?' for key in FIELDS)},last_reminder_key=? WHERE id=?",
                           (*(fields[key] for key in FIELDS), "" if changed else row["last_reminder_key"], item_id))
            else:
                real_owner = self.settings()["reminder_owner_key"] if owner_key == SHARED else owner_key
                if not db.execute("SELECT 1 FROM owners WHERE owner_key=?", (real_owner,)).fetchone():
                    raise ValueError("请先在私聊中发送 /订阅，以绑定提醒会话")
                fields = clean_fields(payload)
                item_id = uuid.uuid4().hex[:12]
                columns = ",".join(("id", "owner_key", *FIELDS, "created_at"))
                placeholders = ",".join("?" for _ in range(len(FIELDS) + 3))
                db.execute(f"INSERT INTO subscriptions ({columns}) VALUES ({placeholders})",
                           (item_id, real_owner, *(fields[key] for key in FIELDS), datetime.now().isoformat()))
        result = self.get(owner_key, item_id)
        assert result is not None
        return result

    def delete(self, owner_key: str, item_id: str) -> bool:
        where, args = self._scope(owner_key)
        with self._db() as db:
            return db.execute(f"DELETE FROM subscriptions WHERE id=? AND {where}", (item_id, *args)).rowcount > 0

    def summary(self, owner_key: str) -> dict:
        totals: dict[str, dict[str, Decimal]] = {}
        for item in self.list(owner_key):
            if item["status"] != "active" or item["cycle_months"] == 0 or item["trial_active"]:
                continue
            bucket = totals.setdefault(item["currency"], {"monthly": Decimal(0), "yearly": Decimal(0)})
            yearly = Decimal(item["amount"]) * Decimal(12) / Decimal(item["cycle_months"])
            bucket["yearly"] += yearly
            bucket["monthly"] += yearly / Decimal(12)
        return {currency: {period: str(value.quantize(Decimal("0.01"))) for period, value in amounts.items()}
                for currency, amounts in sorted(totals.items())}

    def due_items(self) -> list[dict]:
        today, settings = self.today(), self.settings()
        with self._db() as db:
            rows = db.execute("""SELECT s.*,o.private_umo FROM subscriptions s
                JOIN owners o ON o.owner_key=s.owner_key WHERE s.status='active' AND s.reminder_enabled=1""").fetchall()
            shared_target = None
            if settings["storage_scope"] == "unified":
                shared_target = db.execute("SELECT private_umo FROM owners WHERE owner_key=?", (settings["reminder_owner_key"],)).fetchone()
        result = []
        for row in rows:
            item = self._decorate(row, today)
            if settings["storage_scope"] == "unified" and not shared_target:
                continue
            if shared_target:
                item["private_umo"] = shared_target["private_umo"]
            if not item["private_umo"]:
                continue
            due = item["trial_end_date"] if item["trial_active"] else item["next_due_date"]
            if not due:
                continue
            days = (date.fromisoformat(due) - today).days
            mode = item["reminder_mode"]
            early = 0 < days <= item["reminder_days"]
            on_day = days == 0
            when = (early and mode in ("before", "both")) or (on_day and (mode in ("day", "both") or item["reminder_days"] == 0))
            phase = "day" if on_day else "before"
            key = f"{'trial' if item['trial_active'] else 'renewal'}:{due}:{phase}"
            if when and key != item["last_reminder_key"] and item["last_reminded_for"] != due:
                item.update(reminder_key=key, reminder_due_date=due, days_until=days)
                result.append(item)
        return result

    def mark_reminded(self, item_id: str, key: str) -> None:
        with self._db() as db:
            db.execute("UPDATE subscriptions SET last_reminder_key=? WHERE id=?", (key, item_id))
