# -*- coding: utf-8 -*-
# =====================================================================
#  MajorityCASINO — Telegram-бот (казино, мины, бонусы, рефералы)
#  Библиотека: pyTelegramBotAPI
#  Работает на кастомном сервере Bot API
# =====================================================================

import telebot
from telebot import apihelper

SERVER_URL = "http://177.3.213.27:8081"
apihelper.API_URL = f"{SERVER_URL}/bot{{0}}/{{1}}"
apihelper.FILE_URL = f"{SERVER_URL}/file/bot{{0}}/{{1}}"

import json
import os
import random
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- ТОКЕН бота (переменная окружения BOT_TOKEN или токен по умолчанию) ---
TOKEN = os.environ.get("BOT_TOKEN", "1780253908:YG78GYA-LrLANjSjGqzmZXMxjeG8Nrdibid")

bot = telebot.TeleBot(TOKEN)

# ---------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"))
START_BALANCE = 100.0
DAILY_BONUS = 25.0
REF_BONUS = 100.0
MIN_BET = 1.0
MIN_WITHDRAW = 150.0

# Множители для режима "Минное поле" (количество мин -> число открытых кристаллов -> множитель)
MULTIPLIERS = {
    3: {1: 1.25, 2: 1.60, 3: 2.20, 4: 3.10, 5: 4.60, 6: 6.50},  # 6 кристаллов, до x6.50
    4: {1: 1.50, 2: 2.20, 3: 3.40, 4: 5.30, 5: 9.00},            # 5 кристаллов, до x9.00
}

LOCK = threading.Lock()
DATA = {"users": {}}
PENDING = {}   # user_id -> {"mode": "bet"/"deposit"/"withdraw", "amount": float}
GAMES = {}     # user_id -> состояние активной игры
BOT_USERNAME = None

# ---------------------------------------------------------------------
# ХРАНИЛИЩЕ ИГРОКОВ
# ---------------------------------------------------------------------
def load_data():
    global DATA
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                DATA = json.load(f)
        except Exception:
            DATA = {"users": {}}
    else:
        DATA = {"users": {}}
    if "users" not in DATA:
        DATA["users"] = {}

def save_data():
    with LOCK:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)

def fmt(n):
    if n == int(n):
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")

def get_user(user_id, username="", first_name=""):
    uid = str(user_id)
    if uid not in DATA["users"]:
        DATA["users"][uid] = {
            "id": user_id,
            "username": username,
            "first_name": first_name,
            "balance": START_BALANCE,
            "registered": time.time(),
            "last_bonus": 0,
            "referrer": None,
            "referrals": [],
            "games_played": 0,
            "games_won": 0,
            "games_lost": 0,
            "total_won": 0.0,
            "total_lost": 0.0,
        }
        save_data()
    else:
        u = DATA["users"][uid]
        if username and u["username"] != username:
            u["username"] = username
        if first_name and u["first_name"] != first_name:
            u["first_name"] = first_name
    return DATA["users"][uid]

def bot_username():
    global BOT_USERNAME
    if BOT_USERNAME is None:
        try:
            BOT_USERNAME = bot.get_me().username
        except Exception:
            BOT_USERNAME = "your_bot"
    return BOT_USERNAME

# ---------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------
def main_menu_markup():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🎮 Мини-игры", callback_data="menu_games"))
    kb.row(
        InlineKeyboardButton("🎁 Ежедневный бонус", callback_data="bonus"),
        InlineKeyboardButton("👥 Рефералы", callback_data="refs"),
    )
    kb.row(
        InlineKeyboardButton("🏷 Ввести промокод", callback_data="promo"),
        InlineKeyboardButton("🎒 Инвентарь", callback_data="inv"),
    )
    kb.row(
        InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        InlineKeyboardButton("🏆 Топы лидеров", callback_data="top"),
    )
    kb.row(
        InlineKeyboardButton("➕ Пополнить", callback_data="deposit"),
        InlineKeyboardButton("➖ Вывести", callback_data="withdraw"),
    )
    return kb

