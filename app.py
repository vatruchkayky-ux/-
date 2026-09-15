import asyncio
import json
import logging
import os
import time
from dotenv import load_dotenv

load_dotenv("env.txt")

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

# ==================== НАСТРОЙКИ (из env.txt) ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
LOG_BOT_TOKEN = os.environ["LOG_BOT_TOKEN"]
LOG_CHAT_ID = int(os.environ["LOG_CHAT_ID"])
OWNER_ID = int(os.environ["OWNER_ID"])
ADMIN_IDS = [OWNER_ID]

USERS_FILE = "/data/users.json"

COOLDOWN_SEC = 30
MAX_TEXT_LEN = 500
MAX_NICK_LEN = 20
MIN_NICK_LEN = 2

PROTECTED_NICKS = ["правитель", "правител", "pravitel", "prawitel", "правители"]
MUTE_DURATION_SEC = 40 * 60

logging.basicConfig(level=logging.WARNING)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

log_bot = Bot(token=LOG_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp_log = Dispatcher(storage=MemoryStorage())

# ==================== ХРАНИЛИЩЕ ====================
users = {}
drafts = {}
cooldowns = {}
bot_username = None


def load_users():
    global users
    try:
        os.makedirs("/data", exist_ok=True)
    except Exception:
        pass
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                users = json.load(f)
        except Exception:
            users = {}


def save_users():
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"save: {e}")


def ensure_user(user_id: int):
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "nickname": None, "last_text": None, "seen_rules": True,
            "banned": False, "mute_until": 0, "reason": ""
        }
    else:
        users[uid].setdefault("banned", False)
        users[uid].setdefault("mute_until", 0)
        users[uid].setdefault("reason", "")
        users[uid].setdefault("seen_rules", True)


def get_nick(user_id: int) -> str:
    u = users.get(str(user_id))
    return u["nickname"] if u and u.get("nickname") else "аноним"


def has_nick(user_id: int) -> bool:
    u = users.get(str(user_id))
    return bool(u and u.get("nickname"))


def seen_rules(user_id: int) -> bool:
    u = users.get(str(user_id))
    return bool(u and u.get("seen_rules"))


def mark_rules_seen(user_id: int):
    ensure_user(user_id)
    users[str(user_id)]["seen_rules"] = True
    save_users()


def is_protected_nick(nick: str) -> bool:
    return nick.strip().lower() in PROTECTED_NICKS


def nick_is_taken_by_other(nick: str, user_id: int) -> bool:
    nick_lower = nick.strip().lower()
    for uid, data in users.items():
        if uid == str(user_id):
            continue
        other = (data.get("nickname") or "").strip().lower()
        if other == nick_lower:
            return True
    return False


# ==================== НАКАЗАНИЯ ====================
def is_banned(user_id: int) -> bool:
    u = users.get(str(user_id))
    return bool(u and u.get("banned"))


def get_mute_left(user_id: int) -> int:
    u = users.get(str(user_id))
    if not u:
        return 0
    return max(0, int(u.get("mute_until", 0) - time.time()))


def get_punish_reason(user_id: int) -> str:
    u = users.get(str(user_id))
    return (u.get("reason") if u else "") or "нарушение правил чата"


def punish_message(user_id: int) -> str:
    if is_banned(user_id):
        return (
            "🚫 <b>Вы забанены за нарушение правил.</b>\n\n"
            f"Причина: {get_punish_reason(user_id)}\n"
            "Срок: <b>навсегда</b>"
        )
    left = get_mute_left(user_id)
    if left > 0:
        mins = left // 60
        secs = left % 60
        return (
            "🔇 <b>Вы в муте за нарушение правил.</b>\n\n"
            f"Причина: {get_punish_reason(user_id)}\n"
            f"Осталось: <b>{mins} мин {secs} сек</b>"
        )
    return ""


def set_mute(user_id: int, reason: str = "нарушение правил чата"):
    ensure_user(user_id)
    users[str(user_id)]["banned"] = False
    users[str(user_id)]["mute_until"] = time.time() + MUTE_DURATION_SEC
    users[str(user_id)]["reason"] = reason
    save_users()


def set_ban(user_id: int, reason: str = "нарушение правил чата"):
    ensure_user(user_id)
    users[str(user_id)]["banned"] = True
    users[str(user_id)]["mute_until"] = 0
    users[str(user_id)]["reason"] = reason
    save_users()


