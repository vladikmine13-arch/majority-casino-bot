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
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
try:
    from telebot.handler_backends import ContinueHandling
except ImportError:
    ContinueHandling = None

# --- ТОКЕН бота (переменная окружения BOT_TOKEN или токен по умолчанию) ---
TOKEN = os.environ.get("BOT_TOKEN", "1780253908:YG78GYA-LrLANjSjGqzmZXMxjeG8Nrdibid")

bot = telebot.TeleBot(TOKEN, threaded=False)

# --- ЛОГ в /tmp/bot.log (доступен через /diag) ---
def _setup_log():
    try:
        import logging
        fh = logging.FileHandler("/tmp/bot.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(fh)
        root.setLevel(logging.INFO)
        logging.getLogger("TeleBot").addHandler(fh)
        print("logging to /tmp/bot.log")
    except Exception as e:
        print("log setup error", e)

try:
    import collections
    LOG_RING = collections.deque(maxlen=60)
    RING_LOCK = threading.Lock()

    def _log_line(s):
        with RING_LOCK:
            LOG_RING.append("[%s] %s" % (time.strftime("%d.%m %H:%M:%S"), str(s)[:500]))
        try:
            with open("/tmp/updates.log", "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%d.%m %H:%M:%S"), str(s)[:500]))
        except Exception as e:
            with RING_LOCK:
                LOG_RING.append("FILEWRITE_ERR: " + str(e)[:200])

    def _msg_summary(m):
        if isinstance(m, dict):
            return "MSG(dict) keys=%s data=%r" % (list(m.keys())[:20], str(m)[:300])
        chat = getattr(m, "chat", None)
        fu = getattr(m, "from_user", None)
        return "MSG ct=%s chat=%s from=%s text=%r" % (
            getattr(m, "content_type", None), getattr(chat, "id", None) if chat else None,
            getattr(fu, "id", None) if fu else None, (getattr(m, "text", None) or "")[:80])

    def _update_summary(u):
        if isinstance(u, (list, tuple)):
            return " | ".join(_msg_summary(x) for x in u[:8])
        if isinstance(u, dict):
            if "message" in u:
                return _msg_summary(u["message"])
            if "callback_query" in u:
                cq = u["callback_query"] or {}
                cm = cq.get("message") or {}
                return "CB chat=%s from=%s data=%r" % (
                    (cm.get("chat") or {}).get("id"), (cq.get("from") or {}).get("id"),
                    (cq.get("data") or "")[:80])
            return "RAW dict keys=%s data=%r" % (list(u.keys())[:20], str(u)[:300])
        m = getattr(u, "message", None) or getattr(u, "edited_message", None) or getattr(u, "business_message", None)
        if m is not None:
            return _msg_summary(m)
        cq = getattr(u, "callback_query", None)
        if cq is not None:
            cm = getattr(cq, "message", None)
            return "CB chat=%s from=%s data=%r" % (
                getattr(cm, "chat", None).id if cm and getattr(cm, "chat", None) else None,
                getattr(getattr(cq, "from_user", None), "id", None),
                (getattr(cq, "data", None) or "")[:80])
        return "UP %s" % type(u).__name__

    def _on_update(u):
        LAST_UPDATE["ts"] = time.time()
        try:
            _log_line(_update_summary(u))
        except Exception as e:
            _log_line("ONUPDATE_ERR: %r %s" % (e, traceback.format_exc()[-300:]))
    bot.set_update_listener(_on_update)
except Exception:
    pass

# ---------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"))
START_BALANCE = 100.0
DAILY_BONUS = 25.0
REF_BONUS = 100.0
MIN_BET = 1.0
MIN_WITHDRAW = 150.0

# Комиссия казино (владельцу отчисляется 10% с пополнения И с вывода)
DEPOSIT_FEE = 0.10
WITHDRAW_FEE = 0.10
ADMIN_IDS = {1600699268, 1780253260}

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
    if "payouts" not in DATA:
        DATA["payouts"] = []   # очередь выплат: {"id","uid","amount","fee","total","ts"}
    if "stats" not in DATA:
        DATA["stats"] = {}     # {"deposited","withdrawals","deposit_fee","withdraw_fee"}
    DATA["stats"].setdefault("deposited", 0.0)
    DATA["stats"].setdefault("withdrawals", 0.0)
    DATA["stats"].setdefault("deposit_fee", 0.0)
    DATA["stats"].setdefault("withdraw_fee", 0.0)