def back_markup(dest="menu_main"):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀ Назад", callback_data=dest))
    return kb

def field_markup(game):
    kb = InlineKeyboardMarkup()
    for row in range(3):
        buttons = []
        for col in range(3):
            idx = row * 3 + col
            if idx in game["opened"]:
                emoji = "💣" if idx in game["mine_pos"] else "💎"
                buttons.append(InlineKeyboardButton(emoji, callback_data=f"cell_{idx}"))
            else:
                buttons.append(InlineKeyboardButton("⬜", callback_data=f"cell_{idx}"))
        kb.row(*buttons)
    btns = []
    if game["opened_count"] >= 2:
        current_win = round(game["bet"] * game["mult"], 2)
        btns.append(InlineKeyboardButton(f"💰 Забрать куш: +{fmt(current_win)} ⭐ (x{fmt(game['mult'])})",
                                         callback_data="mine_cashout"))
    btns.append(InlineKeyboardButton("◀ В меню", callback_data="menu_main"))
    kb.row(*btns)
    return kb

# ---------------------------------------------------------------------
# СТАРТ / РЕГИСТРАЦИЯ
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# ОТЛАДКА СТАР/ЗВЁЗД — ловим сырые апдейты и форматы звёзд MechaGram
# ---------------------------------------------------------------------
LAST_UPDATE = {"ts": None}