def unban(user_id: int):
    ensure_user(user_id)
    users[str(user_id)]["banned"] = False
    users[str(user_id)]["mute_until"] = 0
    users[str(user_id)]["reason"] = ""
    save_users()


# ==================== ЧЕРНОВИКИ ====================
def new_draft():
    return {
        "text": None, "photo": None, "voice": None, "video": None,
        "video_note": None, "sticker": None, "document": None,
        "animation": None, "audio": None,
        "reply_to": None, "reply_nick": None,
    }


def draft_has_content(d: dict) -> bool:
    return any([
        d.get("text"), d.get("photo"), d.get("voice"), d.get("video"),
        d.get("video_note"), d.get("sticker"), d.get("document"),
        d.get("animation"), d.get("audio"),
    ])


def clear_media(d: dict):
    for k in ["photo", "voice", "video", "video_note",
              "sticker", "document", "animation", "audio"]:
        d[k] = None


def check_cooldown(user_id: int) -> int:
    last = cooldowns.get(user_id, 0)
    elapsed = time.time() - last
    if elapsed >= COOLDOWN_SEC:
        return 0
    return int(COOLDOWN_SEC - elapsed)


def set_cooldown(user_id: int):
    cooldowns[user_id] = time.time()


async def send_log(text: str, kb: InlineKeyboardMarkup = None):
    try:
        await log_bot.send_message(LOG_CHAT_ID, text, reply_markup=kb)
    except Exception as e:
        logging.error(f"log error: {e}")


class Form(StatesGroup):
    nickname = State()
    message = State()


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Отправить сообщение")],
        [KeyboardButton(text="🎭 Сменить имя"), KeyboardButton(text="👤 Моё имя")],
    ],
    resize_keyboard=True
)

CANCEL = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)


def post_kb(user_id: int, nickname: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💬 Ответить {nickname}",
            url=f"https://t.me/{bot_username}?start=reply_{user_id}"
        )],
        [InlineKeyboardButton(
            text=f"🤖 Бот: @{bot_username}",
            url=f"https://t.me/{bot_username}"
        )],
    ])


def draft_kb(has_media: bool):
    rows = [[InlineKeyboardButton(text="✅ Отправить", callback_data="send")]]
    if has_media:
        rows.append([InlineKeyboardButton(text="🗑 Убрать вложение", callback_data="delmedia")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def log_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔇 Мут 40 мин", callback_data=f"mod:mute:{user_id}"),
            InlineKeyboardButton(text="🔨 Бан", callback_data=f"mod:ban:{user_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Разбанить / снять мут", callback_data=f"mod:unban:{user_id}"),
        ],
    ])


RULES_TEXT = (
    "📜 <b>Правила анонимного чата</b>\n\n"
    "🔹 <b>Соблюдайте закон</b>\n"
    "Всё, что вы пишете, должно быть в рамках законодательства.\n\n"
    "🔹 <b>Запрещена реклама</b>\n"
    "Никакой рекламы, спама, самопиара, ссылок.\n\n"
    "🔹 <b>Запрещены оскорбления</b>\n"
    "Угрозы, мат, оскорбления, NSFW, экстремизм — бан.\n\n"
    "🔹 <b>Не выдавайте себя за других</b>\n"
    "Запрещено использовать чужие имена.\n\n"
    "🔹 <b>Лимиты</b>\n"
    f"• Текст — до <b>{MAX_TEXT_LEN}</b> символов\n"
    f"• Имя — от <b>{MIN_NICK_LEN}</b> до <b>{MAX_NICK_LEN}</b> символов\n"
    f"• Между сообщениями — <b>{COOLDOWN_SEC} сек</b>\n\n"
    "🔹 <b>Наказания</b>\n"
    "За нарушения — мут на 40 минут или бан навсегда.\n\n"
    "Нажмите кнопку ниже, чтобы продолжить."
)

RULES_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Я согласен", callback_data="accept_rules")
]])


# ==================== ОСНОВНОЙ БОТ ====================
@dp.callback_query(F.data == "accept_rules")
async def accept_rules(cb: types.CallbackQuery, state: FSMContext):
    mark_rules_seen(cb.from_user.id)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Принято ✅")

    uid = cb.from_user.id
    if not has_nick(uid):
        await cb.message.answer(
            f"Выберите имя (от {MIN_NICK_LEN} до {MAX_NICK_LEN} символов):",
            reply_markup=CANCEL
        )
        await state.set_state(Form.nickname)
    else:
        await cb.message.answer(
            f"👋 С возвращением, <b>{get_nick(uid)}</b>!\n\n"
            f"Жмите «✍️ Отправить сообщение».",
            reply_markup=MENU
        )


