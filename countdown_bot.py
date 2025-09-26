# countdown_bot.py — PTB 21.6, asyncio task (без JobQueue)
# Доступ только для ALLOWED_IDS. Кнопка-таймер: "DD kun, HH:MM".
# Обновление по минутам, меняется только кнопка.

import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, CallbackQueryHandler, filters
)

# ---------- доступ ----------
ALLOWED_IDS = {5790925357, 1407015589, 573761807}

def is_allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_IDS)

# ---------- логирование ----------
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("countdown")

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
TZ = ZoneInfo(TZ_NAME)

# Состояния мастера
TEXT, DEADLINE, CHANNEL, LINK, CONFIRM = range(5)

# Активные фоновые задачи: (chat_id, message_id) -> asyncio.Task
TASKS: dict[tuple[int, int], asyncio.Task] = {}

# ---------- утилиты ----------
def parse_deadline(s: str) -> datetime:
    s = s.strip()
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.replace(tzinfo=TZ)
        except Exception:
            pass
    raise ValueError("Неверный формат. Пример: 2025-09-30 23:59 (или 2025-09-30 23:59:00)")

def fmt_dd_hh_mm(delta: timedelta) -> str:
    total = int(max(0, delta.total_seconds()))
    total -= total % 60
    d, r = divmod(total, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d:02d} kun, {h:02d}:{m:02d}"

def normalize_link(s: str | None) -> str | None:
    if not s:
        return None
    v = s.strip().lower()
    if v in ("null", "none", "-", "—"):
        return None
    return s.strip()

def make_keyboard(label: str, url: str | None) -> InlineKeyboardMarkup:
    if url:
        btn = InlineKeyboardButton(f"⏳ {label}", url=url)
    else:
        btn = InlineKeyboardButton(f"⏳ {label}", callback_data="noop")
    return InlineKeyboardMarkup([[btn]])

# ---------- фон: минутные апдейты ----------
async def ticker(bot, chat_id: int, message_id: int, deadline: datetime, url: str | None):
    key = (chat_id, message_id)
    try:
        while True:
            now = datetime.now(tz=TZ)
            left = deadline - now
            if left.total_seconds() <= 0:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ Aktsiya yakunlandi!")
                except Exception as e:
                    log.warning("final edit failed: %r", e)
                break

            label = fmt_dd_hh_mm(left)
            kb = make_keyboard(label, url)
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=kb)
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    log.error("BadRequest: %r", e)
            except (TimedOut, NetworkError) as e:
                log.warning("Network/Timeout: %r", e)
            except Exception as e:
                log.exception("Unexpected edit error: %r", e)

            now2 = datetime.now(tz=TZ)
            sleep_sec = 60 - now2.second
            await asyncio.sleep(sleep_sec)
    except asyncio.CancelledError:
        log.info("ticker cancelled for %s", key)
        raise
    finally:
        TASKS.pop(key, None)

# ---------- диалог ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    await context.application.bot.set_my_commands([("start", "Запустить мастер таймера"), ("stop", "Остановить все таймеры")])
    await update.message.reply_text("1) Заголовок сообщения (то, что будет над кнопкой).", parse_mode=ParseMode.HTML)
    return TEXT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

async def ask_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    context.user_data["text"] = update.message.text.strip()
    await update.message.reply_text(
        f"2) Дата/время окончания: <b>YYYY-MM-DD HH:MM</b> (таймзона: <b>{TZ_NAME}</b>)\nПример: <code>2025-09-30 23:59</code>",
        parse_mode=ParseMode.HTML,
    )
    return DEADLINE

async def ask_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    try:
        dt = parse_deadline(update.message.text)
    except Exception as e:
        await update.message.reply_text(str(e)); return DEADLINE
    if dt <= datetime.now(tz=TZ):
        await update.message.reply_text("Время уже прошло. Укажите будущую дату/время."); return DEADLINE
    context.user_data["deadline"] = dt
    await update.message.reply_text("3) Канал: @channel_username или ID -100xxxxxxxxxx.\nБот должен быть админом с правом редактировать сообщения.")
    return CHANNEL

