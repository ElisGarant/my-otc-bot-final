import os
import sqlite3
import json
import asyncio
import logging
import re
import random
import html
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Tuple
from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ChatPermissions, ChatInviteLink,
    Message, ChatMember
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# ===================== НАСТРОЙКИ =====================
CONFIG_PATH = os.environ.get("BOT_CONFIG_PATH", "config.json")
DEFAULT_CONFIG = {
    "token": "",
    "db_name": "bot_data.db",
    "owner_ids": [],
    "admin_ids": [],
    "required_subscription_chat": "",  # @username или chat_id
    "required_subscription_link": "",  # при необходимости отдельная ссылка-приглашение
    "allowed_link_domains": ["t.me", "ton.org", "telegram.org"],
    "settings_defaults": {
        "vip_threshold_amount": "500",
        "vip_threshold_level": "Эксперт",
        "vip_invite_link": "",
        "moderation_enabled": "1",
        "filter_links": "1",
        "filter_badwords": "1",
        "filter_spam": "1",
        "xp_per_message": "1",
        "ref_bonus_coins": "50",
        "ref_bonus_xp": "10",
        "deal_xp_percent": "5",
        "default_commission": "0.02",
        "scam_action": "mute",
        "premium_emoji_id": "",
        "premium_emoji_base_xp": "2",
        "premium_emoji_growth_step": "1",
        "premium_emoji_max_xp": "10",
        "xp_bar_filled_emoji_id": "5366058919019980842",
        "xp_bar_empty_emoji_id": "5368525295399773078",
        "xp_bar_length": "10",
        "commission_currency": "coins"
    }
}

def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def load_config(path: str) -> dict:
    config = DEFAULT_CONFIG
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = deep_merge(DEFAULT_CONFIG, loaded if isinstance(loaded, dict) else {})
        except Exception as e:
            logging.warning(f"Не удалось прочитать config.json, используются значения по умолчанию: {e}")
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    return config

CONFIG = load_config(CONFIG_PATH)

def parse_ids(value) -> list[int]:
    if isinstance(value, str):
        return [int(x.strip()) for x in value.split(",") if x.strip()]
    if isinstance(value, list):
        return [int(x) for x in value if str(x).strip()]
    return []

TOKEN = os.environ.get("TELEGRAM_TOKEN") or CONFIG.get("token")
if not TOKEN:
    raise ValueError("Не задан TELEGRAM_TOKEN или token в config.json")

admins_env = os.environ.get("ADMIN_IDS", "")
admins_from_env = [int(x.strip()) for x in admins_env.split(",") if x.strip()] if admins_env else []
owners_env = os.environ.get("OWNER_IDS", "")
owners_from_env = [int(x.strip()) for x in owners_env.split(",") if x.strip()] if owners_env else []
admins_from_config = parse_ids(CONFIG.get("admin_ids", []))
owners_from_config = parse_ids(CONFIG.get("owner_ids", []))
OWNER_IDS = sorted(set(owners_from_env + owners_from_config))
INITIAL_ADMINS = sorted(set(admins_from_env + admins_from_config))
DB_NAME = CONFIG.get("db_name", "bot_data.db")
REQUIRED_SUBSCRIPTION_CHAT = str(CONFIG.get("required_subscription_chat", "")).strip()
REQUIRED_SUBSCRIPTION_LINK = str(CONFIG.get("required_subscription_link", "")).strip()
ALLOWED_LINK_DOMAINS = [str(x).strip().lower() for x in CONFIG.get("allowed_link_domains", []) if str(x).strip()]