def _plain(obj):
    if hasattr(obj, "__dict__"):
        return {k: _plain(v) for k, v in vars(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)

def dump_update_debug(message):
    try:
        d = _plain(message) if message else None
        if not d:
            return
        keys = " ".join(d.keys())
        dbg_line = json.dumps(d, ensure_ascii=False)[:4000]
        now = time.strftime("%d.%m %H:%M:%S")
        with open("/tmp/updates_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{now}] {dbg_line}\n")
        # интересующие поля для звёзд/платежей
        for marker in ("star", "Star", "invoice", "payment", "gift", "paid", "deeplink"):
            if marker in keys:
                print("STAR_MARKER:", marker, dbg_line[:1000])
    except Exception as e:
        print("debug dump error", e)

@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    uid = message.from_user.id
    if uid != 1600699268 and uid != 1780253260:
        bot.reply_to(message, "Недоступно")
        return
    try:
        lines = []
        if os.path.exists("/tmp/updates_debug.log"):
            with open("/tmp/updates_debug.log", "r", encoding="utf-8") as f:
                lines = f.readlines()[-20:]
        text = "".join(lines) if lines else "Лог пуст"
        text = text[:3500]
        bot.send_message(message.chat.id, text)
    except Exception as e:
        bot.send_message(message.chat.id, "Err: " + str(e))

@bot.message_handler(func=lambda m: m is not None and (m.content_type in ("successful_payment", "invoice", "withdrawal")
    or m.content_type is None
    or any(hasattr(m, a) for a in ("star", "stars", "gift", "gift_amount", "paid_star_count"))))
def handle_star_payment(message):
    if not getattr(message, "from_user", None):
        return
    uid = message.from_user.id
    user = get_user(uid)
    paid = 0.0
    if getattr(message, "content_type", None) == "successful_payment":
        sp = message.successful_payment
        paid = float(getattr(sp, "total_amount", 0) or getattr(sp, "amount", 0))
        if hasattr(sp, "total_amount") and sp.total_amount and getattr(sp, "currency", None) in (None, "XTR", "STARS"):
            paid = float(sp.total_amount)
    else:
        for attr in ("withdrawal", "star", "stars", "gift", "gift_amount", "paid_star_count"):
            v = getattr(message, attr, None)
            if isinstance(v, dict):
                paid = float(v.get("amount", 0) or v.get("total_amount", 0) or 0)
                if paid:
                    break
            elif isinstance(v, (int, float)) and v:
                paid = float(v)
                break
    paid = float(paid)
    if paid > 0:
        user["balance"] += paid
        save_data()
        bot.send_message(message.chat.id,
                         f"⭐ Получено звёзд: +{fmt(paid)}!\nТвой баланс: {fmt(user['balance'])} ⭐",
                         reply_markup=main_menu_markup())
    elif message.content_type in ("successful_payment", "invoice", "withdrawal"):
        bot.reply_to(message, "Не удалось распознать сумму звёзд. Нажми /debug")

# универсальный ловец: считаем ВСЕ апдейты, чтобы понять формат
@bot.message_handler(func=lambda m: True)
def log_all_update_types(message):
    already = hasattr(message, "_logged")
    if message and not already:
        try:
            setattr(message, "_logged", True)
            LAST_UPDATE["ts"] = time.time()
        except Exception:
            pass
        dump_update_debug(message)

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    ref_id = None
    args = message.text.split()
    if len(args) > 1:
        payload = args[1]
        if payload.startswith("ref"):
            try:
                ref_id = int(payload.replace("ref", ""))
            except ValueError:
                ref_id = None

    uname = message.from_user.username or ""
    fname = message.from_user.first_name or ""

    user = get_user(uid, uname, fname)

    if ref_id and ref_id != uid and user["referrer"] is None:
        ref_str = str(ref_id)
        if ref_str in DATA["users"]:
            user["referrer"] = ref_id
            if uid not in DATA["users"][ref_str]["referrals"]:
                DATA["users"][ref_str]["referrals"].append(uid)
            DATA["users"][ref_str]["balance"] += REF_BONUS
            save_data()
            try:
                bot.send_message(ref_id,
                                 f"👥 Твой друг @{uname or fname} зарегистрировался!\n"
                                 f"Тебе начислено +{fmt(REF_BONUS)} ⭐")
            except Exception:
                pass

    nick = f"@{uname}" if uname else (fname or str(uid))
    text = (f"Привет, {nick}!\n"
            f"Добро пожаловать в MajorityCASINO - открывай кейсы, играй в мини-игры "
            f"и выводи реальные NFT со склада в свой профиль\n\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐\n"
            f"Выбирай раздел в меню:")
    bot.send_message(message.chat.id, text, reply_markup=main_menu_markup())

# ---------------------------------------------------------------------
# ОБЩИЙ МЕНЮ-ОБРАБОТЧИК
# ---------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "menu_main")
def cb_menu_main(call):
    uid = call.from_user.id
    user = get_user(uid)
    nick = f"@{user['username']}" if user["username"] else (user["first_name"] or str(uid))
    text = (f"Привет, {nick}!\n"
            f"Добро пожаловать в MajorityCASINO - открывай кейсы, играй в мини-игры "
            f"и выводи реальные NFT со склада в свой профиль\n\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐\n"
            f"Выбирай раздел в меню:")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=main_menu_markup())
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=main_menu_markup())
    bot.answer_callback_query(call.id)

# ---------------------------------------------------------------------
# МИНИ-ИГРЫ
# ---------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "menu_games")
def cb_menu_games(call):
    uid = call.from_user.id
    user = get_user(uid)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💣 Минное поле", callback_data="menu_mines"))
    kb.add(InlineKeyboardButton("🎰 Кейсы (скоро)", callback_data="soon"))
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="menu_main"))
    text = (f"🎮 Мини-игры\n\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=kb)
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "soon")
def cb_soon(call):
    bot.answer_callback_query(call.id, "Скоро будет доступно!", show_alert=False)