@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    ensure_user(uid)

    pun = punish_message(uid)
    if pun:
        await message.answer(pun)
        return

    if not seen_rules(uid):
        await message.answer(RULES_TEXT, reply_markup=RULES_KB)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("reply_"):
        try:
            target_id = int(parts[1].replace("reply_", ""))
        except ValueError:
            await message.answer("Ошибка ссылки.", reply_markup=MENU)
            return

        if not has_nick(uid):
            d = new_draft()
            d["reply_to"] = target_id
            d["reply_nick"] = get_nick(target_id)
            drafts[uid] = d
            await message.answer(
                f"Выберите имя (от {MIN_NICK_LEN} до {MAX_NICK_LEN} символов):",
                reply_markup=CANCEL
            )
            await state.set_state(Form.nickname)
            return

        await start_reply(message, target_id, state)
        return

    if not has_nick(uid):
        await message.answer(
            f"Выберите имя (от {MIN_NICK_LEN} до {MAX_NICK_LEN} символов):",
            reply_markup=CANCEL
        )
        await state.set_state(Form.nickname)
    else:
        await message.answer(
            f"👋 С возвращением, <b>{get_nick(uid)}</b>!\n\n"
            f"Жмите «✍️ Отправить сообщение», чтобы написать в канал.",
            reply_markup=MENU
        )


@dp.message(Command("cancel"))
async def cancel(message: types.Message, state: FSMContext):
    await state.clear()
    drafts.pop(message.from_user.id, None)
    await message.answer("Отменено.", reply_markup=MENU)


@dp.message(Form.nickname)
async def set_nick(message: types.Message, state: FSMContext):
    if not message.text or message.text == "❌ Отмена":
        await state.clear()
        drafts.pop(message.from_user.id, None)
        await message.answer("Отменено.", reply_markup=MENU)
        return

    uid = message.from_user.id
    nick = message.text.strip()

    if len(nick) < MIN_NICK_LEN:
        await message.answer(
            f"❌ Слишком короткое имя: <b>{len(nick)}</b> символов.\n\n"
            f"Минимум: <b>{MIN_NICK_LEN}</b>. Попробуйте другое:"
        )
        return

    if len(nick) > MAX_NICK_LEN:
        await message.answer(
            f"❌ Слишком длинное имя: <b>{len(nick)}</b> символов.\n\n"
            f"Максимум: <b>{MAX_NICK_LEN}</b>. Сократите и попробуйте снова:"
        )
        return

    if is_protected_nick(nick) and uid not in ADMIN_IDS:
        await message.answer("❌ Это имя зарезервировано.\n\nВыберите другое:")
        return

    if nick_is_taken_by_other(nick, uid):
        await message.answer("❌ Это имя уже занято.\n\nВыберите другое:")
        return

    ensure_user(uid)
    users[str(uid)]["nickname"] = nick
    save_users()
    await state.clear()

    d = drafts.get(uid)
    if d and d.get("reply_to"):
        await start_reply(message, d["reply_to"], state)
        return

    await message.answer(f"✅ Ваше имя: <b>{nick}</b>", reply_markup=MENU)


@dp.message(F.text == "🎭 Сменить имя")
async def change_nick(message: types.Message, state: FSMContext):
    await message.answer(
        f"Выберите имя (от {MIN_NICK_LEN} до {MAX_NICK_LEN} символов):",
        reply_markup=CANCEL
    )
    await state.set_state(Form.nickname)


@dp.message(F.text == "👤 Моё имя")
async def show_nick(message: types.Message):
    await message.answer(f"Ваше имя: <b>{get_nick(message.from_user.id)}</b>")


@dp.message(F.text == "✍️ Отправить сообщение")
async def start_msg(message: types.Message, state: FSMContext):
    uid = message.from_user.id

    pun = punish_message(uid)
    if pun:
        await message.answer(pun)
        return

    if not has_nick(uid):
        await message.answer("Сначала задайте имя — /start")
        return

    d = drafts.get(uid)
    if d and d.get("reply_to"):
        await message.answer(
            f"📝 Напишите ответ (до {MAX_TEXT_LEN} символов, можно с фото/голосовым):",
            reply_markup=CANCEL
        )
    else:
        drafts[uid] = new_draft()
        await message.answer(
            f"📝 Отправьте текст (до {MAX_TEXT_LEN} символов) или медиа:",
            reply_markup=CANCEL
        )
    await state.set_state(Form.message)