def _stats(**kw):
    st = DATA.setdefault("stats", {})
    st.setdefault("deposited", 0.0)
    st.setdefault("withdrawals", 0.0)
    st.setdefault("deposit_fee", 0.0)
    st.setdefault("withdraw_fee", 0.0)
    for k, v in kw.items():
        st[k] = round(st.get(k, 0.0) + v, 2)

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
        btns.append(InlineKeyboardButton(f"💰 Забрать куш +{fmt(current_win)} ⭐",
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

@bot.message_handler(commands=["payouts"])
def cmd_payouts(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    pays = [p for p in DATA.get("payouts", []) if not p.get("done")]
    if not pays:
        bot.send_message(message.chat.id, "Очередь на выплату пуста.")
        return
    lines = []
    for p in pays:
        ts = datetime.fromtimestamp(p["ts"]).strftime("%d.%m %H:%M")
        lines.append(f"#{p['id']} uid={p['uid']} → {fmt(p['amount'])} ⭐ (fee {fmt(p['fee'])} ⭐) [{ts}]")
    bot.send_message(message.chat.id, "📋 Очередь выплат:\n\n" + "\n".join(lines)[:3500])

@bot.message_handler(commands=["paydone"])
def cmd_paydone(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /paydone <номер>")
        return
    try:
        pid = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "Номер должен быть числом")
        return
    for p in DATA.get("payouts", []):
        if p["id"] == pid and not p.get("done"):
            p["done"] = True
            save_data()
            _log_line("PAYOUT_DONE id=%s uid=%s amt=%s" % (pid, p["uid"], fmt(p["amount"])))
            bot.send_message(message.chat.id, f"✅ Выплата #{pid} отмечена отправленной (+{fmt(p['amount'])} ⭐ → uid={p['uid']})")
            return
    bot.send_message(message.chat.id, "Выплата с таким номером не найдена.")

@bot.message_handler(commands=["revenue"])
def cmd_revenue(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    st = DATA.get("stats", {})
    text = (f"💰 Доход казино\n\n"
            f"Пополнено игроками: {fmt(st.get('deposited', 0))} ⭐\n"
            f"Комиссия с пополнений (10%): {fmt(st.get('deposit_fee', 0))} ⭐\n"
            f"Выведено (заявки): {fmt(st.get('withdrawals', 0))} ⭐\n"
            f"Комиссия с выводов (10%): {fmt(st.get('withdraw_fee', 0))} ⭐\n\n"
            f"Итого комиссии: {fmt(st.get('deposit_fee', 0) + st.get('withdraw_fee', 0))} ⭐\n"
            f"В очереди на выплату: {sum(1 for p in DATA.get('payouts', []) if not p.get('done'))}")
    bot.send_message(message.chat.id, text)

def _star_amount(message):
    if getattr(message, "content_type", None) == "successful_payment":
        sp = message.successful_payment
        v = getattr(sp, "total_amount", None) or getattr(sp, "amount", None)
        return float(v) if v else None
    psc = getattr(message, "paid_star_count", None)
    if isinstance(psc, (int, float)) and psc and int(psc) > 0:
        return float(psc)
    for attr in ("gift", "gift_amount", "star", "stars", "receipt", "withdrawal"):
        v = getattr(message, attr, None)
        if isinstance(v, dict):
            amt = (v.get("amount") or v.get("total_amount") or v.get("stars") or v.get("value") or v.get("paid_star_count"))
            if amt:
                return float(amt)
        elif isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None

@bot.message_handler(func=lambda m: m is not None and (m.content_type in ("successful_payment", "invoice", "withdrawal")
    or m.content_type is None
    or any(hasattr(m, a) for a in ("star", "stars", "gift", "gift_amount", "paid_star_count"))))
def handle_star_payment(message):
    if not getattr(message, "from_user", None):
        return ContinueHandling()
    uid = message.from_user.id
    user = get_user(uid)
    paid = _star_amount(message)
    if paid and paid > 0:
        credit = round(paid * (1 - DEPOSIT_FEE), 2)
        fee = round(paid * DEPOSIT_FEE, 2)
        user["balance"] += credit
        _stats(deposited=paid, deposit_fee=fee)
        save_data()
        _log_line("STAR_TX uid=%s paid=%s fee=%s credit=%s" % (uid, fmt(paid), fmt(fee), fmt(credit)))
        try:
            bot.send_message(message.chat.id,
                             f"⭐ Оплата получена!\n\n"
                             f"Оплачено звёзд: {fmt(paid)} ⭐\n"
                             f"Комиссия казино (10%): -{fmt(fee)} ⭐\n"
                             f"На игровой баланс зачислено: +{fmt(credit)} ⭐\n\n"
                             f"Твой баланс: {fmt(user['balance'])} ⭐",
                             reply_markup=main_menu_markup())
        except Exception as e:
            _log_line("STAR_REPLY_ERR: %s" % str(e)[:200])
        return ContinueHandling()
    if getattr(message, "content_type", None) in ("successful_payment", "invoice", "withdrawal", None):
        _log_line("STAR_UNKNOWN ct=%s keys=%s" % (getattr(message, "content_type", None),
                  " ".join(k for k in vars(message) if "star" in k.lower() or "paid" in k.lower() or "gift" in k.lower())))
    return ContinueHandling()

# отвечаем на pre_checkout_query — клиент MechaGram может сам запускать оплату звёздами
@bot.pre_checkout_query_handler(func=lambda q: True)
def handle_pre_checkout(query):
    try:
        uid = getattr(getattr(query, "from_user", None), "id", None)
        amount = getattr(query, "total_amount", None)
        currency = getattr(query, "currency", "") or ""
        _log_line("PRECHECKOUT uid=%s amount=%s cur=%s qid=%s" % (uid, amount, currency, query.id))
        try:
            dump_update_debug(query)
        except Exception:
            pass
        try:
            bot.answer_pre_checkout_query(query.id, ok=True)
            _log_line("PRECHECKOUT_OK")
        except Exception as e:
            _log_line("PRECHECKOUT_ERR: %s" % str(e)[:200])
    except Exception as e:
        _log_line("PRECHECKOUT_HANDLER_ERR: %s" % str(e)[:200])

# универсальный ловец: считаем ВСЕ апдейты, чтобы понять формат
@bot.message_handler(func=lambda m: True)
def log_all_update_types(message):
    already = hasattr(message, "_logged")
    if message and not already:
        try:
            setattr(message, "_logged", True)
            LAST_UPDATE["ts"] = time.time()
            _log_line("MSGHANDLER fired ct=%s chat=%s text=%r" % (
                getattr(message, "content_type", None),
                getattr(getattr(message, "chat", None), "id", None),
                (getattr(message, "text", None) or "")[:80]))
        except Exception:
            pass
        dump_update_debug(message)
    if ContinueHandling is None:
        return None
    return ContinueHandling()

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
    try:
        bot.send_message(message.chat.id, text, reply_markup=main_menu_markup())
        _log_line("REPLIED /start chat=%s" % message.chat.id)
    except Exception as e:
        _log_line("CMD_START_ERR: %s" % str(e)[:200])
        import logging
        logging.getLogger("TeleBot").exception("cmd_start send failed")
        try:
            bot.send_message(message.chat.id, "Ошибка обработки: " + str(e)[:200])
        except Exception:
            pass

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
        pending["amount"] = round(val, 2)
        credit = round(val * (1 - DEPOSIT_FEE), 2)
        text = (f"✅ Заявка на пополнение: {fmt(val)} ⭐\n\n"
                f"1. Нажми в клиенте кнопку «Оплатить звёздами» (⭐)\n"
                f"2. Отправь ровно {fmt(val)} ⭐\n"
                f"3. Подтверди оплату — звёзды зачислятся автоматически\n\n"
                f"Минус 10% комиссия: на баланс упадёт {fmt(credit)} ⭐.\n"
                f"Если ничего не произошло — проверь раздел «Профиль».")
        bot.send_message(message.chat.id, text, reply_markup=back_markup("menu_main"))
        del PENDING[uid]

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
        payout = round(val * (1 - WITHDRAW_FEE), 2)
        fee = round(val - payout, 2)
        user["balance"] -= val
        _stats(withdrawals=val, withdraw_fee=fee)
        payouts = DATA.setdefault("payouts", [])
        next_id = (max((p["id"] for p in payouts), default=0) + 1)
        payouts.append({"id": next_id, "uid": uid, "amount": payout, "fee": fee,
                        "total": val, "ts": time.time(), "done": False})
        save_data()
        del PENDING[uid]
        bot.send_message(message.chat.id,
                         f"➖ Заявка на вывод создана!\n\n"
                         f"Запрошено: {fmt(val)} ⭐\n"
                         f"Комиссия казино (10%): -{fmt(fee)} ⭐\n"
                         f"На твой реальный Telegram-баланс будет отправлено: {fmt(payout)} ⭐\n\n"
                         f"Номер заявки: #{next_id}\n"
                         f"Статус: в очереди на выплату",
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
    text = (f"➕ Пополнение звёздами\n\n"
            f"1. Напиши в чат, сколько звёзд хочешь завести\n"
            f"2. Бот покажет, сколько надо отправить\n"
            f"3. Отправь звёзды боту через кнопку ⭐ в чате — зачислится автоматически\n\n"
            f"💸 Комиссия казино: 10% с каждого пополнения.\n"
            f"Пример: отправляешь 100 ⭐ → на баланс падает 90 ⭐.")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=back_markup("menu_main"))
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=back_markup("menu_main"))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("dep") and c.data[3:].isdigit())
def cb_dep_fast(call):
    val = float(call.data.replace("dep", ""))
    user = get_user(call.from_user.id)
    credit = round(val * (1 - DEPOSIT_FEE), 2)
    text = (f"➕ {fmt(val)} ⭐ звёзд\n\n"
            f"Пришли эту сумму боту через отправку звёзд (кнопка ⭐ в чате).\n"
            f"На баланс будет зачислено {fmt(credit)} ⭐ (комиссия 10%).")
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_markup("deposit"))
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=back_markup("deposit"))
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "withdraw")
def cb_withdraw(call):
    uid = call.from_user.id
    user = get_user(uid)
    PENDING[uid] = {"mode": "withdraw", "amount": None}
    text = (f"➖ Вывести\n"
            f"Минимальная сумма вывода: {fmt(MIN_WITHDRAW)} ⭐\n"
            f"Твой баланс: {fmt(user['balance'])} ⭐\n\n"
            f"💸 Комиссия казино при выводе: 10%.\n"
            f"Пример: выводишь 100 ⭐ → на реальный баланс уйдёт 90 ⭐.\n\n"
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
                req = urllib.request.Request(SERVER_URL + "/bot" + TOKEN + "/getMe")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    out.append("getMe_http=%d" % resp.status)
                    out.append("getMe_body=%s" % resp.read(300).decode("utf-8", "replace"))
            except Exception as e:
                out.append("getMe_err=%s" % type(e).__name__ + ":" + str(e)[:200])
            st = DATA.get("stats", {})
            out.append("payouts_pending=%d payouts_done=%d revenue=%s" % (
                sum(1 for p in DATA.get("payouts", []) if not p.get("done")),
                sum(1 for p in DATA.get("payouts", []) if p.get("done")),
                fmt(st.get("deposit_fee", 0) + st.get("withdraw_fee", 0))))
            with RING_LOCK:
                ring_lines = list(LOG_RING)[-35:]
            if ring_lines:
                out.append("--- RING LOG ---")
                out.extend(l[:300] for l in ring_lines)
            else:
                out.append("RING EMPTY")
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
    _setup_log()
    thread = threading.Thread(target=start_health_server, daemon=True)
    thread.start()
    print("Bot started. Server:", SERVER_URL)
    import telebot.apihelper as _ah
    while True:
        try:
            updates = bot.get_updates(offset=(bot.last_update_id + 1), timeout=15)
            if updates:
                _log_line("POLL got=%d first_id=%d" % (len(updates), updates[0].update_id))
                try:
                    m0 = updates[0].message
                    if m0 is not None:
                        star_attr = [a for a in ("star", "stars", "gift", "gift_amount", "paid_star_count") if hasattr(m0, a)]
                        _log_line("DIAG n_handlers=%d ct=%s star_hasattr=%s text=%r" % (
                            len(bot.message_handlers), getattr(m0, "content_type", None),
                            star_attr, (getattr(m0, "text", None) or "")[:50]))
                except Exception as e:
                    _log_line("DIAG_ERR: %s %s" % (type(e).__name__, str(e)[:200]))
                bot.process_new_updates(updates)
                _log_line("PROCESSED n=%d" % len(updates))
            else:
                _log_line("POLL empty")
        except _ah.ApiTelegramException as e:
            code = getattr(e, "error_code", None)
            _log_line("API error: %s %s" % (code, str(e)[:200]))
            time.sleep(10 if code == 409 else 5)
        except Exception as e:
            _log_line("POLLERR: %s %s" % (type(e).__name__, str(e)[:300]))
            traceback.print_exc()
            time.sleep(5)