@bot.callback_query_handler(func=lambda c: c.data == "menu_mines")
def cb_menu_mines(call):
    uid = call.from_user.id
    user = get_user(uid)
    PENDING[uid] = {"mode": "bet", "amount": None}
    text = (f"💣 Минное поле\n"
            f"Режим \"Минное поле\" (3x3)\n"
            f"Внимание: забрать куш можно только открыв от 2 кристаллов!\n\n"
            f"Выбирай количество мин (от 3 до 4)\n"
            f"3 мины: до x6.50\n"
            f"4 мины: до x9.00\n\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐\n"
            f"Напиши в чат сумму ставки:")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("menu_games"))
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=back_markup("menu_games"))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("mines_mine_"))
def cb_mines_choose(call):
    uid = call.from_user.id
    user = get_user(uid)
    mines = int(call.data.split("_")[2])

    pending = PENDING.get(uid)
    if not pending or pending["mode"] != "bet":
        bot.answer_callback_query(call.id, "Сначала введи сумму ставки!", show_alert=True)
        return
    amount = pending["amount"]

    if amount < MIN_BET:
        bot.answer_callback_query(call.id, f"Минимальная ставка {fmt(MIN_BET)} ⭐", show_alert=True)
        return
    if amount > user["balance"]:
        bot.answer_callback_query(call.id, "Недостаточно средств!", show_alert=True)
        return

    # создаём поле
    cells = list(range(9))
    mine_pos = set(random.sample(cells, mines))
    game = {
        "bet": amount,
        "mines": mines,
        "mine_pos": mine_pos,
        "opened": set(),
        "opened_count": 0,
        "mult": 1.0,
        "msg_id": None,
        "chat_id": call.message.chat.id,
        "finished": False,
    }
    GAMES[uid] = game
    del PENDING[uid]

    user["balance"] -= amount
    user["games_played"] += 1
    save_data()

    text = (f"Поле создано! (мин: {mines})\n"
            f"Ставка: {fmt(amount)} ⭐\n"
            f"Открой минимум 2 кристалла, чтобы забрать куш")
    try:
        msg = bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                    reply_markup=field_markup(game))
    except Exception:
        msg = bot.send_message(call.message.chat.id, text, reply_markup=field_markup(game))
    game["msg_id"] = msg.message_id
    bot.answer_callback_query(call.id, "Игра началась! Удачи 🍀")

# ---------------------------------------------------------------------
# ОТКРЫТИЕ КЛЕТОК
# ---------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("cell_"))
def cb_cell(call):
    uid = call.from_user.id
    idx = int(call.data.split("_")[1])
    game = GAMES.get(uid)
    if not game or game["finished"]:
        bot.answer_callback_query(call.id, "Игра не активна", show_alert=False)
        return
    if idx in game["opened"]:
        bot.answer_callback_query(call.id, "Клетка уже открыта", show_alert=False)
        return

    user = get_user(uid)

    if idx in game["mine_pos"]:
        # 💥 подорвался
        game["finished"] = True
        game["opened"].add(idx)
        user["games_lost"] += 1
        user["total_lost"] += game["bet"]
        save_data()

        mines = game["mines"]
        opened = game["opened_count"]
        total_safe = 9 - mines
        label = f"Открыто {opened}/{total_safe}"
        text = (f"💥 ТЫ ПОДОРВАЛСЯ\n\n"
                f"Мин на поле: {mines}\n"
                f"{label}\n"
                f"Ты потерял: {fmt(game['bet'])} ⭐\n"
                f"Твой баланс: {fmt(user['balance'])} ⭐")
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=field_markup(game))
        except Exception:
            pass
        bot.answer_callback_query(call.id, "ТЫ ПОДОРВАЛСЯ 💥", show_alert=True)
        return

    # 💎 кристалл найден
    game["opened"].add(idx)
    game["opened_count"] += 1
    game["mult"] = MULTIPLIERS[game["mines"]].get(game["opened_count"], 1.0)

    mines = game["mines"]
    opened = game["opened_count"]
    total_safe = 9 - mines

    if opened >= total_safe:
        # всё поле очищено — автоматический куш
        win = round(game["bet"] * game["mult"], 2)
        user["balance"] += win
        user["games_won"] += 1
        user["total_won"] += win
        game["finished"] = True
        save_data()
        text = (f"💎 Кристалл найден!\n"
                f"Мин на поле: {mines}\n"
                f"Открыто {opened}/{total_safe}\n"
                f"Множитель: x{fmt(game['mult'])}\n\n"
                f"🌸 ПОЛЕ ПОЛНОСТЬЮ ОЧИЩЕНО!\n"
                f"+{fmt(win)} ⭐\n"
                f"Твой баланс: {fmt(user['balance'])} ⭐")
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=main_menu_markup())
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"+{fmt(win)} ⭐", show_alert=True)
        return

    text = (f"💎 Кристалл найден!\n"
            f"Мин на поле: {mines}\n"
            f"Открыто {opened}/{total_safe}\n"
            f"Множитель: x{fmt(game['mult'])}\n"
            f"💰 Текущий выигрыш: +{fmt(round(game['bet'] * game['mult'], 2))} ⭐\n"
            f"Нажми «Забрать куш», чтобы получить его")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=field_markup(game))
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"Множитель x{fmt(game['mult'])}")