# ===================== ИНИЦИАЛИЗАЦИЯ =====================
storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Пользователи
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        level TEXT DEFAULT 'Новичок',
        xp INTEGER DEFAULT 0,
        total_xp INTEGER DEFAULT 0,
        bio TEXT DEFAULT '',
        custom_fields TEXT DEFAULT '{}',
        join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        coins INTEGER DEFAULT 0,
        warnings_count INTEGER DEFAULT 0,
        is_muted BOOLEAN DEFAULT 0,
        mute_until TIMESTAMP,
        referred_by INTEGER,
        referral_count INTEGER DEFAULT 0,
        active_prefix TEXT,
        pending_prefix TEXT,
        commission_discount REAL DEFAULT 0.0,
        no_queue BOOLEAN DEFAULT 0
    )''')
    cur.execute("PRAGMA table_info(users)")
    user_columns = [row[1] for row in cur.fetchall()]
    if "referral_rewarded" not in user_columns:
        cur.execute("ALTER TABLE users ADD COLUMN referral_rewarded BOOLEAN DEFAULT 0")

    # Гаранты
    cur.execute('''CREATE TABLE IF NOT EXISTS guarantors (
        user_id INTEGER PRIMARY KEY,
        level INTEGER DEFAULT 1,
        is_active BOOLEAN DEFAULT 1,
        current_deal_id INTEGER,
        total_deals INTEGER DEFAULT 0,
        rating REAL DEFAULT 0.0,
        feedback_count INTEGER DEFAULT 0,
        vip_chat_id INTEGER,
        vip_invite_link TEXT,
        commission_rate REAL DEFAULT 0.02,
        max_deal_amount REAL DEFAULT 0,
        max_concurrent_deals INTEGER DEFAULT 1
    )''')
    cur.execute("PRAGMA table_info(guarantors)")
    guarantor_columns = [row[1] for row in cur.fetchall()]
    if "max_deal_amount" not in guarantor_columns:
        cur.execute("ALTER TABLE guarantors ADD COLUMN max_deal_amount REAL DEFAULT 0")
    if "max_concurrent_deals" not in guarantor_columns:
        cur.execute("ALTER TABLE guarantors ADD COLUMN max_concurrent_deals INTEGER DEFAULT 1")
    if "vip_invite_link" not in guarantor_columns:
        cur.execute("ALTER TABLE guarantors ADD COLUMN vip_invite_link TEXT")

    cur.execute('''CREATE TABLE IF NOT EXISTS admin_roles (
        user_id INTEGER PRIMARY KEY,
        rank INTEGER CHECK(rank IN (2, 3)) DEFAULT 3
    )''')

    # Сделки
    cur.execute('''CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        guarantor_id INTEGER,
        amount REAL,
        description TEXT,
        status TEXT DEFAULT 'pending',
        vip_chat_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        is_vip BOOLEAN DEFAULT 0,
        commission REAL DEFAULT 0.0,
        priority BOOLEAN DEFAULT 0
    )''')

    # Заявки на сделку (ожидание назначения гаранта)
    cur.execute('''CREATE TABLE IF NOT EXISTS deal_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        amount REAL,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        priority BOOLEAN DEFAULT 0,
        status TEXT DEFAULT 'waiting'
    )''')

    # Магазин товаров
    cur.execute('''CREATE TABLE IF NOT EXISTS store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        description TEXT,
        price INTEGER,
        type TEXT,
        value TEXT,
        available BOOLEAN DEFAULT 1
    )''')

    # Приобретённые товары пользователей
    cur.execute('''CREATE TABLE IF NOT EXISTS user_items (
        user_id INTEGER,
        item_id INTEGER,
        applied BOOLEAN DEFAULT 0,
        PRIMARY KEY (user_id, item_id)
    )''')

    # Заявки на префиксы (ожидание одобрения)
    cur.execute('''CREATE TABLE IF NOT EXISTS prefix_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        prefix TEXT,
        status TEXT DEFAULT 'pending',
        buyer_comment TEXT DEFAULT '',
        admin_comment TEXT DEFAULT ''
    )''')
    cur.execute("PRAGMA table_info(prefix_requests)")
    prefix_columns = [row[1] for row in cur.fetchall()]
    if "buyer_comment" not in prefix_columns:
        cur.execute("ALTER TABLE prefix_requests ADD COLUMN buyer_comment TEXT DEFAULT ''")
    if "admin_comment" not in prefix_columns:
        cur.execute("ALTER TABLE prefix_requests ADD COLUMN admin_comment TEXT DEFAULT ''")

    # Отзывы о гарантах
    cur.execute('''CREATE TABLE IF NOT EXISTS feedbacks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER,
        to_user INTEGER,
        rating INTEGER CHECK(rating BETWEEN 1 AND 5),
        text TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        deleted BOOLEAN DEFAULT 0
    )''')

    # Лог модерации с причинами (бан/мут/пред)
    cur.execute('''CREATE TABLE IF NOT EXISTS moderation_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action_type TEXT,
        target_user INTEGER,
        admin_user INTEGER,
        reason TEXT,
        duration_seconds INTEGER DEFAULT 0,
        chat_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Прогресс XP за premium emoji
    cur.execute('''CREATE TABLE IF NOT EXISTS premium_emoji_progress (
        user_id INTEGER PRIMARY KEY,
        usage_count INTEGER DEFAULT 0,
        last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Подтверждения завершения сделки от сторон
    cur.execute('''CREATE TABLE IF NOT EXISTS deal_completion_confirmations (
        deal_id INTEGER,
        user_id INTEGER,
        confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (deal_id, user_id)
    )''')

    # Сообщения о подтверждении для последующей очистки
    cur.execute('''CREATE TABLE IF NOT EXISTS deal_completion_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER
    )''')

    # Временный доступ пользователей в VIP-чат сделки
    cur.execute('''CREATE TABLE IF NOT EXISTS vip_chat_access (
        deal_id INTEGER,
        chat_id INTEGER,
        user_id INTEGER,
        expires_at TIMESTAMP,
        PRIMARY KEY (deal_id, chat_id, user_id)
    )''')

    # Скам-база (основная)
    cur.execute('''CREATE TABLE IF NOT EXISTS scammers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        evidence TEXT,
        added_by INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Заявки на скамеров (от пользователей)
    cur.execute('''CREATE TABLE IF NOT EXISTS scam_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reported_username TEXT NOT NULL,
        evidence TEXT,
        reporter_id INTEGER,
        status TEXT DEFAULT 'pending',
        admin_comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Внешние скам-боты (для интеграции)
    cur.execute('''CREATE TABLE IF NOT EXISTS external_bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        username TEXT,
        api_url TEXT,
        is_active BOOLEAN DEFAULT 1
    )''')

    # Уровни пользователей
    cur.execute('''CREATE TABLE IF NOT EXISTS levels (
        name TEXT PRIMARY KEY,
        xp_required INTEGER,
        bonus_coins INTEGER DEFAULT 0,
        commission_rate REAL DEFAULT 0.02,
        no_queue BOOLEAN DEFAULT 0
    )''')

    # Настройки (ключ-значение)
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    # Чёрный список слов
    cur.execute('''CREATE TABLE IF NOT EXISTS blacklist (
        word TEXT PRIMARY KEY
    )''')

    # Добавляем уровни по умолчанию, если их нет
    cur.execute("SELECT COUNT(*) FROM levels")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO levels (name, xp_required, bonus_coins, commission_rate, no_queue) VALUES (?, ?, ?, ?, ?)",
            [
                ('Новичок', 0, 0, 0.02, 0),
                ('Продвинутый', 100, 50, 0.015, 0),
                ('Эксперт', 300, 150, 0.01, 1),
                ('Легенда', 600, 300, 0.005, 1)
            ]
        )

    settings_defaults = dict(CONFIG.get("settings_defaults", {}))
    settings_defaults["admin_ids"] = settings_defaults.get("admin_ids") or ','.join(map(str, INITIAL_ADMINS))
    for key, value in settings_defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    conn.commit()
    conn.close()

init_db()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_admin(user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    if user_id in INITIAL_ADMINS:
        return True
    admins_str = get_setting('admin_ids')
    if admins_str:
        admin_list = [int(x.strip()) for x in admins_str.split(',') if x.strip()]
        if user_id in admin_list:
            return True
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT rank FROM admin_roles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return True
    return False

def get_admin_rank(user_id: int) -> Optional[int]:
    if user_id in OWNER_IDS:
        return 1
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT rank FROM admin_roles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return int(row[0])
    admins_str = get_setting('admin_ids') or ""
    if user_id in INITIAL_ADMINS or (admins_str and user_id in [int(x.strip()) for x in admins_str.split(',') if x.strip()]):
        conn.close()
        return 2
    conn.close()
    return None

def has_admin_permission(user_id: int, min_rank: int) -> bool:
    # rank: 1 > 2 > 3 (меньшее число = выше роль)
    rank = get_admin_rank(user_id)
    return rank is not None and rank <= min_rank

def set_admin_rank(user_id: int, rank: int):
    if rank not in (2, 3):
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO admin_roles (user_id, rank) VALUES (?, ?)", (user_id, rank))
    conn.commit()
    conn.close()

def remove_admin_rank(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_roles WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_vip_chat_id():
    vip = get_setting('vip_chat_id')
    return int(vip) if vip else None

def get_vip_invite_link():
    return get_setting('vip_invite_link') or ''

def get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_username(username: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_level(user_id: int):
    user = get_user(user_id)
    return user[3] if user else 'Новичок'

def create_user_if_not_exists(user_id, username, full_name, referred_by=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?, ?, ?, ?)",
        (user_id, username, full_name, referred_by)
    )
    is_new_user = cur.rowcount > 0
    cur.execute(
        "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
        (username, full_name, user_id)
    )
    conn.commit()
    conn.close()
    return is_new_user

def apply_referral_bonus_if_eligible(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT referred_by, referral_rewarded FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    referred_by, referral_rewarded = row
    if not referred_by or referral_rewarded:
        conn.close()
        return False
    if referred_by == user_id:
        cur.execute("UPDATE users SET referral_rewarded = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return False
    cur.execute("SELECT 1 FROM users WHERE user_id = ?", (referred_by,))
    if not cur.fetchone():
        conn.close()
        return False
    ref_coins = int(get_setting('ref_bonus_coins') or 50)
    ref_xp = int(get_setting('ref_bonus_xp') or 10)
    cur.execute(
        "UPDATE users SET referral_count = referral_count + 1, coins = coins + ? WHERE user_id = ?",
        (ref_coins, referred_by)
    )
    cur.execute("UPDATE users SET referral_rewarded = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    add_xp(referred_by, ref_xp)
    return True

def update_user_field(user_id, field, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def log_moderation_action(action_type: str, target_user: int, admin_user: int, reason: str, duration_seconds: int = 0, chat_id: Optional[int] = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO moderation_actions (action_type, target_user, admin_user, reason, duration_seconds, chat_id) VALUES (?, ?, ?, ?, ?, ?)",
        (action_type, target_user, admin_user, reason, duration_seconds, chat_id)
    )
    conn.commit()
    conn.close()

def get_moderation_history(target_user: int, limit: int = 10):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT action_type, admin_user, reason, duration_seconds, created_at FROM moderation_actions WHERE target_user = ? ORDER BY id DESC LIMIT ?",
        (target_user, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def parse_duration_to_seconds(raw: str) -> Optional[int]:
    raw = (raw or "").strip().lower()
    match = re.fullmatch(r"(\d+)([mhd])", raw)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == 'm':
        return value * 60
    if unit == 'h':
        return value * 3600
    return value * 86400

def get_user_feedback_stats(user_id: int) -> Tuple[float, int]:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT AVG(rating), COUNT(*) FROM feedbacks WHERE to_user = ? AND deleted = 0", (user_id,))
    avg, count = cur.fetchone()
    conn.close()
    return float(avg or 0), int(count or 0)

def resolve_target_user(target_raw: str):
    target_raw = (target_raw or "").strip()
    if not target_raw:
        return None
    if target_raw.startswith('@'):
        return get_user_by_username(target_raw.lstrip('@'))
    try:
        return get_user(int(target_raw))
    except ValueError:
        return None

def format_user_label(user_id: int) -> str:
    user = get_user(user_id)
    if not user:
        return str(user_id)
    if user[1]:
        return f"@{user[1]}"
    return f"{user[2]} ({user_id})"

def get_subscription_link() -> str:
    if REQUIRED_SUBSCRIPTION_LINK:
        return REQUIRED_SUBSCRIPTION_LINK
    if REQUIRED_SUBSCRIPTION_CHAT.startswith("@"):
        return f"https://t.me/{REQUIRED_SUBSCRIPTION_CHAT.lstrip('@')}"
    return ""

async def is_user_subscribed(user_id: int) -> bool:
    if not REQUIRED_SUBSCRIPTION_CHAT:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_SUBSCRIPTION_CHAT, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logging.warning(f"Не удалось проверить подписку пользователя {user_id}: {e}")
        return False

async def send_subscription_required_message(message: Message, edit_only: bool = False):
    subscribe_link = get_subscription_link()
    rows = []
    if subscribe_link:
        rows.append([InlineKeyboardButton(text="✅ Подписаться", url=subscribe_link)])
    rows.append([InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_subscription")])
    text = (
        "📌 Для использования бота нужна обязательная подписка на чат.\n"
        "Подпишитесь и нажмите «Проверить подписку»."
    )
    if edit_only:
        try:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            return
        except TelegramBadRequest:
            return
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def ensure_subscription_or_prompt(message: Message, user_id: int, edit_only: bool = False) -> bool:
    if await is_user_subscribed(user_id):
        apply_referral_bonus_if_eligible(user_id)
        return True
    await send_subscription_required_message(message, edit_only=edit_only)
    return False

def add_coins(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def get_coins(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def add_xp(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT xp, total_xp, level FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    new_xp = row[0] + amount
    total_xp = row[1] + amount
    current_level = row[2]
    levels = get_levels()
    new_level = None
    for level_name, xp_req, bonus, comm, noq in levels:
        if new_xp >= xp_req:
            new_level = level_name
    if new_level and new_level != current_level:
        for level_name, xp_req, bonus, comm, noq in levels:
            if level_name == new_level:
                add_coins(user_id, bonus)
                update_user_field(user_id, 'commission_discount', comm)
                update_user_field(user_id, 'no_queue', 1 if noq else 0)
                asyncio.create_task(bot.send_message(user_id, f"🎉 Поздравляем! Вы достигли уровня {new_level}! Получено {bonus} монет."))
                break
    cur.execute(
        "UPDATE users SET xp = ?, total_xp = ?, level = ? WHERE user_id = ?",
        (new_xp, total_xp, new_level or current_level, user_id)
    )
    conn.commit()
    conn.close()
    return new_level

def get_levels():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT name, xp_required, bonus_coins, commission_rate, no_queue FROM levels ORDER BY xp_required")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_xp_progress(current_xp: int, current_level: str):
    levels = get_levels()
    current_req = 0
    next_level_name = None
    next_req = None
    for i, (name, xp_req, *_) in enumerate(levels):
        if name == current_level:
            current_req = xp_req
            if i + 1 < len(levels):
                next_level_name = levels[i + 1][0]
                next_req = levels[i + 1][1]
            break
    if next_req is None:
        return None, None, 1.0, 0
    span = next_req - current_req
    if span <= 0:
        return next_level_name, next_req, 1.0, 0
    progress = (current_xp - current_req) / span
    progress = max(0.0, min(1.0, progress))
    xp_to_next = max(0, next_req - current_xp)
    return next_level_name, next_req, progress, xp_to_next

def build_xp_bar_html(filled_count: int, bar_length: int = 10) -> str:
    filled_id = get_setting('xp_bar_filled_emoji_id') or '5366058919019980842'
    empty_id = get_setting('xp_bar_empty_emoji_id') or '5368525295399773078'
    filled_count = max(0, min(bar_length, filled_count))
    empty_count = bar_length - filled_count
    filled = ''.join(f'<tg-emoji emoji-id="{filled_id}">🟣</tg-emoji>' for _ in range(filled_count))
    empty = ''.join(f'<tg-emoji emoji-id="{empty_id}">🔵</tg-emoji>' for _ in range(empty_count))
    return filled + empty

def format_profile_xp_section(xp: int, total_xp: int, level_name: str) -> str:
    bar_length = int(get_setting('xp_bar_length') or 10)
    next_level_name, _, progress, xp_to_next = get_xp_progress(xp, level_name)
    filled = int(progress * bar_length)
    if next_level_name and filled >= bar_length and xp_to_next > 0:
        filled = bar_length - 1
    bar = build_xp_bar_html(filled, bar_length)
    lines = [
        f"⭐ Опыт: {xp} XP (всего {total_xp})",
        bar,
    ]
    if next_level_name:
        lines.append(f"📈 До {html.escape(next_level_name)}: {xp_to_next} XP")
    else:
        lines.append("🏆 Максимальный уровень")
    return '\n'.join(lines)

def is_guarantor(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM guarantors WHERE user_id = ? AND is_active = 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def add_guarantor(user_id, vip_chat_id=None, commission_rate=0.02):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM guarantors WHERE user_id = ?", (user_id,))
    if cur.fetchone():
        cur.execute(
            "UPDATE guarantors SET is_active = 1, vip_chat_id = COALESCE(?, vip_chat_id), commission_rate = COALESCE(?, commission_rate) WHERE user_id = ?",
            (vip_chat_id, commission_rate, user_id)
        )
    else:
        cur.execute(
            "INSERT INTO guarantors (user_id, vip_chat_id, commission_rate, max_deal_amount, max_concurrent_deals) VALUES (?, ?, ?, 0, 1)",
            (user_id, vip_chat_id, commission_rate)
        )
    conn.commit()
    conn.close()

def set_guarantor_vip_data(user_id: int, vip_chat_id: Optional[int] = None, vip_invite_link: Optional[str] = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    updates = []
    params = []
    if vip_chat_id is not None:
        updates.append("vip_chat_id = ?")
        params.append(vip_chat_id)
    if vip_invite_link is not None:
        updates.append("vip_invite_link = ?")
        params.append(vip_invite_link)
    if not updates:
        conn.close()
        return
    params.append(user_id)
    cur.execute(f"UPDATE guarantors SET {', '.join(updates)} WHERE user_id = ?", tuple(params))
    conn.commit()
    conn.close()

def remove_guarantor(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM guarantors WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_guarantor_limits(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT max_deal_amount, max_concurrent_deals FROM guarantors WHERE user_id = ? AND is_active = 1",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row if row else (0, 1)

def get_active_deals_count_for_guarantor(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM deals WHERE guarantor_id = ? AND status IN ('pending', 'vip_created', 'public', 'completion_pending')",
        (user_id,)
    )
    count = cur.fetchone()[0]
    conn.close()
    return int(count or 0)

def can_guarantor_take_deal(user_id: int, amount: float) -> bool:
    max_amount, max_concurrent = get_guarantor_limits(user_id)
    if max_amount and max_amount > 0 and amount > max_amount:
        return False
    if get_active_deals_count_for_guarantor(user_id) >= int(max_concurrent or 1):
        return False
    return True

def get_guarantor_full_info(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT total_deals, rating, feedback_count, max_deal_amount, max_concurrent_deals FROM guarantors WHERE user_id = ? AND is_active = 1",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row

def get_free_guarantors(amount: Optional[float] = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, vip_chat_id, commission_rate FROM guarantors WHERE is_active = 1"
    )
    rows = cur.fetchall()
    conn.close()
    if amount is None:
        return [row for row in rows if can_guarantor_take_deal(row[0], 0)]
    return [row for row in rows if can_guarantor_take_deal(row[0], amount)]

def set_guarantor_deal(user_id, deal_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET current_deal_id = ? WHERE user_id = ?", (deal_id, user_id))
    conn.commit()
    conn.close()

def clear_guarantor_deal(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET current_deal_id = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def create_deal_request(buyer_id, seller_id, amount, description, priority=0):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deal_requests (buyer_id, seller_id, amount, description, priority) VALUES (?, ?, ?, ?, ?)",
        (buyer_id, seller_id, amount, description, priority)
    )
    req_id = cur.lastrowid
    conn.commit()
    conn.close()
    return req_id

def get_active_deal_requests():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, buyer_id, seller_id, amount, description, priority FROM deal_requests WHERE status = 'waiting' ORDER BY priority DESC, created_at"
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def set_deal_request_status(request_id, status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE deal_requests SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()

def create_deal(buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, is_vip, commission):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deals (buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, is_vip, commission) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, is_vip, commission)
    )
    deal_id = cur.lastrowid
    conn.commit()
    conn.close()
    return deal_id

def get_deal(deal_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM deals WHERE id = ?", (deal_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_deal_status(deal_id, status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
    if status == 'completed':
        cur.execute("UPDATE deals SET completed_at = CURRENT_TIMESTAMP WHERE id = ?", (deal_id,))
    conn.commit()
    conn.close()

def reset_deal_completion_confirmations(deal_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM deal_completion_confirmations WHERE deal_id = ?", (deal_id,))
    conn.commit()
    conn.close()

def add_deal_completion_confirmation(deal_id: int, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO deal_completion_confirmations (deal_id, user_id) VALUES (?, ?)",
        (deal_id, user_id)
    )
    conn.commit()
    conn.close()

def get_deal_completion_confirmations(deal_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM deal_completion_confirmations WHERE deal_id = ?", (deal_id,))
    rows = cur.fetchall()
    conn.close()
    return {row[0] for row in rows}

def add_deal_completion_message(deal_id: int, chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deal_completion_messages (deal_id, chat_id, message_id) VALUES (?, ?, ?)",
        (deal_id, chat_id, message_id)
    )
    conn.commit()
    conn.close()

def get_deal_completion_messages(deal_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT chat_id, message_id FROM deal_completion_messages WHERE deal_id = ?", (deal_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def clear_deal_completion_messages(deal_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM deal_completion_messages WHERE deal_id = ?", (deal_id,))
    conn.commit()
    conn.close()

def grant_vip_access(deal_id: int, chat_id: int, user_id: int, expires_at: datetime):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO vip_chat_access (deal_id, chat_id, user_id, expires_at) VALUES (?, ?, ?, ?)",
        (deal_id, chat_id, user_id, expires_at.isoformat())
    )
    conn.commit()
    conn.close()

def has_vip_access(chat_id: int, user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT expires_at FROM vip_chat_access WHERE chat_id = ? AND user_id = ? ORDER BY expires_at DESC LIMIT 1",
        (chat_id, user_id)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    try:
        return datetime.fromisoformat(row[0]) > datetime.now()
    except Exception:
        return False

def clear_vip_access_for_deal(deal_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM vip_chat_access WHERE deal_id = ?", (deal_id,))
    conn.commit()
    conn.close()

def add_feedback(from_user, to_user, rating, text):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO feedbacks (from_user, to_user, rating, text) VALUES (?, ?, ?, ?)",
        (from_user, to_user, rating, text)
    )
    conn.commit()
    cur.execute("SELECT AVG(rating), COUNT(*) FROM feedbacks WHERE to_user = ? AND deleted = 0", (to_user,))
    avg, count = cur.fetchone()
    cur.execute(
        "UPDATE guarantors SET rating = ?, feedback_count = ? WHERE user_id = ?",
        (avg or 0, count or 0, to_user)
    )
    conn.commit()
    conn.close()

def delete_feedback(feedback_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE feedbacks SET deleted = 1 WHERE id = ?", (feedback_id,))
    conn.commit()
    conn.close()

def get_feedbacks_for_user(target_user_id, limit=10):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, from_user, rating, text, timestamp FROM feedbacks WHERE to_user = ? AND deleted = 0 ORDER BY timestamp DESC LIMIT ?",
        (target_user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

FEEDBACKS_PER_PAGE = 5

def get_feedbacks_for_user_page(target_user_id: int, page: int = 0, per_page: int = FEEDBACKS_PER_PAGE):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM feedbacks WHERE to_user = ? AND deleted = 0",
        (target_user_id,)
    )
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT id, from_user, rating, text, timestamp FROM feedbacks WHERE to_user = ? AND deleted = 0 ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (target_user_id, per_page, page * per_page)
    )
    rows = cur.fetchall()
    conn.close()
    return rows, total

def build_feedbacks_keyboard(target_user_id: int, page: int, total: int, per_page: int = FEEDBACKS_PER_PAGE) -> InlineKeyboardMarkup:
    rows = []
    max_page = max(0, (total - 1) // per_page) if total else 0
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"user_feedbacks_{target_user_id}_{page - 1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"user_feedbacks_{target_user_id}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 К профилю", callback_data=f"profile_{target_user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_user_feedbacks(message: Message, target_user_id: int, page: int = 0, edit_only: bool = False):
    target = get_user(target_user_id)
    if not target:
        text = "❌ Пользователь не найден."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
        ])
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            if not edit_only:
                await message.answer(text, reply_markup=keyboard)
        return

    avg_rating, feedback_count = get_user_feedback_stats(target_user_id)
    feedbacks, total = get_feedbacks_for_user_page(target_user_id, page)
    user_label = format_user_label(target_user_id)
    keyboard = build_feedbacks_keyboard(target_user_id, page, total)

    if total == 0:
        text = f"📭 У {user_label} пока нет отзывов."
    else:
        max_page = max(0, (total - 1) // FEEDBACKS_PER_PAGE)
        text = (
            f"📝 Отзывы о {user_label}\n"
            f"Всего: {feedback_count} | Рейтинг: {avg_rating:.2f}\n"
        )
        if max_page > 0:
            text += f"Страница {page + 1}/{max_page + 1}\n"
        text += "\n"
        for fb in feedbacks:
            author = format_user_label(fb[1])
            text += (
                f"#{fb[0]} | От: {author} | {'⭐' * fb[2]}\n"
                f"Когда: {fb[4]}\n"
                f"{fb[3]}\n"
                f"{'—' * 20}\n"
            )

    try:
        await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        if not edit_only:
            await message.answer(text, reply_markup=keyboard)

def apply_premium_emoji_xp(user_id: int, emoji_hit: bool) -> int:
    if not emoji_hit:
        return 0
    configured_emoji = get_setting('premium_emoji_id') or ''
    if not configured_emoji:
        return 0
    base_xp = int(get_setting('premium_emoji_base_xp') or 2)
    growth = int(get_setting('premium_emoji_growth_step') or 1)
    max_xp = int(get_setting('premium_emoji_max_xp') or 10)
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT usage_count FROM premium_emoji_progress WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    usage_count = row[0] if row else 0
    next_count = usage_count + 1
    xp_gain = min(base_xp + (next_count - 1) * growth, max_xp)
    if row:
        cur.execute(
            "UPDATE premium_emoji_progress SET usage_count = ?, last_used_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (next_count, user_id)
        )
    else:
        cur.execute(
            "INSERT INTO premium_emoji_progress (user_id, usage_count) VALUES (?, ?)",
            (user_id, next_count)
        )
    conn.commit()
    conn.close()
    add_xp(user_id, xp_gain)
    return xp_gain

# ===================== СКАМ-СИСТЕМА =====================
def add_scammer(username, evidence, admin_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scammers (username, evidence, added_by) VALUES (?, ?, ?)",
        (username, evidence, admin_id)
    )
    conn.commit()
    conn.close()

def remove_scammer(username):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM scammers WHERE username = ?", (username,))
    conn.commit()
    conn.close()

def is_scammer(username):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT username FROM scammers WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def add_scam_report(reported_username, evidence, reporter_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scam_reports (reported_username, evidence, reporter_id) VALUES (?, ?, ?)",
        (reported_username, evidence, reporter_id)
    )
    report_id = cur.lastrowid
    conn.commit()
    conn.close()
    return report_id

def get_pending_scam_reports():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, reported_username, evidence, reporter_id, created_at FROM scam_reports WHERE status = 'pending'")
    rows = cur.fetchall()
    conn.close()
    return rows

def approve_scam_report(report_id, admin_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT reported_username, evidence FROM scam_reports WHERE id = ?", (report_id,))
    row = cur.fetchone()
    if row:
        username, evidence = row
        add_scammer(username, evidence, admin_id)
        cur.execute("UPDATE scam_reports SET status = 'approved', admin_comment = 'Одобрено админом' WHERE id = ?", (report_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def reject_scam_report(report_id, comment):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE scam_reports SET status = 'rejected', admin_comment = ? WHERE id = ?", (comment, report_id))
    conn.commit()
    conn.close()

# ===================== ВНЕШНИЕ СКАМ-БОТЫ =====================
def add_external_bot(name, username, api_url):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO external_bots (name, username, api_url) VALUES (?, ?, ?)",
        (name, username, api_url)
    )
    conn.commit()
    conn.close()

def remove_external_bot(bot_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM external_bots WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()

def get_external_bots():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name, username, api_url FROM external_bots WHERE is_active = 1")
    rows = cur.fetchall()
    conn.close()
    return rows

async def check_external_bot(username, api_url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{api_url}?username={username}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('found', False)
    except:
        return False
    return False

async def check_all_external_bots(username):
    bots = get_external_bots()
    for bot_id, name, bot_username, api_url in bots:
        if await check_external_bot(username, api_url):
            return True, name
    return False, None

# ===================== МАГАЗИН И ПРЕФИКСЫ =====================
def get_store_items():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, price, type, value FROM store_items WHERE available = 1")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_store_item(name, description, price, type, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO store_items (name, description, price, type, value) VALUES (?, ?, ?, ?, ?)",
        (name, description, price, type, value)
    )
    conn.commit()
    conn.close()

def remove_store_item(item_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE store_items SET available = 0 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()

def buy_item(user_id, item_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name, price, type, value FROM store_items WHERE id = ? AND available = 1", (item_id,))
    item = cur.fetchone()
    if not item:
        conn.close()
        return None
    if get_coins(user_id) < item[2]:
        conn.close()
        return False
    update_user_field(user_id, 'coins', get_coins(user_id) - item[2])
    try:
        cur.execute("INSERT INTO user_items (user_id, item_id) VALUES (?, ?)", (user_id, item_id))
    except sqlite3.IntegrityError:
        conn.close()
        return "already_owned"
    conn.commit()
    conn.close()
    return item

def get_user_items(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT item_id FROM user_items WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

def add_prefix_request(user_id, prefix, buyer_comment=""):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO prefix_requests (user_id, prefix, buyer_comment) VALUES (?, ?, ?)",
        (user_id, prefix, buyer_comment or "")
    )
    conn.commit()
    conn.close()

def get_pending_prefix_requests():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, prefix, buyer_comment FROM prefix_requests WHERE status = 'pending'")
    rows = cur.fetchall()
    conn.close()
    return rows

def approve_prefix_request(request_id, admin_comment=""):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id, prefix FROM prefix_requests WHERE id = ?", (request_id,))
    row = cur.fetchone()
    if row:
        user_id, prefix = row
        update_user_field(user_id, 'active_prefix', prefix)
        cur.execute(
            "UPDATE prefix_requests SET status = 'approved', admin_comment = ? WHERE id = ?",
            (admin_comment or "", request_id)
        )
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def reject_prefix_request(request_id, admin_comment=""):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "UPDATE prefix_requests SET status = 'rejected', admin_comment = ? WHERE id = ?",
        (admin_comment or "", request_id)
    )
    conn.commit()
    conn.close()

def get_badwords():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT word FROM blacklist")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

def add_badword(word):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO blacklist (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()

def remove_badword(word):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM blacklist WHERE word = ?", (word,))
    conn.commit()
    conn.close()

# ===================== СОСТОЯНИЯ ДЛЯ FSM =====================
# Все состояния объединены в одном классе
class DealStates(StatesGroup):
    waiting_for_buyer = State()
    waiting_for_seller = State()
    waiting_for_amount = State()
    waiting_for_description = State()

class AdminStates(StatesGroup):
    waiting_for_bio = State()
    waiting_for_level_name = State()
    waiting_for_level_xp = State()
    waiting_for_level_bonus = State()
    waiting_for_level_commission = State()
    waiting_for_level_noqueue = State()
    waiting_for_item_name = State()
    waiting_for_item_desc = State()
    waiting_for_item_price = State()
    waiting_for_item_type = State()
    waiting_for_item_value = State()
    waiting_for_badword = State()
    waiting_for_broadcast = State()
    waiting_for_scammer_username = State()
    waiting_for_scammer_evidence = State()
    waiting_for_feedback_delete = State()
    waiting_for_prefix_approve = State()
    waiting_for_prefix_reject = State()
    waiting_for_vip_threshold_amount = State()
    waiting_for_vip_threshold_level = State()
    waiting_for_moderation_toggle = State()
    waiting_for_xp_per_message = State()
    waiting_for_ref_bonus = State()
    waiting_for_deal_xp_percent = State()
    waiting_for_default_commission = State()
    waiting_for_scam_action = State()
    waiting_for_user_search = State()
    waiting_for_guarantor_id = State()
    waiting_for_user_xp = State()
    waiting_for_user_coins = State()
    waiting_for_user_level = State()
    waiting_for_remove_guarantor = State()
    waiting_for_external_bot_name = State()
    waiting_for_external_bot_username = State()
    waiting_for_external_bot_api = State()
    waiting_for_scam_report_approve = State()
    waiting_for_scam_report_reject = State()
    waiting_for_remove_scammer = State()
    waiting_for_guarantor_amount_limit = State()
    waiting_for_guarantor_concurrency_limit = State()
    waiting_for_admin_rank = State()
    waiting_for_guarantor_vip = State()

class FeedbackStates(StatesGroup):
    waiting_for_guarantor = State()
    waiting_for_rating = State()
    waiting_for_text = State()

class PrefixStates(StatesGroup):
    waiting_for_prefix = State()

class BuyStates(StatesGroup):
    waiting_for_item_id = State()
    waiting_for_purchase_comment = State()

class ReportScamStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_evidence = State()

# ===================== ОБРАБОТЧИКИ КОМАНД =====================
def build_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
         InlineKeyboardButton(text="🔒 Создать сделку", callback_data="menu_deal")],
        [InlineKeyboardButton(text="🛒 Магазин", callback_data="menu_shop"),
         InlineKeyboardButton(text="👥 Рефералы", callback_data="menu_referral")],
        [InlineKeyboardButton(text="📋 Помощь", callback_data="menu_help"),
         InlineKeyboardButton(text="⚠️ Сообщить о скамере", callback_data="menu_report_scam")],
    ])
    if is_admin(user_id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="menu_admin")])
    return keyboard

def get_state_back_keyboard(admin_flow: bool = False) -> InlineKeyboardMarkup:
    callback = "state_back_admin" if admin_flow else "state_back_main"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=callback)]]
    )

async def show_main_menu(message: Message, user_id: int, edit_only: bool = False):
    keyboard = build_main_menu_keyboard(user_id)
    if edit_only:
        try:
            await message.edit_text("👋 Добро пожаловать! Выберите действие:", reply_markup=keyboard)
            return
        except TelegramBadRequest:
            return
    await message.answer("👋 Добро пожаловать! Выберите действие:", reply_markup=keyboard)

@dp.message(Command("start"))
async def start_cmd(message: Message, command: CommandObject = None):
    user = message.from_user
    referred_by = None
    if command and command.args:
        try:
            referred_by = int(command.args)
        except:
            pass
    create_user_if_not_exists(user.id, user.username, user.full_name, referred_by)
    if not await ensure_subscription_or_prompt(message, user.id):
        return
    await show_main_menu(message, user.id)

@dp.callback_query(lambda c: c.data in ("state_back_main", "state_back_admin"))
async def state_back_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.data == "state_back_admin":
        await admin_panel(callback.message, callback.from_user.id, edit_only=True)
    else:
        await show_main_menu(callback.message, callback.from_user.id, edit_only=True)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    if await is_user_subscribed(callback.from_user.id):
        apply_referral_bonus_if_eligible(callback.from_user.id)
        await show_main_menu(callback.message, callback.from_user.id, edit_only=True)
        await callback.answer()
    else:
        await callback.answer("Подписка пока не обнаружена", show_alert=True)

# ------ Меню (обработчики callback) ------
@dp.callback_query(lambda c: c.data.startswith("menu_"))
async def menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await is_user_subscribed(callback.from_user.id):
        await callback.answer("Сначала подпишитесь на обязательный чат", show_alert=True)
        await send_subscription_required_message(callback.message, edit_only=True)
        return
    apply_referral_bonus_if_eligible(callback.from_user.id)
    action = callback.data.removeprefix("menu_")
    if action == "profile":
        await show_profile(callback.message, callback.from_user.id, edit_only=True)
    elif action == "deal":
        await create_deal_start(callback.message, callback.from_user.id, state)
    elif action == "shop":
        await show_shop(callback.message)
    elif action == "referral":
        await show_referral(callback.message, callback.from_user.id)
    elif action == "help":
        await show_help(callback.message)
    elif action == "admin":
        if is_admin(callback.from_user.id):
            await admin_panel(callback.message, callback.from_user.id, edit_only=True)
        else:
            await callback.answer("Нет доступа", show_alert=True)
    elif action == "main":
        await show_main_menu(callback.message, callback.from_user.id, edit_only=True)
    elif action == "report_scam":
        await report_scam_start(callback.message, callback.from_user.id, state)
    await callback.answer()

# ------ Профиль ------
async def show_profile(message: Message, user_id: int = None, edit_only: bool = False):
    if not user_id:
        user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        if edit_only:
            try:
                await message.edit_text("❌ Вы не зарегистрированы. Напишите /start")
            except TelegramBadRequest:
                pass
        else:
            await message.answer("❌ Вы не зарегистрированы. Напишите /start")
        return
    avg_rating, feedback_count = get_user_feedback_stats(user_id)
    admin_rank = get_admin_rank(user_id)
    guarantor_info = get_guarantor_full_info(user_id)
    scam_mark = "Да" if (user[1] and is_scammer(user[1])) else "Нет"
    username = html.escape(user[1] or 'без username')
    full_name = html.escape(user[2] or '')
    level_name = html.escape(user[3] or '')
    xp_section = format_profile_xp_section(user[4], user[5], user[3] or '')
    text = (
        f"📋 Профиль @{username}\n"
        f"👤 Имя: {full_name}\n"
        f"📊 Уровень: {level_name}\n"
        f"{xp_section}\n"
        f"💰 Монеты: {user[9]}\n"
        f"⚠️ Предупреждения: {user[10]}\n"
        f"🚫 В скам-базе: {scam_mark}\n"
        f"📝 Отзывы: {feedback_count} | Рейтинг: {avg_rating:.2f}\n"
        f"🔗 Рефералов: {user[14] or 0}\n"
        f"💳 Скидка на комиссию: {user[17] * 100}%\n"
        f"🚀 Без очереди: {'Да' if user[18] else 'Нет'}\n"
    )
    if admin_rank:
        text += f"🛡️ Ранг админа: {admin_rank}\n"
    if guarantor_info:
        active_deals = get_active_deals_count_for_guarantor(user_id)
        max_amount = guarantor_info[3] or 0
        max_amount_text = f"{max_amount} USDT" if max_amount > 0 else "без лимита"
        text += (
            f"👑 Гарант: да | Сделок всего: {guarantor_info[0]} | Рейтинг: {guarantor_info[1]:.2f}\n"
            f"📦 Одновременных сделок: {active_deals}/{guarantor_info[4]}\n"
            f"💰 Лимит суммы сделки: {max_amount_text}\n"
        )
    if user[15]:
        text += f"🏷️ Активный префикс: {html.escape(user[15])}\n"

    is_own_profile = message.from_user and message.from_user.id == user_id
    feedback_btn_text = f"📝 Отзывы ({feedback_count})" if feedback_count else "📝 Отзывы"
    keyboard_rows = []
    if is_own_profile:
        keyboard_rows.append([InlineKeyboardButton(text="✏️ Редактировать био", callback_data="edit_bio")])
        keyboard_rows.append([InlineKeyboardButton(text=feedback_btn_text, callback_data=f"user_feedbacks_{user_id}")])
    else:
        keyboard_rows.append([InlineKeyboardButton(text=feedback_btn_text, callback_data=f"user_feedbacks_{user_id}")])
        keyboard_rows.append([InlineKeyboardButton(text="📝 Оставить отзыв", callback_data=f"feedback_{user_id}")])
    keyboard_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    if is_admin(message.from_user.id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="menu_admin")])
    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest:
        if not edit_only:
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@dp.message(Command("profile", "i"))
async def profile_cmd(message: Message, command: CommandObject = None):
    if not await ensure_subscription_or_prompt(message, message.from_user.id):
        return
    target_id = message.from_user.id
    if command and command.args:
        target = resolve_target_user(command.args.split()[0])
        if not target:
            await message.answer("❌ Пользователь не найден. Используйте /profile @username или /profile ID")
            return
        target_id = target[0]
    await show_profile(message, target_id, edit_only=False)

@dp.callback_query(lambda c: c.data == "edit_bio")
async def edit_bio_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✏️ Отправьте новую биографию:", reply_markup=get_state_back_keyboard())
    await state.set_state(AdminStates.waiting_for_bio)
    await callback.answer()

@dp.message(AdminStates.waiting_for_bio)
async def process_bio(message: Message, state: FSMContext):
    update_user_field(message.from_user.id, 'bio', message.text)
    await state.clear()
    await message.answer("✅ Биография обновлена!")

@dp.callback_query(lambda c: c.data.startswith("profile_"))
async def profile_callback(callback: CallbackQuery):
    user_id = int(callback.data.split("_", 1)[1])
    await show_profile(callback.message, user_id, edit_only=True)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("user_feedbacks_"))
async def user_feedbacks_callback(callback: CallbackQuery):
    data = callback.data.removeprefix("user_feedbacks_")
    if "_" in data and data.rsplit("_", 1)[1].isdigit():
        user_id_str, page_str = data.rsplit("_", 1)
        page = int(page_str)
    else:
        user_id_str = data
        page = 0
    await show_user_feedbacks(callback.message, int(user_id_str), page=page, edit_only=True)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "my_feedbacks")
async def my_feedbacks(callback: CallbackQuery):
    await show_user_feedbacks(callback.message, callback.from_user.id, page=0, edit_only=True)
    await callback.answer()

@dp.message(Command("feedbacks", "reviews"))
async def feedbacks_cmd(message: Message, command: CommandObject = None):
    if not await ensure_subscription_or_prompt(message, message.from_user.id):
        return
    target_id = message.from_user.id
    if command and command.args:
        target = resolve_target_user(command.args.split()[0])
        if not target:
            await message.answer("❌ Пользователь не найден. Используйте /feedbacks @username или /feedbacks ID")
            return
        target_id = target[0]
    await show_user_feedbacks(message, target_id, page=0, edit_only=False)

# ------ Создание сделки ------
async def submit_deal_request(message: Message, buyer_id: int, seller_id: int, seller_username: str, amount: float, description: str):
    buyer_level = get_user_level(buyer_id)
    seller_level = get_user_level(seller_id)
    levels = get_levels()
    priority = 0
    for level_name, xp_req, bonus, comm, noq in levels:
        if level_name == buyer_level or level_name == seller_level:
            if noq:
                priority = 1
                break

    request_id = create_deal_request(buyer_id, seller_id, amount, description, priority)
    free_guarantors = get_free_guarantors(amount)
    if not free_guarantors:
        await message.answer("😔 Нет свободных гарантов. Ваша заявка будет в очереди.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Провести сделку", callback_data=f"take_deal_{request_id}")]
    ])

    for guarantor_id, vip_chat, comm in free_guarantors:
        try:
            await bot.send_message(
                guarantor_id,
                f"🔔 Новая заявка на сделку!\n"
                f"Покупатель: @{message.from_user.username or 'без username'}\n"
                f"Продавец: @{seller_username}\n"
                f"Сумма: {amount} USDT\n"
                f"Описание: {description}\n"
                f"Приоритет: {'Да' if priority else 'Нет'}",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить гаранта {guarantor_id}: {e}")

    await message.answer("✅ Заявка создана. Ожидайте, когда гарант примет сделку.")

async def create_deal_start(message: Message, user_id: int, state: FSMContext):
    await message.edit_text(
        "🔒 Создание сделки.\nВведите @username или ID покупателя:",
        reply_markup=get_state_back_keyboard()
    )
    await state.set_state(DealStates.waiting_for_buyer)

@dp.message(Command("deal"))
async def deal_command(message: Message, command: CommandObject = None, state: FSMContext = None):
    if not await ensure_subscription_or_prompt(message, message.from_user.id):
        return
    if command and command.args:
        parts = command.args.split(maxsplit=3)
        if len(parts) < 4:
            await message.answer("❌ Формат: /deal @buyer @seller сумма описание")
            return
        buyer = resolve_target_user(parts[0])
        seller = resolve_target_user(parts[1])
        if not buyer or not seller:
            await message.answer("❌ Покупатель или продавец не найден.")
            return
        if message.from_user.id not in (buyer[0], seller[0]):
            await message.answer("❌ Создатель сделки должен быть покупателем или продавцом.")
            return
        try:
            amount = float(parts[2].replace(',', '.'))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Сумма должна быть положительным числом.")
            return
        description = parts[3].strip()
        await submit_deal_request(message, buyer[0], seller[0], seller[1] or str(seller[0]), amount, description)
        return
    await message.answer(
        "🔒 Создание сделки.\nВведите @username или ID покупателя:",
        reply_markup=get_state_back_keyboard()
    )
    await state.set_state(DealStates.waiting_for_buyer)

@dp.message(DealStates.waiting_for_buyer)
async def deal_buyer(message: Message, state: FSMContext):
    buyer = resolve_target_user(message.text.strip())
    if not buyer:
        await message.answer("❌ Покупатель не найден. Введите @username или ID.")
        return
    await state.update_data(buyer_id=buyer[0], buyer_username=buyer[1] or "")
    await message.answer("Введите @username или ID продавца:", reply_markup=get_state_back_keyboard())
    await state.set_state(DealStates.waiting_for_seller)

@dp.message(DealStates.waiting_for_seller)
async def deal_seller(message: Message, state: FSMContext):
    seller = resolve_target_user(message.text.strip())
    if not seller:
        await message.answer("❌ Продавец не найден. Введите @username или ID.")
        return
    data = await state.get_data()
    buyer_id = data.get("buyer_id")
    if buyer_id is None:
        await state.clear()
        await message.answer("❌ Сессия создания сделки сброшена, начните заново.")
        return
    if message.from_user.id not in (buyer_id, seller[0]):
        await state.clear()
        await message.answer("❌ Создатель сделки должен быть покупателем или продавцом.")
        return
    await state.update_data(seller_id=seller[0], seller_username=seller[1] or "")
    await message.answer("💰 Введите сумму сделки (в USDT):", reply_markup=get_state_back_keyboard())
    await state.set_state(DealStates.waiting_for_amount)

@dp.message(DealStates.waiting_for_amount)
async def deal_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    await state.update_data(amount=amount)
    await message.answer("📝 Введите краткое описание сделки:", reply_markup=get_state_back_keyboard())
    await state.set_state(DealStates.waiting_for_description)

@dp.message(DealStates.waiting_for_description)
async def deal_description(message: Message, state: FSMContext):
    description = message.text
    data = await state.get_data()
    buyer_id = data["buyer_id"]
    seller_id = data['seller_id']
    seller_username = data.get('seller_username', 'без username')
    amount = data['amount']

    await submit_deal_request(message, buyer_id, seller_id, seller_username, amount, description)
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("take_deal_"))
async def take_deal(callback: CallbackQuery):
    request_id = int(callback.data.split("_")[2])
    guarantor_id = callback.from_user.id
    if not is_guarantor(guarantor_id):
        await callback.answer("Вы не являетесь гарантом", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT buyer_id, seller_id, amount, description, priority FROM deal_requests WHERE id = ? AND status = 'waiting'",
        (request_id,)
    )
    request = cur.fetchone()
    if not request:
        conn.close()
        await callback.answer("Эта заявка уже не актуальна", show_alert=True)
        return
    buyer_id, seller_id, amount, description, priority = request
    if not can_guarantor_take_deal(guarantor_id, amount):
        conn.close()
        await callback.answer("Лимит гаранта: слишком много активных сделок или превышена сумма", show_alert=True)
        return
    cur.execute("UPDATE deal_requests SET status = 'taken' WHERE id = ?", (request_id,))
    conn.commit()
    conn.close()

    vip_threshold_amount = float(get_setting('vip_threshold_amount') or 500)
    vip_threshold_level = get_setting('vip_threshold_level') or 'Эксперт'
    buyer_level = get_user_level(buyer_id)
    seller_level = get_user_level(seller_id)
    levels = get_levels()
    level_names = [l[0] for l in levels]
    need_vip = False
    if amount >= vip_threshold_amount:
        need_vip = True
    if buyer_level in level_names and level_names.index(buyer_level) >= level_names.index(vip_threshold_level):
        need_vip = True
    if seller_level in level_names and level_names.index(seller_level) >= level_names.index(vip_threshold_level):
        need_vip = True

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT commission_rate, vip_chat_id FROM guarantors WHERE user_id = ?", (guarantor_id,))
    guarantor_data = cur.fetchone()
    conn.close()
    commission_rate = guarantor_data[0] if guarantor_data else 0.02
    vip_chat_id = guarantor_data[1] if guarantor_data else get_vip_chat_id()

    deal_id = create_deal(buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, need_vip, commission_rate)
    set_guarantor_deal(guarantor_id, deal_id)

    await bot.send_message(buyer_id, f"✅ Ваша сделка принята гарантом @{callback.from_user.username or 'без username'}\nСумма: {amount} USDT")
    await bot.send_message(seller_id, f"✅ Ваша сделка принята гарантом @{callback.from_user.username or 'без username'}\nСумма: {amount} USDT")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 VIP-офис", callback_data=f"deal_vip_{deal_id}")],
    ])
    if not need_vip:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="💬 Общий чат", callback_data=f"deal_public_{deal_id}")])

    await callback.message.edit_text(
        f"🔑 Вы стали гарантом сделки #{deal_id}\n"
        f"Сумма: {amount} USDT\n"
        f"Описание: {description}\n"
        f"Выберите место проведения:",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("deal_vip_") or c.data.startswith("deal_public_"))
async def choose_place(callback: CallbackQuery):
    data = callback.data.split("_")
    place = data[1]
    deal_id = int(data[2])
    deal = get_deal(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена", show_alert=True)
        return
    buyer_id, seller_id, guarantor_id, amount, description, status, vip_chat_id, created_at, completed_at, is_vip, commission = deal[1], deal[2], deal[3], deal[4], deal[5], deal[6], deal[7], deal[8], deal[9], deal[10], deal[11]

    if place == "vip":
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("SELECT vip_invite_link, vip_chat_id FROM guarantors WHERE user_id = ?", (guarantor_id,))
        row = cur.fetchone()
        conn.close()
        invite_link = row[0] if row else None
        guarantor_vip_chat_id = row[1] if row else None
        if not invite_link:
            await callback.answer("VIP-ссылка не настроена. Используйте /setvipchat <invite_link> <chat_id>", show_alert=True)
            return
        if not guarantor_vip_chat_id:
            await callback.answer("Для автоприёма нужен chat_id VIP-чата. Укажите его в /setvipchat <invite_link> <chat_id>", show_alert=True)
            return
        access_expires = datetime.now() + timedelta(hours=4)
        grant_vip_access(deal_id, int(guarantor_vip_chat_id), buyer_id, access_expires)
        grant_vip_access(deal_id, int(guarantor_vip_chat_id), seller_id, access_expires)
        update_deal_status(deal_id, 'vip_created')
        await bot.send_message(buyer_id, f"🏢 Ссылка на VIP-офис: {invite_link}")
        await bot.send_message(seller_id, f"🏢 Ссылка на VIP-офис: {invite_link}")
        await callback.message.edit_text("✅ VIP-офис создан. Ссылки отправлены участникам.")
        await bot.send_message(guarantor_id, "🏢 VIP-офис создан. После завершения сделки напишите /complete в этом чате или в VIP-чате.")
    else:
        update_deal_status(deal_id, 'public')
        await callback.message.edit_text("💬 Сделка будет проведена в общем чате.")
        await bot.send_message(buyer_id, "💬 Сделка будет проведена в общем чате.")
        await bot.send_message(seller_id, "💬 Сделка будет проведена в общем чате.")
    await callback.answer()

# ------ Завершение сделки ------
async def cleanup_deal_after_delay(deal_id: int, buyer_id: int, seller_id: int, guarantor_id: int, is_vip: int, vip_chat_id: Optional[int]):
    await asyncio.sleep(300)
    for chat_id, message_id in get_deal_completion_messages(deal_id):
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    clear_deal_completion_messages(deal_id)
    clear_vip_access_for_deal(deal_id)

    if is_vip and vip_chat_id:
        for participant_id in [buyer_id, seller_id, guarantor_id]:
            if is_admin(participant_id):
                continue
            try:
                await bot.ban_chat_member(vip_chat_id, participant_id)
                await bot.unban_chat_member(vip_chat_id, participant_id)
            except Exception as e:
                logging.error(f"Ошибка очистки VIP-офиса для пользователя {participant_id}: {e}")
        try:
            await bot.send_message(guarantor_id, f"🗑️ VIP-офис по сделке #{deal_id} очищен.")
        except Exception:
            pass

async def finalize_completed_deal(deal_id: int) -> bool:
    deal = get_deal(deal_id)
    if not deal:
        return False
    status = deal[6]
    if status == "completed":
        return False
    if status not in ("completion_pending", "vip_created", "public"):
        return False
    buyer_id, seller_id, guarantor_id, amount, is_vip, vip_chat_id, commission = deal[1], deal[2], deal[3], deal[4], deal[10], deal[7], deal[11]

    update_deal_status(deal_id, 'completed')
    clear_guarantor_deal(guarantor_id)
    reset_deal_completion_confirmations(deal_id)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET total_deals = total_deals + 1 WHERE user_id = ?", (guarantor_id,))
    conn.commit()
    conn.close()

    xp_percent = int(get_setting('deal_xp_percent') or 5)
    xp_gain = int(amount * xp_percent / 100) if xp_percent else 0
    add_xp(buyer_id, xp_gain)
    add_xp(seller_id, xp_gain)
    add_xp(guarantor_id, xp_gain)

    commission_currency = get_setting('commission_currency') or 'coins'
    if commission_currency == 'coins':
        commission_coins = int(amount * commission) if commission else 0
        add_coins(guarantor_id, commission_coins)
        commission_text = f"{commission_coins} монет"
    else:
        commission_usdt = amount * commission
        commission_text = f"{commission_usdt:.2f} USDT (не начисляется в боте)"

    notify_text = f"✅ Сделка #{deal_id} завершена. Начислено {xp_gain} XP. Комиссия гаранта: {commission_text}."
    for uid in [buyer_id, seller_id, guarantor_id]:
        try:
            await bot.send_message(uid, notify_text)
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление о завершении пользователю {uid}: {e}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Оставить отзыв о гаранте", callback_data=f"feedback_{guarantor_id}")]
    ])
    for uid in [buyer_id, seller_id]:
        try:
            await bot.send_message(uid, "📝 Пожалуйста, оцените работу гаранта.", reply_markup=keyboard)
        except Exception as e:
            logging.warning(f"Не удалось отправить запрос отзыва пользователю {uid}: {e}")

    asyncio.create_task(cleanup_deal_after_delay(deal_id, buyer_id, seller_id, guarantor_id, is_vip, vip_chat_id))
    return True

@dp.message(Command("complete"))
async def complete_deal(message: Message):
    guarantor_id = message.from_user.id
    if not is_guarantor(guarantor_id):
        await message.answer("Вы не являетесь гарантом.")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, buyer_id, seller_id, amount, vip_chat_id, is_vip, commission, status FROM deals WHERE guarantor_id = ? AND status IN ('vip_created', 'public', 'completion_pending')",
        (guarantor_id,)
    )
    deal = cur.fetchone()
    if not deal:
        conn.close()
        await message.answer("У вас нет активных сделок.")
        return
    conn.close()
    deal_id, buyer_id, seller_id, amount, vip_chat_id, is_vip, commission, status = deal
    if status == "completion_pending":
        await message.answer("⏳ По этой сделке уже ожидаются подтверждения сторон.")
        return

    update_deal_status(deal_id, 'completion_pending')
    reset_deal_completion_confirmations(deal_id)

    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить завершение", callback_data=f"confirm_complete_{deal_id}")]
    ])
    prompt_text = (
        f"📌 Сделка #{deal_id}: подтвердите завершение.\n"
        "Нужно подтверждение от покупателя и продавца."
    )
    try:
        confirmation_message = await message.answer(prompt_text, reply_markup=confirm_keyboard)
        add_deal_completion_message(deal_id, confirmation_message.chat.id, confirmation_message.message_id)
    except Exception:
        pass
    for participant_id in [buyer_id, seller_id]:
        try:
            dm_msg = await bot.send_message(participant_id, prompt_text, reply_markup=confirm_keyboard)
            add_deal_completion_message(deal_id, dm_msg.chat.id, dm_msg.message_id)
        except Exception as e:
            logging.warning(f"Не удалось отправить запрос подтверждения пользователю {participant_id}: {e}")

    await message.answer("⏳ Запрос на подтверждение отправлен сторонам. После подтверждения обеими сделка завершится.")

@dp.callback_query(lambda c: c.data.startswith("confirm_complete_"))
async def confirm_complete_callback(callback: CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    deal = get_deal(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена", show_alert=True)
        return
    if deal[6] != "completion_pending":
        await callback.answer("Подтверждение уже не требуется", show_alert=True)
        return
    buyer_id, seller_id = deal[1], deal[2]
    if callback.from_user.id not in (buyer_id, seller_id):
        await callback.answer("Только покупатель и продавец могут подтверждать", show_alert=True)
        return

    add_deal_completion_confirmation(deal_id, callback.from_user.id)
    confirmed = get_deal_completion_confirmations(deal_id)
    if buyer_id in confirmed and seller_id in confirmed:
        await callback.answer("✅ Второе подтверждение получено")
        if await finalize_completed_deal(deal_id):
            try:
                await callback.message.edit_text(f"✅ Сделка #{deal_id} закрыта. Через 5 минут служебные сообщения будут очищены.")
            except Exception:
                pass
        return

    waiting_for = "продавца" if buyer_id in confirmed else "покупателя"
    await callback.answer(f"✅ Подтверждено. Ожидаем подтверждение от {waiting_for}.")

# ------ Отзывы ------
@dp.message(Command("feedback"))
async def feedback_command(message: Message, command: CommandObject = None, state: FSMContext = None):
    if not await ensure_subscription_or_prompt(message, message.from_user.id):
        return
    if not command or not command.args:
        await message.answer("❌ Используйте: /feedback @username 1-5 текст_отзыва")
        return
    parts = command.args.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("❌ Используйте: /feedback @username 1-5 текст_отзыва")
        return
    target = resolve_target_user(parts[0])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    if target[0] == message.from_user.id:
        await message.answer("❌ Нельзя оставить отзыв самому себе.")
        return
    try:
        rating = int(parts[1])
        if rating < 1 or rating > 5:
            raise ValueError
    except ValueError:
        await message.answer("❌ Оценка должна быть от 1 до 5.")
        return
    add_feedback(message.from_user.id, target[0], rating, parts[2].strip())
    await message.answer(f"✅ Отзыв для @{target[1] or target[0]} сохранён.")

@dp.callback_query(lambda c: c.data.startswith("feedback_"))
async def feedback_start(callback: CallbackQuery, state: FSMContext):
    target_user_id = int(callback.data.split("_")[1])
    if target_user_id == callback.from_user.id:
        await callback.answer("Нельзя оставить отзыв самому себе", show_alert=True)
        return
    await state.update_data(target_user_id=target_user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{i} ⭐", callback_data=f"rate_{i}") for i in range(1, 6)]
    ])
    await callback.message.edit_text("Оцените пользователя (1-5):", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("rate_"))
async def feedback_rating(callback: CallbackQuery, state: FSMContext):
    rating = int(callback.data.split("_")[1])
    await state.update_data(rating=rating)
    await callback.message.edit_text("✏️ Напишите текстовый отзыв:", reply_markup=get_state_back_keyboard())
    await state.set_state(FeedbackStates.waiting_for_text)
    await callback.answer()

@dp.message(FeedbackStates.waiting_for_text)
async def feedback_text(message: Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data['target_user_id']
    rating = data['rating']
    text = message.text
    add_feedback(message.from_user.id, target_user_id, rating, text)
    await state.clear()
    await message.answer("✅ Спасибо за отзыв!")

# ------ Магазин ------
async def show_shop(message: Message):
    items = get_store_items()
    if not items:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
        ])
        await message.edit_text("🛒 Магазин пуст.", reply_markup=keyboard)
        return
    text = "🛒 Магазин:\n\n"
    for item in items:
        text += f"ID: {item[0]} | {item[1]} - {item[3]} монет\n  {item[2]}\n  Тип: {item[4]}, Значение: {item[5]}\n\n"
    keyboard_rows = [
        [InlineKeyboardButton(text=f"🛍️ Купить: {item[1]} ({item[3]}💰)", callback_data=f"buy_direct_{item[0]}")]
        for item in items
    ]
    keyboard_rows.append([InlineKeyboardButton(text="📝 Купить по ID", callback_data="buy_item")])
    keyboard_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.edit_text(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("buy_direct_"))
async def buy_direct_item(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.split("_")[2])
    result = buy_item(callback.from_user.id, item_id)
    if result is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if result is False:
        await callback.answer("Недостаточно монет", show_alert=True)
        return
    if result == "already_owned":
        await callback.answer("Товар уже куплен", show_alert=True)
        return

    item_name, item_type, item_value = result[1], result[3], result[4]
    if item_type == 'prefix':
        await state.update_data(prefix_value=item_value, prefix_item_name=item_name)
        await callback.message.edit_text(
            "📝 Введите комментарий к покупке префикса (например, зачем нужен префикс).\n"
            "Отправьте '-' если без комментария.",
            reply_markup=get_state_back_keyboard()
        )
        await state.set_state(BuyStates.waiting_for_purchase_comment)
        await callback.answer()
        return
    else:
        await callback.answer(f"Куплено: {item_name}", show_alert=True)
    await show_shop(callback.message)

@dp.callback_query(lambda c: c.data == "buy_item")
async def buy_item_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID товара, который хотите купить:", reply_markup=get_state_back_keyboard())
    await state.set_state(BuyStates.waiting_for_item_id)
    await callback.answer()

@dp.message(BuyStates.waiting_for_item_id)
async def process_buy_item(message: Message, state: FSMContext):
    try:
        item_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    result = buy_item(message.from_user.id, item_id)
    if result is None:
        await message.answer("❌ Товар не найден или недоступен.")
    elif result is False:
        await message.answer("❌ Недостаточно монет.")
    elif result == "already_owned":
        await message.answer("❌ Этот товар уже куплен.")
    else:
        item_name, item_type, item_value = result[1], result[3], result[4]
        if item_type == 'prefix':
            await state.update_data(prefix_value=item_value, prefix_item_name=item_name)
            await message.answer(
                "📝 Введите комментарий к покупке префикса (например, зачем нужен префикс).\n"
                "Отправьте '-' если без комментария.",
                reply_markup=get_state_back_keyboard()
            )
            await state.set_state(BuyStates.waiting_for_purchase_comment)
            return
        else:
            await message.answer(f"✅ Товар '{item_name}' куплен! Тип: {item_type}, значение: {item_value}.")
    await state.clear()

@dp.message(BuyStates.waiting_for_purchase_comment)
async def process_prefix_purchase_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    prefix_value = data.get("prefix_value")
    item_name = data.get("prefix_item_name", "Префикс")
    if not prefix_value:
        await state.clear()
        await message.answer("❌ Не удалось определить товар, купите заново.")
        return
    comment = "" if message.text.strip() == "-" else message.text.strip()
    add_prefix_request(message.from_user.id, prefix_value, comment)
    await state.clear()
    await message.answer(
        f"✅ Товар '{item_name}' куплен!\n"
        f"Заявка на префикс отправлена администратору.\n"
        f"Комментарий: {comment or '—'}"
    )

# ------ Рефералы ------
async def show_referral(message: Message, user_id: int = None):
    if not user_id:
        user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.edit_text("❌ Вы не зарегистрированы.")
        return
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    text = (
        f"👥 Ваша реферальная ссылка:\n{ref_link}\n\n"
        f"Приглашено: {user[14] or 0}\n"
        f"Бонус за приглашение: {get_setting('ref_bonus_coins')} монет и {get_setting('ref_bonus_xp')} XP"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
    ])
    await message.edit_text(text, reply_markup=keyboard)

# ------ Помощь ------
async def show_help(message: Message):
    text = (
        "📋 Помощь по боту\n\n"
        "🔒 Создать сделку: нажмите кнопку и следуйте инструкциям.\n"
        "🛒 Магазин: покупка префиксов, ролей, скидок.\n"
        "👥 Рефералы: ваша реферальная ссылка.\n"
        "👤 Профиль: просмотр и редактирование.\n"
        "⚠️ Сообщить о скамере: отправьте жалобу на мошенника.\n\n"
        "Для гарантов:\n"
        "- /complete - завершить активную сделку.\n"
        "- /setvipchat <invite_link> <chat_id> - задать VIP-офис для себя.\n"
        "- (админ) /setvipchat <guarantor_id> <invite_link> <chat_id> - для конкретного гаранта.\n\n"
        "Для всех пользователей:\n"
        "- /deal @seller сумма описание - быстро создать сделку из чата.\n"
        "- /profile @username - посмотреть профиль любого пользователя.\n"
        "- /feedbacks @username - посмотреть отзывы и их количество.\n"
        "- /feedback @username 1-5 текст - оставить отзыв пользователю.\n"
        "- /history @username - посмотреть причины предов/мутов/банов.\n\n"
        "Для администраторов:\n"
        "- /admin - панель управления.\n"
        "- /ban, /mute, /pred - модерация с сохранением причины.\n"
        "- /unban, /unmute - снять ограничения.\n"
        "- Ранг 3: /mute и /pred, ранг 2: /ban /mute /pred.\n"
        "Для владельцев:\n"
        "- /setadminrank <id> <2|3>, /deladminrank <id>."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
    ])
    await message.edit_text(text, reply_markup=keyboard)

# ------ Сообщить о скамере ------
async def report_scam_start(message: Message, user_id: int, state: FSMContext):
    await message.edit_text("⚠️ Сообщение о скамере.\nВведите @username мошенника:", reply_markup=get_state_back_keyboard())
    await state.set_state(ReportScamStates.waiting_for_username)

@dp.message(ReportScamStates.waiting_for_username)
async def report_scam_username(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    if not username:
        await message.answer("❌ Введите корректный username.")
        return
    await state.update_data(reported_username=username)
    await message.answer("📝 Введите доказательства (скриншоты, ссылки, описание):", reply_markup=get_state_back_keyboard())
    await state.set_state(ReportScamStates.waiting_for_evidence)

@dp.message(ReportScamStates.waiting_for_evidence)
async def report_scam_evidence(message: Message, state: FSMContext):
    evidence = message.text
    data = await state.get_data()
    reported_username = data['reported_username']
    reporter_id = message.from_user.id
    report_id = add_scam_report(reported_username, evidence, reporter_id)
    for admin_id in INITIAL_ADMINS:
        try:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_scam_{report_id}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_scam_{report_id}")]
            ])
            await bot.send_message(
                admin_id,
                f"🆕 Новая заявка на скамера #{report_id}\n"
                f"Username: @{reported_username}\n"
                f"Доказательства: {evidence}\n"
                f"От: @{message.from_user.username or 'без username'}",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить админа {admin_id}: {e}")
    await state.clear()
    await message.answer("✅ Заявка отправлена на модерацию. Администраторы проверят её в ближайшее время.")

# ------ Обработчики одобрения/отклонения скам-заявок (админы) ------
@dp.callback_query(lambda c: c.data.startswith("approve_scam_"))
async def approve_scam(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    report_id = int(callback.data.split("_")[2])
    if approve_scam_report(report_id, callback.from_user.id):
        await callback.message.edit_text(f"✅ Заявка #{report_id} одобрена. Скамер добавлен в базу.")
        await callback.answer()
    else:
        await callback.answer("Ошибка при одобрении", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("reject_scam_"))
async def reject_scam(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    report_id = int(callback.data.split("_")[2])
    reject_scam_report(report_id, "Отклонено админом")
    await callback.message.edit_text(f"❌ Заявка #{report_id} отклонена.")
    await callback.answer()

# ===================== АДМИН-ПАНЕЛЬ (ОСНОВНОЕ МЕНЮ) =====================
async def admin_panel(message: Message, actor_id: int = None, edit_only: bool = False):
    check_user_id = actor_id if actor_id is not None else (message.from_user.id if message.from_user else 0)
    if not is_admin(check_user_id):
        try:
            await message.edit_text("⛔ Нет доступа.")
        except TelegramBadRequest:
            if not edit_only:
                await message.answer("⛔ Нет доступа.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Управление гарантами", callback_data="admin_guarantors")],
        [InlineKeyboardButton(text="📊 Уровни и опыт", callback_data="admin_levels")],
        [InlineKeyboardButton(text="🏪 Управление магазином", callback_data="admin_shop_manage")],
        [InlineKeyboardButton(text="🔞 Модерация", callback_data="admin_moderation")],
        [InlineKeyboardButton(text="✅ Одобрение префиксов", callback_data="admin_prefixes")],
        [InlineKeyboardButton(text="👥 Управление пользователями", callback_data="admin_users")],
        [InlineKeyboardButton(text="🚫 Скам-база", callback_data="admin_scammers")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки VIP", callback_data="admin_vip_settings")],
        [InlineKeyboardButton(text="📝 Управление отзывами", callback_data="admin_feedbacks")],
        [InlineKeyboardButton(text="🔗 Внешние скам-боты", callback_data="admin_external_bots")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")],
    ])
    try:
        await message.edit_text("👑 Админ-панель:", reply_markup=keyboard)
    except TelegramBadRequest:
        if not edit_only:
            await message.answer("👑 Админ-панель:", reply_markup=keyboard)

# ----- Управление гарантами -----
@dp.callback_query(lambda c: c.data == "admin_guarantors")
async def admin_guarantors(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, level, is_active, current_deal_id, total_deals, rating, feedback_count, vip_chat_id, vip_invite_link, commission_rate, max_deal_amount, max_concurrent_deals FROM guarantors"
    )
    rows = cur.fetchall()
    conn.close()
    text = "👑 Список гарантов:\n\n"
    if not rows:
        text += "Нет гарантов."
    for row in rows:
        active_deals = get_active_deals_count_for_guarantor(row[0])
        amount_limit_text = f"{row[10]} USDT" if row[10] and row[10] > 0 else "без лимита"
        vip_link_mark = "✅" if row[8] else "❌"
        text += f"ID: {row[0]} | Активен: {'Да' if row[2] else 'Нет'} | Сделок всего: {row[4]}\n"
        text += f"Активных: {active_deals}/{row[11]} | Лимит суммы: {amount_limit_text}\n"
        text += f"Рейтинг: {row[5]:.2f} (отзывов: {row[6]}) | VIP-чат: {row[7] or 'не задан'} | VIP-ссылка: {vip_link_mark} | Комиссия: {row[9]*100}%\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить гаранта", callback_data="admin_add_guarantor")],
        [InlineKeyboardButton(text="❌ Удалить гаранта", callback_data="admin_remove_guarantor")],
        [InlineKeyboardButton(text="🏢 Настроить VIP офис", callback_data="admin_set_guarantor_vip")],
        [InlineKeyboardButton(text="💰 Лимит суммы сделки", callback_data="admin_set_guarantor_amount_limit")],
        [InlineKeyboardButton(text="📦 Лимит одновременных сделок", callback_data="admin_set_guarantor_concurrency_limit")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_guarantor")
async def add_guarantor_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID пользователя (число):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_guarantor_id)
    await callback.answer()

@dp.message(AdminStates.waiting_for_guarantor_id)
async def process_add_guarantor(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    user = get_user(user_id)
    if not user:
        await message.answer("❌ Пользователь не найден.")
        return
    add_guarantor(user_id)
    await state.clear()
    await message.answer(f"✅ Гарант @{user[1] or 'без username'} добавлен.")

@dp.callback_query(lambda c: c.data == "admin_remove_guarantor")
async def remove_guarantor_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите ID пользователя, которого хотите удалить из гарантов:",
        reply_markup=get_state_back_keyboard(admin_flow=True)
    )
    await state.set_state(AdminStates.waiting_for_remove_guarantor)
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_guarantor)
async def process_remove_guarantor(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    if not is_guarantor(user_id):
        await message.answer("❌ Этот пользователь не является гарантом.")
        return
    remove_guarantor(user_id)
    await state.clear()
    await message.answer("✅ Гарант удалён.")

@dp.callback_query(lambda c: c.data == "admin_set_guarantor_vip")
async def set_guarantor_vip_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите: ID_гаранта invite_link chat_id\n"
        "Пример: 123456789 https://t.me/+abc123 -1001234567890",
        reply_markup=get_state_back_keyboard(admin_flow=True)
    )
    await state.set_state(AdminStates.waiting_for_guarantor_vip)
    await callback.answer()

@dp.message(AdminStates.waiting_for_guarantor_vip)
async def process_set_guarantor_vip(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("❌ Формат: ID_гаранта invite_link chat_id")
        return
    try:
        user_id = int(parts[0])
    except ValueError:
        await message.answer("❌ ID гаранта должен быть числом.")
        return
    invite_link = parts[1].strip()
    if not (invite_link.startswith("http://") or invite_link.startswith("https://")):
        await message.answer("❌ Некорректная ссылка-приглашение.")
        return
    chat_ref = parts[2].strip()
    if chat_ref.startswith('@'):
        try:
            chat_obj = await bot.get_chat(chat_ref)
            chat_id = chat_obj.id
        except Exception as e:
            await message.answer(f"❌ Не удалось найти чат: {e}")
            return
    else:
        try:
            chat_id = int(chat_ref)
        except ValueError:
            await message.answer("❌ Некорректный chat_id.")
            return
    if not is_guarantor(user_id):
        await message.answer("❌ Пользователь не является гарантом.")
        return
    set_guarantor_vip_data(user_id, vip_chat_id=chat_id, vip_invite_link=invite_link)
    await state.clear()
    await message.answer(f"✅ Для гаранта {user_id} сохранён VIP-офис:\nчат {chat_id}\nссылка сохранена.")

@dp.callback_query(lambda c: c.data == "admin_set_guarantor_amount_limit")
async def set_guarantor_amount_limit_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите: ID_гаранта лимит_суммы_USDT\nПример: 123456789 1500\n0 = без лимита",
        reply_markup=get_state_back_keyboard(admin_flow=True)
    )
    await state.set_state(AdminStates.waiting_for_guarantor_amount_limit)
    await callback.answer()

@dp.message(AdminStates.waiting_for_guarantor_amount_limit)
async def process_guarantor_amount_limit(message: Message, state: FSMContext):
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("❌ Формат: ID лимит_суммы")
        return
    try:
        user_id = int(parts[0])
        limit_amount = float(parts[1].replace(',', '.'))
        if limit_amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверные значения.")
        return
    if not is_guarantor(user_id):
        await message.answer("❌ Пользователь не является гарантом.")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET max_deal_amount = ? WHERE user_id = ?", (limit_amount, user_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Лимит суммы для гаранта {user_id} установлен: {limit_amount} USDT")

@dp.callback_query(lambda c: c.data == "admin_set_guarantor_concurrency_limit")
async def set_guarantor_concurrency_limit_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите: ID_гаранта лимит_одновременных_сделок\nПример: 123456789 3",
        reply_markup=get_state_back_keyboard(admin_flow=True)
    )
    await state.set_state(AdminStates.waiting_for_guarantor_concurrency_limit)
    await callback.answer()

@dp.message(AdminStates.waiting_for_guarantor_concurrency_limit)
async def process_guarantor_concurrency_limit(message: Message, state: FSMContext):
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("❌ Формат: ID лимит")
        return
    try:
        user_id = int(parts[0])
        max_concurrent = int(parts[1])
        if max_concurrent <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Лимит должен быть положительным числом.")
        return
    if not is_guarantor(user_id):
        await message.answer("❌ Пользователь не является гарантом.")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET max_concurrent_deals = ? WHERE user_id = ?", (max_concurrent, user_id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Лимит одновременных сделок для гаранта {user_id}: {max_concurrent}")

# ----- Управление уровнями -----
@dp.callback_query(lambda c: c.data == "admin_levels")
async def admin_levels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    levels = get_levels()
    text = "📊 Текущие уровни:\n"
    for name, xp, bonus, comm, noq in levels:
        text += f"{name} — {xp} XP, бонус: {bonus} монет, комиссия: {comm*100}%, без очереди: {'Да' if noq else 'Нет'}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить уровень", callback_data="admin_add_level")],
        [InlineKeyboardButton(text="❌ Удалить уровень", callback_data="admin_del_level")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_level")
async def add_level_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название нового уровня:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_level_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_level_name)
async def process_level_name(message: Message, state: FSMContext):
    await state.update_data(level_name=message.text)
    await message.answer("Введите требуемое количество XP:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_level_xp)

@dp.message(AdminStates.waiting_for_level_xp)
async def process_level_xp(message: Message, state: FSMContext):
    try:
        xp = int(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(level_xp=xp)
    await message.answer("Введите бонусные монеты за достижение уровня:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_level_bonus)

@dp.message(AdminStates.waiting_for_level_bonus)
async def process_level_bonus(message: Message, state: FSMContext):
    try:
        bonus = int(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(level_bonus=bonus)
    await message.answer("Введите комиссию (в долях, например 0.02 = 2%):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_level_commission)

@dp.message(AdminStates.waiting_for_level_commission)
async def process_level_commission(message: Message, state: FSMContext):
    try:
        comm = float(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(level_commission=comm)
    await message.answer("Даёт ли уровень привилегию 'без очереди'? (1 - да, 0 - нет):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_level_noqueue)

@dp.message(AdminStates.waiting_for_level_noqueue)
async def process_level_noqueue(message: Message, state: FSMContext):
    try:
        noq = int(message.text)
    except:
        await message.answer("❌ Введите 0 или 1.")
        return
    data = await state.get_data()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO levels (name, xp_required, bonus_coins, commission_rate, no_queue) VALUES (?, ?, ?, ?, ?)",
        (data['level_name'], data['level_xp'], data['level_bonus'], data['level_commission'], noq)
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Уровень добавлен!")

@dp.callback_query(lambda c: c.data == "admin_del_level")
async def del_level_start(callback: CallbackQuery, state: FSMContext):
    levels = get_levels()
    if len(levels) <= 1:
        await callback.answer("Нельзя удалить единственный уровень", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"del_level_{name}")] for name, _, _, _, _ in levels
    ])
    await callback.message.edit_text("Выберите уровень для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("del_level_"))
async def confirm_del_level(callback: CallbackQuery):
    name = callback.data.split("_", 2)[2]
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM levels WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Уровень '{name}' удалён.")
    await admin_levels(callback)

# ----- Управление магазином -----
@dp.callback_query(lambda c: c.data == "admin_shop_manage")
async def admin_shop_manage(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_item")],
        [InlineKeyboardButton(text="❌ Удалить товар", callback_data="admin_remove_item")],
        [InlineKeyboardButton(text="📋 Список товаров", callback_data="admin_list_items")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text("🏪 Управление магазином:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_item")
async def add_item_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название товара:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_item_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_item_name)
async def process_item_name(message: Message, state: FSMContext):
    await state.update_data(item_name=message.text)
    await message.answer("Введите описание товара:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_item_desc)

@dp.message(AdminStates.waiting_for_item_desc)
async def process_item_desc(message: Message, state: FSMContext):
    await state.update_data(item_desc=message.text)
    await message.answer("Введите цену в монетах:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_item_price)

@dp.message(AdminStates.waiting_for_item_price)
async def process_item_price(message: Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    await state.update_data(item_price=price)
    await message.answer("Введите тип товара (prefix / role / discount):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_item_type)

@dp.message(AdminStates.waiting_for_item_type)
async def process_item_type(message: Message, state: FSMContext):
    typ = message.text.lower()
    if typ not in ['prefix', 'role', 'discount']:
        await message.answer("❌ Тип должен быть: prefix, role или discount.")
        return
    await state.update_data(item_type=typ)
    await message.answer("Введите значение товара (например, для prefix это текст префикса):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_item_value)

@dp.message(AdminStates.waiting_for_item_value)
async def process_item_value(message: Message, state: FSMContext):
    value = message.text
    data = await state.get_data()
    add_store_item(data['item_name'], data['item_desc'], data['item_price'], data['item_type'], value)
    await state.clear()
    await message.answer("✅ Товар добавлен!")

@dp.callback_query(lambda c: c.data == "admin_remove_item")
async def remove_item_start(callback: CallbackQuery, state: FSMContext):
    items = get_store_items()
    if not items:
        await callback.answer("Нет товаров для удаления", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{item[1]} (ID: {item[0]})", callback_data=f"remove_item_{item[0]}")] for item in items
    ])
    await callback.message.edit_text("Выберите товар для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("remove_item_"))
async def confirm_remove_item(callback: CallbackQuery):
    item_id = int(callback.data.split("_")[2])
    remove_store_item(item_id)
    await callback.message.edit_text(f"✅ Товар удалён.")
    await admin_shop_manage(callback)

@dp.callback_query(lambda c: c.data == "admin_list_items")
async def admin_list_items(callback: CallbackQuery):
    items = get_store_items()
    if not items:
        text = "📭 Магазин пуст."
    else:
        text = "📋 Список товаров:\n\n"
        for item in items:
            text += f"ID: {item[0]} | {item[1]} - {item[3]} монет\n  {item[2]}\n  Тип: {item[4]}, Значение: {item[5]}\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_shop_manage")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ----- Управление модерацией -----
@dp.callback_query(lambda c: c.data == "admin_moderation")
async def admin_moderation(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    moderation_enabled = get_setting('moderation_enabled') == '1'
    filter_links = get_setting('filter_links') == '1'
    filter_badwords = get_setting('filter_badwords') == '1'
    filter_spam = get_setting('filter_spam') == '1'
    xp_per_message = get_setting('xp_per_message') or '1'
    text = (
        f"🔞 Настройки модерации:\n"
        f"Модерация включена: {'Да' if moderation_enabled else 'Нет'}\n"
        f"Фильтр ссылок: {'Да' if filter_links else 'Нет'}\n"
        f"Фильтр мата: {'Да' if filter_badwords else 'Нет'}\n"
        f"Фильтр спама: {'Да' if filter_spam else 'Нет'}\n"
        f"XP за сообщение: {xp_per_message}\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Переключить модерацию", callback_data="admin_toggle_moderation")],
        [InlineKeyboardButton(text="🔄 Переключить фильтр ссылок", callback_data="admin_toggle_links")],
        [InlineKeyboardButton(text="🔄 Переключить фильтр мата", callback_data="admin_toggle_badwords")],
        [InlineKeyboardButton(text="📝 Управление чёрным списком", callback_data="admin_blacklist")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_toggle_moderation")
async def toggle_moderation(callback: CallbackQuery):
    val = get_setting('moderation_enabled')
    new_val = '0' if val == '1' else '1'
    set_setting('moderation_enabled', new_val)
    await admin_moderation(callback)

@dp.callback_query(lambda c: c.data == "admin_toggle_links")
async def toggle_links(callback: CallbackQuery):
    val = get_setting('filter_links')
    new_val = '0' if val == '1' else '1'
    set_setting('filter_links', new_val)
    await admin_moderation(callback)

@dp.callback_query(lambda c: c.data == "admin_toggle_badwords")
async def toggle_badwords(callback: CallbackQuery):
    val = get_setting('filter_badwords')
    new_val = '0' if val == '1' else '1'
    set_setting('filter_badwords', new_val)
    await admin_moderation(callback)

@dp.callback_query(lambda c: c.data == "admin_blacklist")
async def admin_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    words = get_badwords()
    text = "📋 Чёрный список слов:\n" + ("\n".join(words) if words else "Список пуст.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить слово", callback_data="admin_add_badword")],
        [InlineKeyboardButton(text="❌ Удалить слово", callback_data="admin_remove_badword")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_moderation")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_badword")
async def add_badword_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите слово для добавления в чёрный список:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_badword)
    await callback.answer()

@dp.message(AdminStates.waiting_for_badword)
async def process_add_badword(message: Message, state: FSMContext):
    word = message.text.lower().strip()
    add_badword(word)
    await state.clear()
    await message.answer(f"✅ Слово '{word}' добавлено в чёрный список.")

@dp.callback_query(lambda c: c.data == "admin_remove_badword")
async def remove_badword_start(callback: CallbackQuery):
    words = get_badwords()
    if not words:
        await callback.answer("Список пуст", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=word, callback_data=f"remove_badword_{word}")] for word in words
    ])
    await callback.message.edit_text("Выберите слово для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("remove_badword_"))
async def confirm_remove_badword(callback: CallbackQuery):
    word = callback.data.split("_", 2)[2]
    remove_badword(word)
    await callback.message.edit_text(f"✅ Слово '{word}' удалено из чёрного списка.")
    await admin_blacklist(callback)

# ----- Одобрение префиксов -----
@dp.callback_query(lambda c: c.data == "admin_prefixes")
async def admin_prefixes(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    pending = get_pending_prefix_requests()
    if not pending:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")]
        ])
        await callback.message.edit_text("📭 Нет заявок на префиксы.", reply_markup=keyboard)
        await callback.answer()
        return
    text = "✅ Заявки на префиксы:\n\n"
    for req in pending:
        user = get_user(req[1])
        username = user[1] if user else 'без username'
        comment = req[3] or "-"
        text += f"ID: {req[0]} | Пользователь: @{username} | Префикс: {req[2]}\nКомментарий: {comment}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Одобрить", callback_data=f"approve_prefix_{req[0]}"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"reject_prefix_{req[0]}")] for req in pending
    ])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("approve_prefix_"))
async def approve_prefix(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_id = int(callback.data.split("_")[2])
    await state.update_data(prefix_request_id=request_id)
    await callback.message.edit_text(
        "Введите комментарий к одобрению (или '-' без комментария):",
        reply_markup=get_state_back_keyboard(admin_flow=True)
    )
    await state.set_state(AdminStates.waiting_for_prefix_approve)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("reject_prefix_"))
async def reject_prefix(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_id = int(callback.data.split("_")[2])
    await state.update_data(prefix_request_id=request_id)
    await callback.message.edit_text(
        "Введите причину отклонения (или '-' без комментария):",
        reply_markup=get_state_back_keyboard(admin_flow=True)
    )
    await state.set_state(AdminStates.waiting_for_prefix_reject)
    await callback.answer()

@dp.message(AdminStates.waiting_for_prefix_approve)
async def process_prefix_approve_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    request_id = data.get("prefix_request_id")
    if not request_id:
        await state.clear()
        await message.answer("❌ Не найдена заявка.")
        return
    comment = "" if message.text.strip() == "-" else message.text.strip()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM prefix_requests WHERE id = ?", (request_id,))
    row = cur.fetchone()
    conn.close()
    if approve_prefix_request(request_id, comment):
        if row:
            try:
                await bot.send_message(row[0], f"✅ Ваша заявка на префикс одобрена.\nКомментарий админа: {comment or '—'}")
            except Exception:
                pass
        await message.answer("✅ Префикс одобрен.")
    else:
        await message.answer("❌ Ошибка при одобрении заявки.")
    await state.clear()

@dp.message(AdminStates.waiting_for_prefix_reject)
async def process_prefix_reject_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    request_id = data.get("prefix_request_id")
    if not request_id:
        await state.clear()
        await message.answer("❌ Не найдена заявка.")
        return
    comment = "" if message.text.strip() == "-" else message.text.strip()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM prefix_requests WHERE id = ?", (request_id,))
    row = cur.fetchone()
    conn.close()
    reject_prefix_request(request_id, comment)
    if row:
        try:
            await bot.send_message(row[0], f"❌ Ваша заявка на префикс отклонена.\nКомментарий админа: {comment or '—'}")
        except Exception:
            pass
    await message.answer("❌ Префикс отклонён.")
    await state.clear()

# ----- Управление пользователями -----
@dp.callback_query(lambda c: c.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="📊 Топ пользователей", callback_data="admin_top_users")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text("👥 Управление пользователями:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_search_user")
async def search_user_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username или ID пользователя:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_search)
async def process_search_user(message: Message, state: FSMContext):
    query = message.text.strip()
    if query.startswith('@'):
        username = query.lstrip('@')
        user = get_user_by_username(username)
        if user:
            await show_user_info(message, user)
        else:
            await message.answer("❌ Пользователь не найден.")
    else:
        try:
            user_id = int(query)
            user = get_user(user_id)
            if user:
                await show_user_info(message, user)
            else:
                await message.answer("❌ Пользователь не найден.")
        except:
            await message.answer("❌ Введите корректный username или ID.")
    await state.clear()

async def show_user_info(message: Message, user):
    text = (
        f"👤 Информация о пользователе @{user[1] or 'без username'}\n"
        f"ID: {user[0]}\n"
        f"Уровень: {user[3]}\n"
        f"Опыт: {user[4]} XP (всего {user[5]})\n"
        f"Монеты: {user[9]}\n"
        f"Предупреждения: {user[10]}\n"
        f"Рефералов: {user[14] or 0}\n"
        f"Активный префикс: {user[15] or 'нет'}\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Начислить XP", callback_data=f"admin_addxp_{user[0]}")],
        [InlineKeyboardButton(text="💰 Начислить монеты", callback_data=f"admin_addcoins_{user[0]}")],
        [InlineKeyboardButton(text="📊 Сменить уровень", callback_data=f"admin_setlevel_{user[0]}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users")],
    ])
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("admin_addxp_"))
async def admin_addxp_start(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user_id=user_id)
    await callback.message.edit_text("Введите количество XP для начисления:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_user_xp)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_xp)
async def process_addxp(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    user_id = data['target_user_id']
    add_xp(user_id, amount)
    await state.clear()
    await message.answer(f"✅ Начислено {amount} XP.")

@dp.callback_query(lambda c: c.data.startswith("admin_addcoins_"))
async def admin_addcoins_start(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user_id=user_id)
    await callback.message.edit_text("Введите количество монет для начисления:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_user_coins)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_coins)
async def process_addcoins(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    user_id = data['target_user_id']
    add_coins(user_id, amount)
    await state.clear()
    await message.answer(f"✅ Начислено {amount} монет.")

@dp.callback_query(lambda c: c.data.startswith("admin_setlevel_"))
async def admin_setlevel_start(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user_id=user_id)
    levels = get_levels()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"setlevel_{user_id}_{name}")] for name, _, _, _, _ in levels
    ])
    await callback.message.edit_text("Выберите новый уровень:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("setlevel_"))
async def process_setlevel(callback: CallbackQuery):
    parts = callback.data.split("_", 2)
    user_id = int(parts[1])
    level_name = parts[2]
    update_user_field(user_id, 'level', level_name)
    await callback.message.edit_text(f"✅ Уровень изменён на {level_name}.")
    await admin_users(callback)

@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await admin_panel(message)

@dp.message(Command("setadminrank"))
async def set_admin_rank_cmd(message: Message, command: CommandObject = None):
    if message.from_user.id not in OWNER_IDS:
        await message.answer("⛔ Только владелец может менять ранги админов.")
        return
    if not command or not command.args:
        await message.answer("❌ Используйте: /setadminrank <user_id> <2|3>")
        return
    parts = command.args.split()
    if len(parts) != 2:
        await message.answer("❌ Используйте: /setadminrank <user_id> <2|3>")
        return
    try:
        user_id = int(parts[0])
        rank = int(parts[1])
    except ValueError:
        await message.answer("❌ Неверный формат. user_id и rank должны быть числами.")
        return
    if rank not in (2, 3):
        await message.answer("❌ Можно назначить только ранг 2 или 3. Ранг 1 — только владельцы из owner_ids.")
        return
    set_admin_rank(user_id, rank)
    await message.answer(f"✅ Пользователю {user_id} назначен админ-ранг {rank}.")

@dp.message(Command("deladminrank"))
async def del_admin_rank_cmd(message: Message, command: CommandObject = None):
    if message.from_user.id not in OWNER_IDS:
        await message.answer("⛔ Только владелец может снимать ранги админов.")
        return
    if not command or not command.args:
        await message.answer("❌ Используйте: /deladminrank <user_id>")
        return
    try:
        user_id = int(command.args.split()[0])
    except ValueError:
        await message.answer("❌ Неверный user_id.")
        return
    if user_id in OWNER_IDS:
        await message.answer("⛔ Владелец задаётся только в owner_ids конфигурации.")
        return
    remove_admin_rank(user_id)
    await message.answer(f"✅ Админ-ранг для {user_id} удалён.")

@dp.callback_query(lambda c: c.data == "admin_top_users")
async def admin_top_users(callback: CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, level, total_xp, coins FROM users ORDER BY total_xp DESC LIMIT 10")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        text = "Нет пользователей."
    else:
        text = "🏆 Топ-10 пользователей по опыту:\n\n"
        for i, row in enumerate(rows, 1):
            text += f"{i}. @{row[1] or 'без username'} | Уровень: {row[2]} | XP: {row[3]} | Монет: {row[4]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ----- Управление скам-базой -----
@dp.callback_query(lambda c: c.data == "admin_scammers")
async def admin_scammers(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, username, evidence, added_at FROM scammers ORDER BY id DESC LIMIT 20")
    rows = cur.fetchall()
    conn.close()
    text = "🚫 Скам-база:\n\n"
    if not rows:
        text += "Нет записей."
    else:
        for row in rows:
            text += f"ID: {row[0]} | @{row[1]} | {row[3]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить скамера", callback_data="admin_add_scammer")],
        [InlineKeyboardButton(text="❌ Удалить скамера", callback_data="admin_remove_scammer")],
        [InlineKeyboardButton(text="📋 Ожидающие заявки", callback_data="admin_pending_scam_reports")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_scammer")
async def add_scammer_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username скамера:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_scammer_username)
    await callback.answer()

@dp.message(AdminStates.waiting_for_scammer_username)
async def process_scammer_username(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    await state.update_data(scammer_username=username)
    await message.answer("Введите доказательства (текст или ссылки):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_scammer_evidence)

@dp.message(AdminStates.waiting_for_scammer_evidence)
async def process_scammer_evidence(message: Message, state: FSMContext):
    evidence = message.text
    data = await state.get_data()
    username = data['scammer_username']
    add_scammer(username, evidence, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Скамер @{username} добавлен в базу.")

@dp.callback_query(lambda c: c.data == "admin_remove_scammer")
async def remove_scammer_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username скамера для удаления:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_remove_scammer)
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_scammer)
async def process_remove_scammer(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    remove_scammer(username)
    await state.clear()
    await message.answer(f"✅ Скамер @{username} удалён из базы.")

@dp.callback_query(lambda c: c.data == "admin_pending_scam_reports")
async def admin_pending_scam_reports(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reports = get_pending_scam_reports()
    if not reports:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_scammers")]
        ])
        await callback.message.edit_text("📭 Нет ожидающих заявок на скамеров.", reply_markup=keyboard)
        await callback.answer()
        return
    text = "📋 Ожидающие заявки:\n\n"
    for rep in reports:
        text += f"ID: {rep[0]} | @{rep[1]} | От: {rep[3]} | {rep[4]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_scam_{rep[0]}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_scam_{rep[0]}")] for rep in reports
    ])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_scammers")])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ----- Рассылка -----
@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("📢 Введите текст для рассылки (можно с Markdown):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    text = message.text
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    conn.close()
    count = 0
    for user in users:
        try:
            await bot.send_message(user[0], text, parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await state.clear()
    await message.answer(f"✅ Рассылка завершена. Отправлено {count} пользователям.")

# ----- Настройки VIP -----
@dp.callback_query(lambda c: c.data == "admin_vip_settings")
async def admin_vip_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current_amount = get_setting('vip_threshold_amount') or '500'
    current_level = get_setting('vip_threshold_level') or 'Эксперт'
    text = f"⚙️ Настройки VIP:\nПорог суммы: {current_amount} USDT\nПорог уровня: {current_level}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Изменить порог суммы", callback_data="admin_set_vip_amount")],
        [InlineKeyboardButton(text="📊 Изменить порог уровня", callback_data="admin_set_vip_level")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_set_vip_amount")
async def set_vip_amount_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новый порог суммы для VIP (в USDT):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_vip_threshold_amount)
    await callback.answer()

@dp.message(AdminStates.waiting_for_vip_threshold_amount)
async def process_vip_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    set_setting('vip_threshold_amount', str(amount))
    await state.clear()
    await message.answer(f"✅ Порог суммы установлен: {amount} USDT")

@dp.callback_query(lambda c: c.data == "admin_set_vip_level")
async def set_vip_level_start(callback: CallbackQuery, state: FSMContext):
    levels = get_levels()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"set_vip_level_{name}")] for name, _, _, _, _ in levels
    ])
    await callback.message.edit_text("Выберите пороговый уровень для VIP:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("set_vip_level_"))
async def process_set_vip_level(callback: CallbackQuery):
    level_name = callback.data.split("_", 3)[3]
    set_setting('vip_threshold_level', level_name)
    await callback.message.edit_text(f"✅ Порог уровня установлен: {level_name}")
    await admin_vip_settings(callback)

# ----- Управление отзывами -----
@dp.callback_query(lambda c: c.data == "admin_feedbacks")
async def admin_feedbacks(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, from_user, to_user, rating, text, timestamp FROM feedbacks WHERE deleted = 0 ORDER BY timestamp DESC LIMIT 10")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        text = "📭 Нет отзывов."
    else:
        text = "📝 Последние отзывы:\n\n"
        for row in rows:
            from_label = format_user_label(row[1])
            to_label = format_user_label(row[2])
            text += (
                f"ID: {row[0]} | От: {from_label} | Кому: {to_label} | Оценка: {'⭐'*row[3]}\n"
                f"Когда: {row[5]}\n"
                f"Текст: {row[4]}\n---\n"
            )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить отзыв", callback_data="admin_delete_feedback")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_delete_feedback")
async def delete_feedback_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID отзыва для удаления:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_feedback_delete)
    await callback.answer()

@dp.message(AdminStates.waiting_for_feedback_delete)
async def process_delete_feedback(message: Message, state: FSMContext):
    try:
        fb_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    delete_feedback(fb_id)
    await state.clear()
    await message.answer(f"✅ Отзыв #{fb_id} удалён.")

# ----- Внешние скам-боты -----
@dp.callback_query(lambda c: c.data == "admin_external_bots")
async def admin_external_bots(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    bots = get_external_bots()
    text = "🔗 Внешние скам-боты:\n\n"
    if not bots:
        text += "Нет подключённых ботов."
    else:
        for bot_data in bots:
            text += f"ID: {bot_data[0]} | {bot_data[1]} (@{bot_data[2]}) | API: {bot_data[3]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота", callback_data="admin_add_external_bot")],
        [InlineKeyboardButton(text="❌ Удалить бота", callback_data="admin_remove_external_bot")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_external_bot")
async def add_external_bot_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название бота:", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_external_bot_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_external_bot_name)
async def process_ext_bot_name(message: Message, state: FSMContext):
    await state.update_data(ext_name=message.text)
    await message.answer("Введите username бота (например, @ScamBot):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_external_bot_username)

@dp.message(AdminStates.waiting_for_external_bot_username)
async def process_ext_bot_username(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    await state.update_data(ext_username=username)
    await message.answer("Введите API URL (например, https://scambot.com/api/check):", reply_markup=get_state_back_keyboard(admin_flow=True))
    await state.set_state(AdminStates.waiting_for_external_bot_api)

@dp.message(AdminStates.waiting_for_external_bot_api)
async def process_ext_bot_api(message: Message, state: FSMContext):
    api_url = message.text.strip()
    data = await state.get_data()
    add_external_bot(data['ext_name'], data['ext_username'], api_url)
    await state.clear()
    await message.answer("✅ Внешний бот добавлен!")

@dp.callback_query(lambda c: c.data == "admin_remove_external_bot")
async def remove_external_bot_start(callback: CallbackQuery):
    bots = get_external_bots()
    if not bots:
        await callback.answer("Нет ботов для удаления", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{b[1]} (ID: {b[0]})", callback_data=f"remove_extbot_{b[0]}")] for b in bots
    ])
    await callback.message.edit_text("Выберите бота для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("remove_extbot_"))
async def confirm_remove_extbot(callback: CallbackQuery):
    bot_id = int(callback.data.split("_")[2])
    remove_external_bot(bot_id)
    await callback.message.edit_text("✅ Бот удалён.")
    await admin_external_bots(callback)

# ===================== МОДЕРАЦИЯ =====================
def resolve_target_from_message(message: Message, token: Optional[str]):
    if message.reply_to_message and message.reply_to_message.from_user:
        reply_user = message.reply_to_message.from_user
        create_user_if_not_exists(reply_user.id, reply_user.username, reply_user.full_name)
        return get_user(reply_user.id)
    return resolve_target_user(token or "")

@dp.message(Command("pred", "warn"))
async def warn_user_command(message: Message, command: CommandObject = None):
    if not has_admin_permission(message.from_user.id, 3):
        await message.answer("⛔ Нет доступа.")
        return
    args_text = (command.args or "").strip() if command else ""
    parts = args_text.split(maxsplit=1) if args_text else []
    if message.reply_to_message:
        target = resolve_target_from_message(message, None)
        reason = args_text or "Без причины"
    else:
        if not parts:
            await message.answer("❌ Используйте: /pred @username причина (или ответом на сообщение).")
            return
        target = resolve_target_from_message(message, parts[0])
        reason = parts[1].strip() if len(parts) > 1 else "Без причины"
    if not target or not reason:
        await message.answer("❌ Укажите пользователя и причину.")
        return
    if target[0] in OWNER_IDS:
        await message.answer("⛔ Нельзя выдать предупреждение владельцу.")
        return
    update_user_field(target[0], 'warnings_count', (target[10] or 0) + 1)
    log_moderation_action("warn", target[0], message.from_user.id, reason, 0, message.chat.id)
    await message.answer(f"⚠️ Пользователь @{target[1] or target[0]} получил предупреждение.\nПричина: {reason}")

@dp.message(Command("mute"))
async def mute_user_command(message: Message, command: CommandObject = None):
    if not has_admin_permission(message.from_user.id, 3):
        await message.answer("⛔ Нет доступа.")
        return
    args_text = (command.args or "").strip() if command else ""
    parts = args_text.split(maxsplit=2) if args_text else []
    if message.reply_to_message:
        if len(parts) == 0:
            duration_seconds = 30 * 60
            reason = "Без причины"
        elif len(parts) == 1:
            duration_seconds = parse_duration_to_seconds(parts[0])
            reason = "Без причины"
        else:
            duration_seconds = parse_duration_to_seconds(parts[0])
            reason = parts[1].strip() or "Без причины"
        target = resolve_target_from_message(message, None)
    else:
        if len(parts) < 3:
            await message.answer("❌ Формат: /mute @username 30m причина")
            return
        target = resolve_target_from_message(message, parts[0])
        duration_seconds = parse_duration_to_seconds(parts[1])
        reason = parts[2].strip()
    if not target or duration_seconds is None or not reason:
        await message.answer("❌ Проверьте пользователя, время и причину.")
        return
    if target[0] in OWNER_IDS:
        await message.answer("⛔ Нельзя мутить владельца.")
        return
    mute_until = datetime.now() + timedelta(seconds=duration_seconds)
    update_user_field(target[0], 'is_muted', 1)
    update_user_field(target[0], 'mute_until', mute_until.isoformat())
    log_moderation_action("mute", target[0], message.from_user.id, reason, duration_seconds, message.chat.id)
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target[0],
            permissions=ChatPermissions(can_send_messages=False),
            until_date=mute_until
        )
    except Exception as e:
        logging.warning(f"Mute в чате не применён: {e}")
    await message.answer(
        f"🔇 Пользователь @{target[1] or target[0]} замучен до {mute_until.strftime('%Y-%m-%d %H:%M')}.\nПричина: {reason}"
    )

@dp.message(Command("ban"))
async def ban_user_command(message: Message, command: CommandObject = None):
    if not has_admin_permission(message.from_user.id, 2):
        await message.answer("⛔ Нет доступа.")
        return
    args_text = (command.args or "").strip() if command else ""
    parts = args_text.split(maxsplit=1) if args_text else []
    if message.reply_to_message:
        target = resolve_target_from_message(message, None)
        reason = args_text or "Без причины"
    else:
        if not parts:
            await message.answer("❌ Используйте: /ban @username причина (или ответом на сообщение).")
            return
        target = resolve_target_from_message(message, parts[0])
        reason = parts[1].strip() if len(parts) > 1 else "Без причины"
    if not target or not reason:
        await message.answer("❌ Укажите пользователя и причину.")
        return
    if target[0] in OWNER_IDS:
        await message.answer("⛔ Нельзя банить владельца.")
        return
    log_moderation_action("ban", target[0], message.from_user.id, reason, 0, message.chat.id)
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
    except Exception as e:
        logging.warning(f"Ban в чате не применён: {e}")
    await message.answer(f"🚫 Пользователь @{target[1] or target[0]} забанен.\nПричина: {reason}")

@dp.message(Command("unmute", "размут"))
async def unmute_user_command(message: Message, command: CommandObject = None):
    if not has_admin_permission(message.from_user.id, 3):
        await message.answer("⛔ Нет доступа.")
        return
    args_text = (command.args or "").strip() if command else ""
    if message.reply_to_message:
        target = resolve_target_from_message(message, None)
    else:
        if not args_text:
            await message.answer("❌ Используйте: /unmute @username (или ответом на сообщение).")
            return
        target = resolve_target_from_message(message, args_text.split()[0])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    update_user_field(target[0], 'is_muted', 0)
    update_user_field(target[0], 'mute_until', None)
    log_moderation_action("unmute", target[0], message.from_user.id, "Размут", 0, message.chat.id)
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target[0],
            permissions=ChatPermissions(can_send_messages=True)
        )
    except Exception as e:
        logging.warning(f"Unmute в чате не применён: {e}")
    await message.answer(f"🔊 Пользователь @{target[1] or target[0]} размучен.")

@dp.message(Command("unban", "разбан"))
async def unban_user_command(message: Message, command: CommandObject = None):
    if not has_admin_permission(message.from_user.id, 2):
        await message.answer("⛔ Нет доступа.")
        return
    args_text = (command.args or "").strip() if command else ""
    if message.reply_to_message:
        target = resolve_target_from_message(message, None)
    else:
        if not args_text:
            await message.answer("❌ Используйте: /unban @username (или ответом на сообщение).")
            return
        target = resolve_target_from_message(message, args_text.split()[0])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    log_moderation_action("unban", target[0], message.from_user.id, "Разбан", 0, message.chat.id)
    try:
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
    except Exception as e:
        logging.warning(f"Unban в чате не применён: {e}")
    await message.answer(f"✅ Пользователь @{target[1] or target[0]} разбанен.")

@dp.message(Command("reasons", "history"))
async def moderation_history_command(message: Message, command: CommandObject = None):
    if not await ensure_subscription_or_prompt(message, message.from_user.id):
        return
    if not command or not command.args:
        await message.answer("❌ Используйте: /history @username или /history ID")
        return
    target = resolve_target_user(command.args.split()[0])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    # Историю может смотреть сам пользователь, админ или гарант.
    if message.from_user.id != target[0] and not is_admin(message.from_user.id) and not is_guarantor(message.from_user.id):
        await message.answer("⛔ Нет доступа к чужой истории модерации.")
        return
    rows = get_moderation_history(target[0], 10)
    if not rows:
        await message.answer("📭 История модерации пуста.")
        return
    text = f"📚 История модерации @{target[1] or target[0]}:\n\n"
    for action_type, admin_user, reason, duration_seconds, created_at in rows:
        duration_text = ""
        if action_type == "mute" and duration_seconds:
            duration_text = f" | срок: {duration_seconds // 60} мин"
        text += f"• {action_type.upper()} | модератор: {admin_user}{duration_text}\nПричина: {reason}\n{created_at}\n\n"
    await message.answer(text)

@dp.message()
async def moderate_message(message: Message):
    if message.new_chat_members:
        await handle_new_member(message)
        return
    if message.from_user.is_bot or is_admin(message.from_user.id):
        return
    user = get_user(message.from_user.id)
    if user and user[11] == 1 and user[12]:
        try:
            mute_until = datetime.fromisoformat(user[12])
            if mute_until > datetime.now():
                await message.delete()
                return
            else:
                update_user_field(message.from_user.id, 'is_muted', 0)
                update_user_field(message.from_user.id, 'mute_until', None)
        except:
            pass
    content_text = (message.text or message.caption or "")
    content_text_lower = content_text.lower().replace("ё", "е")
    if get_setting('moderation_enabled') == '1':
        if get_setting('filter_links') == '1':
            if re.search(r'(https?://[^\s]+)', content_text):
                allowed = ALLOWED_LINK_DOMAINS or ['t.me', 'ton.org', 'telegram.org']
                if not any(dom in content_text_lower for dom in allowed):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await message.answer("🚫 Ссылки запрещены.")
                    return
        if get_setting('filter_badwords') == '1':
            badwords = get_badwords()
            for word in badwords:
                normalized_word = (word or "").strip().lower().replace("ё", "е")
                if not normalized_word:
                    continue
                pattern = rf"(?<!\w){re.escape(normalized_word)}(?!\w)"
                if re.search(pattern, content_text_lower):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await message.answer("🚫 Нецензурная лексика запрещена.")
                    return
    if get_setting('xp_per_message') and int(get_setting('xp_per_message')) > 0:
        add_xp(message.from_user.id, int(get_setting('xp_per_message')))
    configured_emoji_id = get_setting('premium_emoji_id') or ''
    emoji_hit = False
    if configured_emoji_id and message.entities:
        for ent in message.entities:
            if getattr(ent, "type", None) == "custom_emoji" and getattr(ent, "custom_emoji_id", "") == configured_emoji_id:
                emoji_hit = True
                break
    premium_xp = apply_premium_emoji_xp(message.from_user.id, emoji_hit)
    if premium_xp > 0:
        await message.reply(f"✨ +{premium_xp} XP за premium emoji")

@dp.chat_join_request()
async def handle_chat_join_request(join_request: types.ChatJoinRequest):
    chat_id = join_request.chat.id
    user_id = join_request.from_user.id
    if has_vip_access(chat_id, user_id):
        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logging.error(f"Не удалось одобрить заявку в VIP-чат: {e}")
        return
    try:
        await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logging.error(f"Не удалось отклонить неавторизованную заявку в VIP-чат: {e}")

# ===================== АВТОМАТИЧЕСКАЯ ПРОВЕРКА НОВЫХ УЧАСТНИКОВ =====================
async def handle_new_member(message: Message):
    if not message.new_chat_members:
        return
    for member in message.new_chat_members:
        username = member.username or ''
        if is_scammer(username):
            action = get_setting('scam_action') or 'mute'
            if action == 'ban':
                await bot.ban_chat_member(message.chat.id, member.id)
                await message.answer(f"🚫 Пользователь @{username} забанен (найден в скам-базе).")
            else:
                await bot.restrict_chat_member(message.chat.id, member.id, permissions=ChatPermissions(can_send_messages=False))
                await message.answer(f"🔇 Пользователь @{username} замучен (найден в скам-базе).")
            for admin_id in INITIAL_ADMINS:
                await bot.send_message(admin_id, f"🚨 Действие ({action}) применено к скамеру @{username} в чате {message.chat.title}")
            continue
        found, bot_name = await check_all_external_bots(username)
        if found:
            action = get_setting('scam_action') or 'mute'
            if action == 'ban':
                await bot.ban_chat_member(message.chat.id, member.id)
                await message.answer(f"🚫 Пользователь @{username} забанен (найден внешним ботом {bot_name}).")
            else:
                await bot.restrict_chat_member(message.chat.id, member.id, permissions=ChatPermissions(can_send_messages=False))
                await message.answer(f"🔇 Пользователь @{username} замучен (найден внешним ботом {bot_name}).")
            for admin_id in INITIAL_ADMINS:
                await bot.send_message(admin_id, f"🚨 Действие ({action}) применено к скамеру @{username} (внешний бот {bot_name}) в чате {message.chat.title}")

# ===================== КОМАНДА ДЛЯ ГАРАНТА: УСТАНОВИТЬ VIP-ЧАТ =====================
@dp.message(Command("setvipchat"))
async def set_vip_chat_command(message: Message):
    if not is_guarantor(message.from_user.id) and not is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна гарантам и администраторам.")
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "❌ Формат:\n"
            "/setvipchat <invite_link> <chat_id/@chat>  (для себя)\n"
            "/setvipchat <guarantor_id> <invite_link> <chat_id/@chat>  (для админа)"
        )
        return

    target_user_id = message.from_user.id
    arg_offset = 1
    if len(args) >= 4 and args[1].isdigit():
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Назначать VIP другим гарантам может только админ.")
            return
        target_user_id = int(args[1])
        arg_offset = 2

    if not is_guarantor(target_user_id):
        await message.answer("❌ Указанный пользователь не является гарантом.")
        return

    invite_link = args[arg_offset].strip()
    chat_ref = args[arg_offset + 1].strip()
    if not (invite_link.startswith("http://") or invite_link.startswith("https://")):
        await message.answer("❌ Сначала укажите корректную invite-ссылку.")
        return

    if chat_ref.startswith('@'):
        try:
            chat_obj = await bot.get_chat(chat_ref)
            chat_id = chat_obj.id
        except Exception as e:
            await message.answer(f"❌ Не удалось найти чат: {e}")
            return
    else:
        try:
            chat_id = int(chat_ref)
        except ValueError:
            await message.answer("❌ Укажите корректный chat_id или @chat.")
            return

    set_guarantor_vip_data(target_user_id, vip_chat_id=chat_id, vip_invite_link=invite_link)
    await message.answer(
        f"✅ VIP-офис гаранта {target_user_id} обновлён.\n"
        f"Чат: {chat_id}\n"
        "Ссылка сохранена."
    )

@dp.message(Command("setpremiumemoji"))
async def set_premium_emoji_command(message: Message, command: CommandObject = None):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not command or not command.args:
        await message.answer("❌ Используйте: /setpremiumemoji <custom_emoji_id>")
        return
    emoji_id = command.args.strip()
    set_setting("premium_emoji_id", emoji_id)
    await message.answer(f"✅ ID premium emoji сохранён: {emoji_id}")

@dp.message(Command("setpremiumxp"))
async def set_premium_xp_command(message: Message, command: CommandObject = None):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not command or not command.args:
        await message.answer("❌ Используйте: /setpremiumxp <base> <step> <max>")
        return
    parts = command.args.split()
    if len(parts) != 3:
        await message.answer("❌ Используйте: /setpremiumxp <base> <step> <max>")
        return
    try:
        base_xp = int(parts[0])
        growth_step = int(parts[1])
        max_xp = int(parts[2])
        if base_xp <= 0 or growth_step < 0 or max_xp < base_xp:
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректные значения.")
        return
    set_setting("premium_emoji_base_xp", str(base_xp))
    set_setting("premium_emoji_growth_step", str(growth_step))
    set_setting("premium_emoji_max_xp", str(max_xp))
    await message.answer(f"✅ XP premium emoji обновлён: base={base_xp}, step={growth_step}, max={max_xp}")

# ===================== ЗАПУСК =====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))).start()
    asyncio.run(main())