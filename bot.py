import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
)

URL = "https://www.auto-meh.ru/student/zameni/"
STATE_PATH = Path(os.getenv("STATE_FILE", "state.json"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("schedule-replacement-bot")


@dataclass
class PageSnapshot:
    signature: str
    short_text: str
    fetched_at: str


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "subscribers": [],
            "last_signature": None,
            "last_short_text": "",
            "last_checked_at": None,
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def add_subscriber(self, chat_id: int) -> bool:
        subs = set(self.data.get("subscribers", []))
        before = len(subs)
        subs.add(chat_id)
        self.data["subscribers"] = sorted(subs)
        changed = len(subs) != before
        if changed:
            self.save()
        return changed

    def remove_subscriber(self, chat_id: int) -> bool:
        subs = set(self.data.get("subscribers", []))
        if chat_id not in subs:
            return False
        subs.remove(chat_id)
        self.data["subscribers"] = sorted(subs)
        self.save()
        return True

    @property
    def subscribers(self) -> list[int]:
        return [int(x) for x in self.data.get("subscribers", [])]


class ReplacementWatcher:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def fetch_snapshot(self) -> PageSnapshot:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(URL) as resp:
                resp.raise_for_status()
                html = await resp.text()

        signature, short_text = self._build_signature(html)
        return PageSnapshot(
            signature=signature,
            short_text=short_text,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def _build_signature(self, html: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")

        content = (
            soup.select_one("main")
            or soup.select_one("article")
            or soup.select_one(".content")
            or soup.body
            or soup
        )

        for tag in content.select("script, style, noscript"):
            tag.decompose()

        text = " ".join(content.get_text(" ", strip=True).split())
        normalized = text.lower().strip()
        signature = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        short_text = text[:400] + ("..." if len(text) > 400 else "")
        return signature, short_text

    async def check_for_updates(self) -> tuple[bool, PageSnapshot]:
        snapshot = await self.fetch_snapshot()
        previous = self.state.data.get("last_signature")
        changed = previous is not None and snapshot.signature != previous

        self.state.data["last_signature"] = snapshot.signature
        self.state.data["last_short_text"] = snapshot.short_text
        self.state.data["last_checked_at"] = snapshot.fetched_at
        self.state.save()

        return changed, snapshot


state = StateStore(STATE_PATH)
watcher = ReplacementWatcher(state)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    added = state.add_subscriber(chat_id)
    message = (
        "Готово! Вы подписаны на уведомления о заменах в расписании."
        if added
        else "Вы уже подписаны на уведомления."
    )
    await update.message.reply_text(
        f"{message}\n\nПроверяю страницу каждые {CHECK_INTERVAL} сек."
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    removed = state.remove_subscriber(update.effective_chat.id)
    await update.message.reply_text(
        "Вы отписались от уведомлений." if removed else "Вы и так не были подписаны."
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        changed, snapshot = await watcher.check_for_updates()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Manual check failed")
        await update.message.reply_text(f"Не удалось проверить страницу: {exc}")
        return

    status = "⚠️ Обнаружено обновление" if changed else "✅ Новых изменений не найдено"
    await update.message.reply_text(
        f"{status}\n\n{snapshot.short_text}\n\nИсточник: {URL}"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    last_checked = state.data.get("last_checked_at") or "ещё не проверяли"
    subscribers = len(state.subscribers)
    await update.message.reply_text(
        "Статус бота:\n"
        f"• Подписчиков: {subscribers}\n"
        f"• Последняя проверка: {last_checked}\n"
        f"• Интервал: {CHECK_INTERVAL} сек"
    )


async def periodic_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        changed, snapshot = await watcher.check_for_updates()
    except Exception:  # noqa: BLE001
        logger.exception("Periodic check failed")
        return

    if not changed:
        logger.info("No changes detected")
        return

    logger.info("Change detected, notifying subscribers")
    for chat_id in state.subscribers:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🚨 На странице замен расписания обнаружены изменения!\n\n"
                    f"{snapshot.short_text}\n\n"
                    f"Проверьте детали: {URL}"
                ),
            )
            await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify chat_id=%s", chat_id)


async def post_init(application: Application) -> None:
    application.job_queue.run_repeating(
        periodic_check,
        interval=CHECK_INTERVAL,
        first=5,
        name="periodic-check",
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите BOT_TOKEN в переменных окружения")

    app = (
        Application.builder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