@bot.callback_query_handler(func=lambda c: c.data == "mine_cashout")
def cb_mine_cashout(call):
    uid = call.from_user.id
    game = GAMES.get(uid)
    if not game or game["finished"]:
        bot.answer_callback_query(call.id, "Игра не активна", show_alert=False)
        return
    if game["opened_count"] < 2:
        bot.answer_callback_query(call.id, "Нужно открыть минимум 2 кристалла!", show_alert=True)
        return

    user = get_user(uid)
    win = round(game["bet"] * game["mult"], 2)
    user["balance"] += win
    user["games_won"] += 1
    user["total_won"] += win
    game["finished"] = True
    save_data()

    text = (f"💰 Куш забран!\n"
            f"Ставка: {fmt(game['bet'])} ⭐\n"
            f"Множитель: x{fmt(game['mult'])}\n"
            f"Выигрыш: +{fmt(win)} ⭐\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=main_menu_markup())
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"Куш забран! +{fmt(win)} ⭐", show_alert=True)

# ---------------------------------------------------------------------
# ПРИЁМ ЧИСЛОВЫХ ВВОДОВ (ставка / пополнение / вывод)
# ---------------------------------------------------------------------
def is_number(text):
    t = text.strip().replace(",", ".")
    try:
        val = float(t)
        return val >= 0
    except ValueError:
        return False

@bot.message_handler(func=lambda m: m.text and m.from_user.id in PENDING and is_number(m.text))
def handle_amount(message):
    uid = message.from_user.id
    user = get_user(uid)
    val = float(message.text.strip().replace(",", "."))
    pending = PENDING[uid]

    if pending["mode"] == "bet":
        if val < MIN_BET:
            bot.send_message(message.chat.id, f"❌ Минимальная ставка {fmt(MIN_BET)} ⭐")
            return
        if val > user["balance"]:
            bot.send_message(message.chat.id, "❌ Недостаточно средств!")
            return
        pending["amount"] = round(val, 2)
        text = (f"✅ Ставка ({fmt(val)} ⭐) принята.\n"
                f"Выбери количество мин на поле (3x3):")
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("3 мины (до x6.50)", callback_data="mines_mine_3"),
            InlineKeyboardButton("4 мины (до x9.00)", callback_data="mines_mine_4"),
        )
        bot.send_message(message.chat.id, text, reply_markup=kb)

    elif pending["mode"] == "deposit":
        if val <= 0:
            bot.send_message(message.chat.id, "Введи сумму больше 0")
            return
        user["balance"] += round(val, 2)
        save_data()
        del PENDING[uid]
        bot.send_message(message.chat.id,
                         f"➕ Пополнение на {fmt(val)} ⭐ успешно!\n"
                         f"Твой баланс: {fmt(user['balance'])} ⭐",
                         reply_markup=main_menu_markup())

    elif pending["mode"] == "withdraw":
        if val < MIN_WITHDRAW:
            bot.send_message(message.chat.id,
                             f"❌ Минимальная сумма вывода: {fmt(MIN_WITHDRAW)} ⭐",
                             reply_markup=main_menu_markup())
            del PENDING[uid]
            return
        if val > user["balance"]:
            bot.send_message(message.chat.id, "❌ Недостаточно средств!",
                             reply_markup=main_menu_markup())
            del PENDING[uid]
            return
        user["balance"] -= round(val, 2)
        user["total_lost"] += round(val, 2)
        save_data()
        del PENDING[uid]
        bot.send_message(message.chat.id,
                         f"➖ Вывод {fmt(val)} ⭐ оформлен!\n"
                         f"Твой баланс: {fmt(user['balance'])} ⭐",
                         reply_markup=main_menu_markup())