async def ask_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    ch = update.message.text.strip()
    if not (ch.startswith("@") or ch.startswith("-100")):
        await update.message.reply_text("Неверный формат. Пример: @your_channel или -1001234567890")
        return CHANNEL
    context.user_data["channel"] = ch
    await update.message.reply_text("4) Ссылка для кнопки (URL). Если не нужна — отправьте: <code>null</code> или <code>-</code>.", parse_mode=ParseMode.HTML)
    return LINK

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    link = normalize_link(update.message.text)
    context.user_data["link"] = link

    text = context.user_data["text"]
    deadline: datetime = context.user_data["deadline"]
    ch = context.user_data["channel"]

    preview = (
        "<b>Проверка</b>\n"
        f"Заголовок: {text}\n"
        f"Окончание: {deadline.strftime('%Y-%m-%d %H:%M')} ({TZ_NAME})\n"
        f"Канал: {ch}\n"
        f"Ссылка: {link or '— нет —'}\n\n"
        "Нажмите «Отправить»."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить", callback_data="confirm_send"),
                                InlineKeyboardButton("❌ Отмена", callback_data="confirm_cancel")]])
    await update.message.reply_text(preview, parse_mode=ParseMode.HTML, reply_markup=kb)
    return CONFIRM

async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.callback_query.answer("Access denied.", show_alert=True)
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()

    if query.data == "confirm_cancel":
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    data = context.user_data
    header = data["text"]
    deadline: datetime = data["deadline"]
    channel = data["channel"]
    url: str | None = data["link"]

    first_label = fmt_dd_hh_mm(deadline - datetime.now(tz=TZ))
    try:
        msg = await context.bot.send_message(
            chat_id=channel, text=header, reply_markup=make_keyboard(first_label, url), parse_mode=ParseMode.HTML
        )
        log.info("Posted to %s (msg_id=%s)", channel, msg.message_id)
    except Exception as e:
        await query.edit_message_text(f"Ошибка отправки в канал: {e}")
        log.exception("Send error: %r", e)
        return ConversationHandler.END

    await query.edit_message_text("Yuborildi. Taymer ishga tushdi.")

    key = (msg.chat.id, msg.message_id)
    task = context.application.create_task(
        ticker(context.bot, msg.chat.id, msg.message_id, deadline, url),
        name=f"ticker-{msg.chat.id}-{msg.message_id}"
    )
    TASKS[key] = task
    return ConversationHandler.END

# no-op для кнопки без ссылки
async def noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.callback_query.answer("Access denied.", show_alert=True)
        return
    await update.callback_query.answer("")

async def stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return
    n = 0
    for key, task in list(TASKS.items()):
        if not task.done():
            task.cancel()
            n += 1
        TASKS.pop(key, None)
    await update.message.reply_text(f"Остановлено задач: {n}")
    log.info("Stopped %d tasks", n)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN пуст в .env")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start, filters=filters.User(ALLOWED_IDS))],
        states={
            TEXT: [MessageHandler(filters.User(ALLOWED_IDS) & filters.TEXT & ~filters.COMMAND, ask_deadline)],
            DEADLINE: [MessageHandler(filters.User(ALLOWED_IDS) & filters.TEXT & ~filters.COMMAND, ask_channel)],
            CHANNEL: [MessageHandler(filters.User(ALLOWED_IDS) & filters.TEXT & ~filters.COMMAND, ask_link)],
            LINK: [MessageHandler(filters.User(ALLOWED_IDS) & filters.TEXT & ~filters.COMMAND, confirm)],
            # CallbackQueryHandler не поддерживает filters -> проверяем в on_confirm()
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern="^confirm_(send|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=filters.User(ALLOWED_IDS))],
        per_chat=True,
        per_user=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(noop_cb, pattern="^noop$"))
    app.add_handler(CommandHandler("stop", stop_all, filters=filters.User(ALLOWED_IDS)))

    app.run_polling()

if __name__ == "__main__":
    main()