@dp.message(Form.message, F.text == "❌ Отмена")
async def cancel_draft(message: types.Message, state: FSMContext):
    drafts.pop(message.from_user.id, None)
    await state.clear()
    await message.answer("Отменено.", reply_markup=MENU)


def _check_punish(message: types.Message) -> bool:
    return bool(punish_message(message.from_user.id))


async def _check_caption(message: types.Message) -> bool:
    if message.caption and len(message.caption) > MAX_TEXT_LEN:
        await message.answer(
            f"❌ Подпись слишком длинная: <b>{len(message.caption)}</b> символов.\n"
            f"Максимум: <b>{MAX_TEXT_LEN}</b>. Сократите и попробуйте снова."
        )
        return True
    return False


@dp.message(Form.message, F.photo)
async def draft_photo(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    if await _check_caption(message):
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["photo"] = message.photo[-1].file_id
    if message.caption:
        d["text"] = message.caption
    await show_draft(message, d)


@dp.message(Form.message, F.voice)
async def draft_voice(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    if await _check_caption(message):
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["voice"] = message.voice.file_id
    if message.caption:
        d["text"] = message.caption
    await show_draft(message, d)


@dp.message(Form.message, F.video_note)
async def draft_video_note(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["video_note"] = message.video_note.file_id
    await show_draft(message, d)


@dp.message(Form.message, F.video)
async def draft_video(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    if await _check_caption(message):
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["video"] = message.video.file_id
    if message.caption:
        d["text"] = message.caption
    await show_draft(message, d)


@dp.message(Form.message, F.animation)
async def draft_animation(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    if await _check_caption(message):
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["animation"] = message.animation.file_id
    if message.caption:
        d["text"] = message.caption
    await show_draft(message, d)


@dp.message(Form.message, F.audio)
async def draft_audio(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    if await _check_caption(message):
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["audio"] = message.audio.file_id
    if message.caption:
        d["text"] = message.caption
    await show_draft(message, d)


@dp.message(Form.message, F.document)
async def draft_document(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    if await _check_caption(message):
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["document"] = message.document.file_id
    if message.caption:
        d["text"] = message.caption
    await show_draft(message, d)


@dp.message(Form.message, F.sticker)
async def draft_sticker(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return
    d = drafts.setdefault(message.from_user.id, new_draft())
    d["sticker"] = message.sticker.file_id
    await show_draft(message, d)


@dp.message(Form.message, F.text)
async def draft_text(message: types.Message, state: FSMContext):
    if _check_punish(message):
        await state.clear()
        await message.answer(punish_message(message.from_user.id), reply_markup=MENU)
        return

    text = message.text

    if len(text) > MAX_TEXT_LEN:
        await message.answer(
            f"❌ Слишком длинный текст: <b>{len(text)}</b> символов.\n\n"
            f"Максимум: <b>{MAX_TEXT_LEN}</b> символов.\n"
            f"Сократите сообщение и отправьте снова."
        )
        return

    d = drafts.setdefault(message.from_user.id, new_draft())
    d["text"] = text
    await show_draft(message, d)


async def show_draft(message: types.Message, d: dict):
    header = "📎 <b>Черновик:</b>"
    if d.get("reply_to"):
        header += f"\n<i>Ответ для {d.get('reply_nick') or 'пользователя'}</i>"
    if d.get("text"):
        header += f"\n\n{d['text']}"

    try:
        if d.get("photo"):
            await message.answer_photo(d["photo"], caption=header + "\n\n<i>[фото]</i>",
                                       reply_markup=draft_kb(True))
        elif d.get("voice"):
            await message.answer_voice(d["voice"], caption=header + "\n\n<i>[голосовое]</i>",
                                       reply_markup=draft_kb(True))
        elif d.get("video"):
            await message.answer_video(d["video"], caption=header + "\n\n<i>[видео]</i>",
                                       reply_markup=draft_kb(True))
        elif d.get("video_note"):
            await message.answer(header + "\n\n<i>[кружок]</i>", reply_markup=draft_kb(True))
            await message.answer_video_note(d["video_note"])
        elif d.get("sticker"):
            await message.answer(header + "\n\n<i>[стикер]</i>", reply_markup=draft_kb(True))
            await message.answer_sticker(d["sticker"])
        elif d.get("document"):
            await message.answer_document(d["document"], caption=header + "\n\n<i>[документ]</i>",
                                          reply_markup=draft_kb(True))
        elif d.get("animation"):
            await message.answer_animation(d["animation"], caption=header + "\n\n<i>[GIF]</i>",
                                           reply_markup=draft_kb(True))
        elif d.get("audio"):
            await message.answer_audio(d["audio"], caption=header + "\n\n<i>[аудио]</i>",
                                       reply_markup=draft_kb(True))
        else:
            await message.answer(header, reply_markup=draft_kb(False))
    except Exception as e:
        await message.answer(f"⚠️ Ошибка превью: {e}\n\n{header}",
                             reply_markup=draft_kb(draft_has_content(d)))


@dp.callback_query(F.data == "delmedia")
async def delmedia(cb: types.CallbackQuery):
    d = drafts.get(cb.from_user.id)
    if d:
        clear_media(d)
    try:
        await cb.message.edit_reply_markup(reply_markup=draft_kb(False))
    except Exception:
        pass
    await cb.answer("Вложение убрано")


@dp.callback_query(F.data == "cancel")
async def cancel_cb(cb: types.CallbackQuery, state: FSMContext):
    drafts.pop(cb.from_user.id, None)
    await state.clear()
    await cb.message.answer("Отменено.", reply_markup=MENU)
    await cb.answer()


@dp.callback_query(F.data == "send")
async def send_draft(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id

    pun = punish_message(uid)
    if pun:
        drafts.pop(uid, None)
        await state.clear()
        await cb.message.answer(pun, reply_markup=MENU)
        await cb.answer()
        return

    left = check_cooldown(uid)
    if left > 0:
        await cb.answer(f"⏳ Подожди {left} сек", show_alert=True)
        return

    d = drafts.pop(uid, None)
    if not d or not draft_has_content(d):
        await cb.answer("Пустое сообщение", show_alert=True)
        return

    nick = get_nick(uid)
    if d.get("reply_to") and d.get("reply_nick"):
        header = f"<b>{nick} (ответ для {d['reply_nick']}):</b>"
    else:
        header = f"<b>{nick}:</b>"

    footer = f"\n\n🤖 @{bot_username}"
    body = f"{header}\n\n{d['text']}" if d.get("text") else header
    caption = body + footer
    kb = post_kb(uid, nick)

    await cb.answer("Отправляю…")

    try:
        if d.get("photo"):
            await bot.send_photo(CHANNEL_ID, d["photo"], caption=caption, reply_markup=kb)
        elif d.get("voice"):
            await bot.send_voice(CHANNEL_ID, d["voice"], caption=caption, reply_markup=kb)
        elif d.get("video"):
            await bot.send_video(CHANNEL_ID, d["video"], caption=caption, reply_markup=kb)
        elif d.get("video_note"):
            await bot.send_message(CHANNEL_ID, caption, reply_markup=kb)
            await bot.send_video_note(CHANNEL_ID, d["video_note"])
        elif d.get("sticker"):
            await bot.send_message(CHANNEL_ID, caption, reply_markup=kb)
            await bot.send_sticker(CHANNEL_ID, d["sticker"])
        elif d.get("document"):
            await bot.send_document(CHANNEL_ID, d["document"], caption=caption, reply_markup=kb)
        elif d.get("animation"):
            await bot.send_animation(CHANNEL_ID, d["animation"], caption=caption, reply_markup=kb)
        elif d.get("audio"):
            await bot.send_audio(CHANNEL_ID, d["audio"], caption=caption, reply_markup=kb)
        else:
            await bot.send_message(CHANNEL_ID, caption, reply_markup=kb)
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}")
        return

    set_cooldown(uid)

    ensure_user(uid)
    users[str(uid)]["last_text"] = d.get("text") or "[медиа]"
    save_users()

    user = cb.from_user
    log_text = (
        f"📥 <b>Новое сообщение</b>\n\n"
        f"👤 Имя в чате: <b>{nick}</b>\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Username: @{user.username if user.username else '—'}\n"
        f"👋 Имя TG: {user.full_name}\n"
        f"🎯 Кому: {'ответ для ' + str(d.get('reply_nick')) if d.get('reply_to') else 'в канал'}\n\n"
        f"💬 Текст:\n{d.get('text') or '[без текста]'}"
    )
    asyncio.create_task(send_log(log_text, kb=log_kb(uid)))

    await state.clear()
    await cb.message.answer(
        f"✅ Отправлено в канал!\n\n⏳ Следующее сообщение можно через {COOLDOWN_SEC} сек.",
        reply_markup=MENU
    )


async def start_reply(message: types.Message, target_id: int, state: FSMContext):
    uid = message.from_user.id
    target_nick = get_nick(target_id)

    d = new_draft()
    d["reply_to"] = target_id
    d["reply_nick"] = target_nick
    drafts[uid] = d

    await message.answer(
        f"✍️ <b>Ответ для {target_nick}</b>\n\n"
        f"Напишите текст (до {MAX_TEXT_LEN} символов) или отправьте медиа:",
        reply_markup=CANCEL
    )
    await state.set_state(Form.message)


# ==================== ЛОГ-БОТ: МОДЕРАЦИЯ ====================
@dp_log.callback_query(F.data.startswith("mod:"))
async def moderation(cb: types.CallbackQuery):
    try:
        if cb.from_user.id not in ADMIN_IDS:
            await cb.answer("❌ Нет доступа", show_alert=True)
            return

        parts = cb.data.split(":")
        if len(parts) != 3:
            await cb.answer("Ошибка кнопки", show_alert=True)
            return

        action = parts[1]
        target_id_str = parts[2]

        try:
            target_id = int(target_id_str)
        except ValueError:
            await cb.answer("Ошибка ID", show_alert=True)
            return

        target_nick = get_nick(target_id)

        if action == "mute":
            set_mute(target_id, "нарушение правил чата")
            await cb.answer(f"🔇 {target_nick} в муте на 40 минут", show_alert=True)
            try:
                await bot.send_message(
                    target_id,
                    "🔇 <b>Вы получили мут на 40 минут за нарушение правил чата.</b>\n\n"
                    "Писать в канал пока нельзя."
                )
            except Exception:
                pass
            try:
                await log_bot.send_message(
                    LOG_CHAT_ID,
                    f"🔇 <b>Мут выдан</b>\n\n"
                    f"Кому: <b>{target_nick}</b> (ID <code>{target_id}</code>)\n"
                    f"Срок: 40 минут"
                )
            except Exception:
                pass

        elif action == "ban":
            set_ban(target_id, "нарушение правил чата")
            await cb.answer(f"🔨 {target_nick} забанен", show_alert=True)
            try:
                await bot.send_message(
                    target_id,
                    "🚫 <b>Вы забанены за нарушение правил чата.</b>\n\n"
                    "Писать в канал больше нельзя."
                )
            except Exception:
                pass
            try:
                await log_bot.send_message(
                    LOG_CHAT_ID,
                    f"🔨 <b>Бан выдан</b>\n\n"
                    f"Кому: <b>{target_nick}</b> (ID <code>{target_id}</code>)\n"
                    f"Срок: навсегда"
                )
            except Exception:
                pass

        elif action == "unban":
            unban(target_id)
            await cb.answer(f"✅ {target_nick} разбанен / мут снят", show_alert=True)
            try:
                await bot.send_message(
                    target_id,
                    "✅ <b>Наказание снято.</b>\n\n"
                    "Вы снова можете писать в канал."
                )
            except Exception:
                pass
            try:
                await log_bot.send_message(
                    LOG_CHAT_ID,
                    f"✅ <b>Наказание снято</b>\n\n"
                    f"Кому: <b>{target_nick}</b> (ID <code>{target_id}</code>)"
                )
            except Exception:
                pass

        else:
            await cb.answer("Неизвестное действие", show_alert=True)

    except Exception as e:
        try:
            await cb.answer(f"❌ Ошибка: {e}", show_alert=True)
        except Exception:
            pass


# ==================== ЗАПУСК ====================
async def main():
    global bot_username
    load_users()
    for uid in list(users.keys()):
        try:
            ensure_user(int(uid))
        except ValueError:
            pass
    save_users()

    me = await bot.get_me()
    bot_username = me.username
    logging.warning(f"Бот @{bot_username} запущен. Юзеров: {len(users)}")

    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        dp_log.start_polling(log_bot, allowed_updates=["callback_query"]),
    )


if __name__ == "__main__":
    asyncio.run(main())