@bot.message_handler(func=lambda m: m.text and m.from_user.id in PENDING and not is_number(m.text))
def handle_pending_not_number(message):
    bot.reply_to(message, "Введи сумму числом (например: 50 или 100.5)")

@bot.message_handler(func=lambda m: m.text and m.from_user.id not in PENDING and not m.text.startswith("/"))
def handle_unexpected_text(message):
    bot.reply_to(message, "Используй кнопки меню 👇", reply_markup=main_menu_markup())

# ---------------------------------------------------------------------
# РАЗДЕЛЫ МЕНЮ
# ---------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "bonus")
def cb_bonus(call):
    uid = call.from_user.id
    user = get_user(uid)
    now = time.time()
    cooldown = 24 * 3600
    if now - user["last_bonus"] < cooldown:
        wait = int(cooldown - (now - user["last_bonus"]))
        h, m = wait // 3600, (wait % 3600) // 60
        bot.answer_callback_query(call.id, "Бонус уже получен!", show_alert=True)
        try:
            bot.edit_message_text(
                f"🎁 Ежедневный бонус\n\n"
                f"Бонус уже получен ранее.\n"
                f"Следующий бонус через {h} ч {m} мин.",
                call.message.chat.id, call.message.message_id,
                reply_markup=back_markup("menu_main"))
        except Exception:
            pass
        return
    user["last_bonus"] = now
    user["balance"] += DAILY_BONUS
    save_data()
    bot.answer_callback_query(call.id, f"+{fmt(DAILY_BONUS)} ⭐", show_alert=True)
    try:
        bot.edit_message_text(
            f"🎁 Ежедневный бонус\n\n"
            f"+{fmt(DAILY_BONUS)} ⭐ получено!\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐\n\n"
            f"Возвращайся завтра за новым бонусом!",
            call.message.chat.id, call.message.message_id,
            reply_markup=back_markup("menu_main"))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "refs")
def cb_refs(call):
    uid = call.from_user.id
    user = get_user(uid)
    link = f"https://t.me/{bot_username()}?start=ref{uid}"
    ref_earned = len(user["referrals"]) * REF_BONUS
    text = (f"👥 Реферальная система\n\n"
            f"Приглашай друзей и получай +{fmt(REF_BONUS)} ⭐ за каждого!\n\n"
            f"Твоя ссылка:\n{link}\n\n"
            f"Приглашено друзей: {len(user['referrals'])}\n"
            f"Заработано на рефералах: {fmt(ref_earned)} ⭐")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("menu_main"))
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "promo")
def cb_promo(call):
    bot.answer_callback_query(call.id, "Промокоды пока не работают, скоро будут!", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data == "inv")
