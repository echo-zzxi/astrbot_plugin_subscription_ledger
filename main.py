"""AstrBot entry point for the subscription ledger."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .ledger import Ledger, make_owner_key, domain_of

PLUGIN_NAME = "astrbot_plugin_subscription_ledger"
SYMBOLS = {"CNY": "¥", "USD": "$", "HKD": "HK$", "SGD": "S$", "EUR": "€", "JPY": "¥", "GBP": "£"}


def _rates(base: str, quotes: list[str]) -> dict:
    """Fetch reference rates from a fixed trusted host; never pass user URLs."""
    from urllib.parse import urlencode
    url = "https://api.frankfurter.dev/v2/rates?" + urlencode({"base": base, "quotes": ",".join(quotes)})
    req = Request(url, headers={"User-Agent": "AstrBot-Subscription-Ledger/0.4"})
    with urlopen(req, timeout=8) as response:
        if response.status != 200:
            raise ValueError("汇率服务暂不可用")
        data = json.loads(response.read(65536))
    if not isinstance(data, list):
        raise ValueError("汇率响应格式错误")
    return {row["quote"]: {"rate": row["rate"], "date": row["date"]}
            for row in data if isinstance(row, dict) and row.get("quote") in quotes}


def _logo(domain: str) -> str:
    req = Request(f"https://logo.debounce.com/{domain}", headers={"User-Agent": "AstrBot-Subscription-Ledger/0.4"})
    with urlopen(req, timeout=8) as response:
        mime = response.headers.get_content_type()
        data = response.read(131073)
    if mime not in ("image/png", "image/jpeg", "image/webp") or len(data) > 131072:
        raise ValueError("图标服务返回了无效图片")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class SubscriptionLedgerPlugin(Star):
    """Private-chat tools, administrator page APIs, and reminder loop."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.ledger = Ledger(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "subscriptions.sqlite3")
        self._sync_native_settings()
        self._reminder_task: asyncio.Task | None = None
        self._fx_cache: dict[tuple, tuple[float, dict]] = {}
        self._logo_cache: dict[str, tuple[float, str]] = {}
        for path, handler, methods, description in (
            ("owners", self.web_owners, ["GET"], "List private chat owners"),
            ("settings", self.web_settings, ["GET", "POST"], "Ledger settings"),
            ("subscriptions", self.web_subscriptions, ["GET"], "List subscriptions"),
            ("subscriptions/save", self.web_save, ["POST"], "Create or update subscription"),
            ("subscriptions/delete", self.web_delete, ["POST"], "Delete subscription"),
            ("fx", self.web_fx, ["GET"], "Read reference exchange rates"),
            ("logo", self.web_logo, ["GET"], "Read service logo from fixed provider"),
        ):
            context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods, description)
        self._reminder_task = asyncio.create_task(self._reminder_loop())
        logger.info("Subscription Ledger loaded")

    def _sync_config_from_ledger(self) -> None:
        settings = self.ledger.settings()
        for key in ("storage_scope", "reminder_owner_key", "timezone", "currency_mode", "base_currency"):
            self.config[key] = settings[key]
        self.config.save_config()

    def _sync_native_settings(self) -> None:
        if not self.ledger.native_config_synced():
            # The first native config file contains schema defaults. Preserve the
            # values already selected in the existing web UI and SQLite database.
            self._sync_config_from_ledger()
            self.ledger.mark_native_config_synced()
            return
        current = self.ledger.settings()
        payload = {key: self.config.get(key, current[key]) for key in
                   ("storage_scope", "timezone", "currency_mode", "base_currency")}
        # A blank native value means to keep the current private-chat recipient.
        payload["reminder_owner_key"] = self.config.get("reminder_owner_key") or current["reminder_owner_key"]
        try:
            self.ledger.save_settings(payload)
        except ValueError as exc:
            logger.warning("Subscription Ledger native settings rejected: %s", exc)
            self._sync_config_from_ledger()

    async def terminate(self) -> None:
        if self._reminder_task:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass

    def _private_owner(self, event: AstrMessageEvent) -> str:
        if not event.is_private_chat():
            raise ValueError("订阅只能在私聊中管理")
        owner = make_owner_key(str(event.get_platform_name()), str(event.get_self_id()), str(event.get_sender_id()))
        self.ledger.touch_owner(owner, event.get_sender_name() or str(event.get_sender_id()), event.unified_msg_origin)
        return owner

    @filter.command("订阅")
    async def subscriptions_command(self, event: AstrMessageEvent):
        """查看当前账本并绑定私聊提醒会话。"""
        try:
            owner = self._private_owner(event)
            items = self.ledger.list(owner)
            if not items:
                yield event.plain_result("订阅账本已就绪。暂无记录；可让 Agent 添加，或去插件页面录入。")
                return
            lines = ["订阅账本："]
            for item in items:
                due = item["next_due_date"] or "已过期"
                symbol = SYMBOLS.get(item["currency"], item["currency"] + " ")
                trial = " · 试用中" if item["trial_active"] else ""
                lines.append(f"• {item['name']} · {symbol}{item['amount']} {item['currency']} · 下次 {due}{trial} · ID {item['id']}")
            yield event.plain_result("\n".join(lines))
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="subscription_list")
    async def subscription_list(self, event: AstrMessageEvent, query: str = ""):
        """列出当前账本的订阅。统一模式下所有私聊共享，分账户模式下仅当前用户可见。

        Args:
            query(string): 可选的名称或标签筛选词。
        """
        try:
            items = self.ledger.list(self._private_owner(event))
            if query:
                items = [item for item in items if query.casefold() in (item["name"] + " " + item["category"]).casefold()]
            if not items:
                yield event.plain_result("没有找到订阅记录。")
                return
            lines = []
            for item in items:
                trial = f" | 试用至 {item['trial_end_date']}" if item["trial_active"] else ""
                lines.append(f"{item['id']} | {item['name']} | {SYMBOLS.get(item['currency'], item['currency'] + ' ')}{item['amount']} {item['currency']} | 下次 {item['next_due_date'] or '已过期'} | {item['category']} | {item['status']}{trial}")
            yield event.plain_result("\n".join(lines))
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="subscription_summary")
    async def subscription_summary(self, event: AstrMessageEvent):
        """按原币种汇总当前账本月均和年均预计支出，试用中和暂停的订阅不计入。"""
        try:
            totals = self.ledger.summary(self._private_owner(event))
            if not totals:
                yield event.plain_result("暂无启用中的付费周期订阅。")
                return
            yield event.plain_result("\n".join(
                f"{currency}：月均 {SYMBOLS.get(currency, currency + ' ')}{amounts['monthly']}，年均 {SYMBOLS.get(currency, currency + ' ')}{amounts['yearly']}"
                for currency, amounts in totals.items()))
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="subscription_add")
    async def subscription_add(
        self, event: AstrMessageEvent, name: str, renewal_date: str,
        amount: str = "0", currency: str = "CNY", cycle_months: int = 1,
        reminder_days: int = 3, notes: str = "", renewal_mode: str = "manual",
        icon_url: str = "", service_domain: str = "", category: str = "",
        payment_method: str = "unknown", payment_other: str = "",
        reminder_enabled: bool = True, reminder_mode: str = "before",
        is_trial: bool = False, trial_end_date: str = "",
    ):
        """为当前账本添加一项订阅。统一模式下其他私聊用户也能看到并管理。

        Args:
            name(string): 服务名称。
            renewal_date(string): 首次扣款或续费日期，YYYY-MM-DD。
            amount(string): 每期金额，最多两位小数。
            currency(string): 三字母币种，如 CNY、USD、HKD。
            cycle_months(number): 0 单次、1 月、3 季、6 半年、12 年。
            reminder_days(number): 提前 0–30 天。
            notes(string): 可选备注。
            renewal_mode(string): manual 手动或 auto 自动续费。
            icon_url(string): 可选的完整 HTTP(S) 图标链接。
            service_domain(string): 服务官网域名，例如 openai.com，用于自动图标。
            category(string): 可选标签或类别，留空不显示。
            payment_method(string): unknown、alipay、wechat、bank_card、credit_card、paypal、apple_pay、google_pay、other。
            payment_other(string): payment_method 为 other 时的自定义名称。
            reminder_enabled(boolean): 是否提醒。
            reminder_mode(string): before 提前一次、day 当天、both 两次。
            is_trial(boolean): 是否处于试用期。
            trial_end_date(string): 试用结束日期，YYYY-MM-DD；试用时必填。
        """
        try:
            owner = self._private_owner(event)
            values = locals().copy()
            values.pop("self"); values.pop("event"); values.pop("owner")
            item = self.ledger.save(owner, values)
            yield event.plain_result(f"已添加 {item['name']}（ID {item['id']}），下次 {item['next_due_date']}。")
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="subscription_update")
    async def subscription_update(
        self, event: AstrMessageEvent, item_id: str, name: str = "",
        renewal_date: str = "", amount: str = "", currency: str = "",
        cycle_months: int = -1, reminder_days: int = -1,
        notes: str = "__UNCHANGED__", renewal_mode: str = "", status: str = "",
        icon_url: str = "__UNCHANGED__", service_domain: str = "__UNCHANGED__",
        category: str = "__UNCHANGED__", payment_method: str = "", payment_other: str = "__UNCHANGED__",
        reminder_enabled: str = "", reminder_mode: str = "", is_trial: str = "",
        trial_end_date: str = "__UNCHANGED__",
    ):
        """按 ID 修改订阅，未提供的字段保持原值。

        Args:
            item_id(string): 要修改的订阅 ID。
            name(string): 新名称，留空不改。
            renewal_date(string): 新扣款日，YYYY-MM-DD；留空不改。
            amount(string): 新金额，留空不改。
            currency(string): 新币种，留空不改。
            cycle_months(number): 新周期月数，-1 不改。
            reminder_days(number): 新提前天数，-1 不改。
            notes(string): 新备注；__UNCHANGED__ 不改，空串清空。
            renewal_mode(string): manual 或 auto；留空不改。
            status(string): active 或 paused；留空不改。
            icon_url(string): 新图标 URL；__UNCHANGED__ 不改，空串清空。
            service_domain(string): 新官网域名；__UNCHANGED__ 不改。
            category(string): 新标签；__UNCHANGED__ 不改，空串清空。
            payment_method(string): 付款方式代码；留空不改。
            payment_other(string): 其它付款方式名称；__UNCHANGED__ 不改。
            reminder_enabled(string): true/false；留空不改。
            reminder_mode(string): before/day/both；留空不改。
            is_trial(string): true/false；留空不改。
            trial_end_date(string): YYYY-MM-DD；__UNCHANGED__ 不改。
        """
        try:
            owner = self._private_owner(event)
            updates = {}
            for key in ("name", "renewal_date", "amount", "currency", "renewal_mode", "status", "payment_method", "reminder_mode", "reminder_enabled", "is_trial"):
                value = locals()[key]
                if value != "":
                    updates[key] = value
            for key in ("cycle_months", "reminder_days"):
                value = locals()[key]
                if value != -1:
                    updates[key] = value
            for key in ("notes", "icon_url", "service_domain", "category", "payment_other", "trial_end_date"):
                value = locals()[key]
                if value != "__UNCHANGED__":
                    updates[key] = value
            if not updates:
                raise ValueError("请提供要修改的字段")
            item = self.ledger.save(owner, updates, item_id)
            yield event.plain_result(f"已更新 {item['name']}，下次 {item['next_due_date']}。")
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="subscription_delete")
    async def subscription_delete(self, event: AstrMessageEvent, item_id: str, confirm: bool = False):
        """删除当前账本的一条订阅，仅在用户明确要求删除时传 confirm=true。

        Args:
            item_id(string): 要删除的订阅 ID。
            confirm(boolean): 用户明确要求删除时为 true。
        """
        try:
            owner = self._private_owner(event)
            if not confirm:
                yield event.plain_result("未删除。请先确认订阅名称及 ID。")
                return
            if not self.ledger.delete(owner, item_id):
                raise ValueError("订阅不存在或不在当前账本")
            yield event.plain_result(f"已删除订阅 {item_id}。")
        except ValueError as exc:
            yield event.plain_result(str(exc))

    async def web_owners(self):
        return json_response({"owners": self.ledger.owners()})

    async def web_settings(self):
        if request.method == "GET":
            return json_response({"settings": self.ledger.settings(), "server_today": self.ledger.today().isoformat()})
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("数据格式错误", status_code=400)
        try:
            settings = self.ledger.save_settings(payload)
            if any(key in payload for key in ("storage_scope", "reminder_owner_key", "timezone", "currency_mode", "base_currency")):
                self._sync_config_from_ledger()
            return json_response({"settings": settings})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_subscriptions(self):
        owner = request.query.get("owner")
        if not owner:
            return error_response("请选择账本", status_code=400)
        try:
            return json_response({"items": self.ledger.list(owner), "summary": self.ledger.summary(owner)})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("数据格式错误", status_code=400)
        owner, item_id = payload.get("owner_key"), payload.get("id")
        if not isinstance(owner, str) or not owner or (item_id is not None and not isinstance(item_id, str)):
            return error_response("账本或 ID 格式错误", status_code=400)
        try:
            return json_response({"item": self.ledger.save(owner, payload, item_id)})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def web_delete(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not isinstance(payload.get("owner_key"), str) or not isinstance(payload.get("id"), str):
            return error_response("账本或 ID 格式错误", status_code=400)
        try:
            deleted = self.ledger.delete(payload["owner_key"], payload["id"])
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"deleted": True}) if deleted else error_response("订阅不存在", status_code=404)

    async def web_fx(self):
        base = request.query.get("base", "CNY").upper()
        quotes = sorted({quote for quote in request.query.get("quotes", "").upper().split(",") if quote})
        if base not in ("CNY", "USD", "HKD", "SGD", "EUR", "JPY", "GBP") or len(quotes) > 20 or any(len(q) != 3 or not q.isalpha() or not q.isascii() for q in quotes):
            return error_response("币种参数错误", status_code=400)
        if not quotes:
            return json_response({"rates": {}, "base": base})
        key = (base, *quotes)
        cache = self._fx_cache.get(key)
        if cache and time.monotonic() - cache[0] < 21600:
            return json_response({"rates": cache[1], "base": base})
        try:
            rates = await asyncio.to_thread(_rates, base, quotes)
            self._fx_cache[key] = (time.monotonic(), rates)
            return json_response({"rates": rates, "base": base})
        except Exception:
            logger.warning("Subscription Ledger FX lookup failed")
            return error_response("参考汇率暂不可用，请按币种分别查看", status_code=503)

    async def web_logo(self):
        try:
            domain = domain_of(request.query.get("domain", ""))
            if not domain:
                raise ValueError("缺少服务域名")
            cached = self._logo_cache.get(domain)
            if cached and time.monotonic() - cached[0] < 86400:
                return json_response({"data_url": cached[1]})
            data_url = await asyncio.to_thread(_logo, domain)
            if len(self._logo_cache) >= 128:
                oldest = min(self._logo_cache, key=lambda key: self._logo_cache[key][0])
                self._logo_cache.pop(oldest, None)
            self._logo_cache[domain] = (time.monotonic(), data_url)
            return json_response({"data_url": data_url})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.warning("Subscription Ledger logo lookup failed")
            return error_response("图标暂不可用", status_code=503)

    async def _reminder_loop(self) -> None:
        while True:
            try:
                for item in self.ledger.due_items():
                    try:
                        kind = "试用结束" if item["trial_active"] else ("自动扣款" if item["renewal_mode"] == "auto" else "手动续费")
                        when = "今天" if item["days_until"] == 0 else f"{item['days_until']} 天后"
                        symbol = SYMBOLS.get(item["currency"], item["currency"] + " ")
                        message = f"订阅提醒：{item['name']} {when}（{item['reminder_due_date']}）{kind}；预计 {symbol}{item['amount']} {item['currency']}。"
                        await self.context.send_message(item["private_umo"], MessageChain().message(message))
                        self.ledger.mark_reminded(item["id"], item["reminder_key"])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Subscription Ledger reminder delivery failed for item %s", item["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Subscription Ledger reminder scan failed")
            await asyncio.sleep(300)