def cb_inv(call):
    uid = call.from_user.id
    user = get_user(uid)
    items = [
        ("⚔️ Керамический кинжал", "Обычный", 150),
        ("🔫 Пульсар-винтовка", "Редкий", 350),
        ("👾 Киберпанк-шлем", "Эпик", 500),
        ("🐉 Маска Дракона", "Легендарный", 1000),
        ("🎁 Кейс Majority", "Кейс", 250),
    ]
    text = (f"🎒 Инвентарь\n\n"
            f"Сейчас на складе:\n")
    for name, rarity, price in items:
        text += f"{name} · {rarity} · {fmt(price)} ⭐\n"
    text += (f"\nКейсы и продажа NFT скоро в разделе «Мини-игры».\n"
             f"Твой баланс: {fmt(user['balance'])} ⭐")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("menu_main"))
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "profile")
def cb_profile(call):
    uid = call.from_user.id
    user = get_user(uid)
    reg_date = datetime.fromtimestamp(user["registered"]).strftime("%d.%m.%Y %H:%M")
    nick = f"@{user['username']}" if user["username"] else (user["first_name"] or str(uid))
    total = user["games_played"]
    wr = 0.0
    if total > 0:
        wr = round(user["games_won"] / total * 100, 1)
    netto = user["total_won"] - user["total_lost"]
    sign = "+" if netto >= 0 else ""
    text = (f"👤 Профиль\n\n"
            f"Ник: {nick}\n"
            f"ID: {uid}\n"
            f"📅 Регистрация: {reg_date}\n\n"
            f"💰 Баланс: {fmt(user['balance'])} ⭐\n"
            f"🎮 Игр сыграно: {total}\n"
            f"🏅 Побед: {user['games_won']}\n"
            f"💀 Поражений: {user['games_lost']}\n"
            f"📊 Винрейт: {wr}%\n"
            f"📈 Чистый профит: {sign}{fmt(netto)} ⭐\n"
            f"👥 Рефералов: {len(user['referrals'])}")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("menu_main"))
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "top")
def cb_top(call):
    users = list(DATA["users"].values())
    users.sort(key=lambda u: u["balance"], reverse=True)
    text = "🏆 Топы лидеров\n\n"
    medals = ["🥇", "🥈", "🥉"]
    if not users:
        text += "Пока никого нет..."
    for i, u in enumerate(users[:10]):
        nick = f"@{u['username']}" if u["username"] else (u["first_name"] or f"ID {u['id']}")
        medal = medals[i] if i < 3 else f"{i + 1}."
        text += f"{medal} {nick} — {fmt(u['balance'])} ⭐\n"
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("menu_main"))
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "deposit")
def cb_deposit(call):
    uid = call.from_user.id
    PENDING[uid] = {"mode": "deposit", "amount": None}
    text = "➕ Пополнить\n\nСколько звёзд добавить на баланс? Введи сумму в чат:"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("50", callback_data="dep50"),
        InlineKeyboardButton("100", callback_data="dep100"),
        InlineKeyboardButton("250", callback_data="dep250"),
        InlineKeyboardButton("500", callback_data="dep500"),
    )
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="menu_main"))
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dep") and c.data[3:].isdigit())
def cb_dep_fast(call):
    uid = call.from_user.id
    val = float(call.data.replace("dep", ""))
    user = get_user(uid)
    user["balance"] += val
    PENDING.pop(uid, None)
    save_data()
    text = (f"➕ Пополнение на {fmt(val)} ⭐ успешно!\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=main_menu_markup())
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"+{fmt(val)} ⭐", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data == "withdraw")
def cb_withdraw(call):
    uid = call.from_user.id
    user = get_user(uid)
    PENDING[uid] = {"mode": "withdraw", "amount": None}
    text = (f"➖ Вывести\n"
            f"Минимальная сумма вывода: {fmt(MIN_WITHDRAW)} ⭐\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐\n\n"
            f"Введи сумму для вывода в чат:")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("menu_main"))
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=back_markup("menu_main"))
    bot.answer_callback_query(call.id)

# ---------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        if self.path == "/diag":
            out = []
            out.append("now=%s" % time.strftime("%d.%m %H:%M:%S"))
            out.append("last_update=%s" % (time.strftime("%d.%m %H:%M:%S", time.localtime(LAST_UPDATE["ts"])) if LAST_UPDATE["ts"] else "none"))
            try:
                import urllib.request
                req = urllib.request.Request(SERVER_URL + "/bot" + TOKEN + "/getMe", timeout=10)
                with urllib.request.urlopen(req) as resp:
                    out.append("getMe_http=%d" % resp.status)
                    out.append("getMe_body=%s" % resp.read(300).decode("utf-8", "replace"))
            except Exception as e:
                out.append("getMe_err=%s" % type(e).__name__ + ":" + str(e)[:200])
            body = ("\n".join(out)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
    except Exception:
        pass

if __name__ == "__main__":
    load_data()
    thread = threading.Thread(target=start_health_server, daemon=True)
    thread.start()
    print("Bot started. Server:", SERVER_URL)
    bot.infinity_polling(none_stop=True)