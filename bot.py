# Referral Bot — একটাই ফাইল (bot.py)
# নিয়ম: বটের নাম কোথাও লেখা নেই — getMe থেকে নেওয়া হয়।
# Env vars শুধু: BOT_TOKEN, ADMIN_ID
# Polling only, SQLite (referrals.db), কোনো বাইরের ডাটাবেস নেই।

import logging
import os
import sqlite3
import threading
from datetime import datetime

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "referrals.db")

DEFAULT_REWARD = 2      # প্রতি সফল রেফারে রেফারারের ব্যালান্সে যোগ হবে (টাকা)
DEFAULT_MIN_WITHDRAW = 50  # সর্বনিম্ন উইথড্র (টাকা)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            created_at TEXT NOT NULL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            method TEXT NOT NULL,
            account TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('reward', ?)",
        (str(DEFAULT_REWARD),),
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdraw', ?)",
        (str(DEFAULT_MIN_WITHDRAW),),
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('force_channel', '')"
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('support_id', '')"
    )
    # পুরনো ডাটাবেসে banned কলাম না থাকলে যোগ করা
    try:
        cur.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # কলাম আগেই আছে
    conn.commit()
    conn.close()


def get_setting(key, default):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return int(row["value"])
    except ValueError:
        return default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_str_setting(key, default=""):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    return row["value"]


def set_str_setting(key, value):
    set_setting(key, value)


def get_force_channel():
    """ফোর্স-জয়েন চ্যানেল — ইউজারনেম (@ ছাড়া) বা সংখ্যার ID, না থাকলে ''।"""
    return get_str_setting("force_channel", "").strip().lstrip("@")


def get_support_id():
    """সাপোর্ট আইডি — ইউজারনেম (@ ছাড়া) বা সংখ্যার ID, না থাকলে ''।"""
    return get_str_setting("support_id", "").strip().lstrip("@")


def is_banned(user_id):
    user = get_user(user_id)
    if user is None:
        return False
    try:
        return bool(user["banned"])
    except (KeyError, IndexError):
        return False


def set_banned(user_id, banned):
    """ইউজার থাকলে True, না থাকলে False।"""
    if get_user(user_id) is None:
        return False
    conn = get_conn()
    conn.execute(
        "UPDATE users SET banned = ? WHERE user_id = ?", (1 if banned else 0, user_id)
    )
    conn.commit()
    conn.close()
    return True


def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def add_user(user_id, username, first_name, referred_by=None):
    """নতুন ইউজার হলে True, আগের ইউজার হলে False।"""
    if get_user(user_id) is not None:
        return False
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (user_id, username, first_name, balance, referred_by, created_at)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        (user_id, username, first_name, referred_by,
         datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return True


def record_referral(referrer_id, referred_id):
    """সফল রেফারেল গুনে রেফারারের ব্যালান্সে রিওয়ার্ড যোগ করে। True = গোনা হয়েছে।"""
    if referrer_id == referred_id:
        return False  # নিজেকে রেফার করা যাবে না
    if get_user(referrer_id) is None:
        return False  # রেফারার বটে নেই
    conn = get_conn()
    exists = conn.execute(
        "SELECT id FROM referrals WHERE referred_id = ?", (referred_id,)
    ).fetchone()
    if exists is not None:
        conn.close()
        return False  # এই ইউজারের রেফারেল আগেই গোনা হয়েছে
    reward = get_setting("reward", DEFAULT_REWARD)
    conn.execute(
        "INSERT INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
        (referrer_id, referred_id, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.execute(
        "UPDATE users SET referred_by = ? WHERE user_id = ?",
        (referrer_id, referred_id),
    )
    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
        (reward, referrer_id),
    )
    conn.commit()
    conn.close()
    return True


def get_referral_count(user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def get_balance(user_id):
    user = get_user(user_id)
    return user["balance"] if user else 0


def add_balance(user_id, amount):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
    )
    conn.commit()
    conn.close()


def create_withdrawal(user_id, amount, method, account):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO withdrawals (user_id, amount, method, account, status, created_at)"
        " VALUES (?, ?, ?, ?, 'pending', ?)",
        (user_id, amount, method, account, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    wd_id = cur.lastrowid
    conn.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id)
    )
    conn.commit()
    conn.close()
    return wd_id


def get_withdrawal(wd_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM withdrawals WHERE id = ?", (wd_id,)
    ).fetchone()
    conn.close()
    return row


def set_withdrawal_status(wd_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE withdrawals SET status = ?, decided_at = ? WHERE id = ?",
        (status, datetime.now().strftime("%Y-%m-%d %H:%M"), wd_id),
    )
    conn.commit()
    conn.close()


def get_pending_withdrawals():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return rows


def get_stats():
    conn = get_conn()
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    total_refs = conn.execute("SELECT COUNT(*) AS c FROM referrals").fetchone()["c"]
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM withdrawals WHERE status = 'paid'"
    ).fetchone()["s"]
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM withdrawals WHERE status = 'pending'"
    ).fetchone()["c"]
    conn.close()
    return total_users, total_refs, paid, pending


def get_recent_users(limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_all_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users WHERE banned = 0").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def get_bot_info(context):
    """getMe থেকে বটের নাম + ইউজারনেম — কোথাও হার্ডকোড করা নেই।"""
    me = context.bot_data.get("me")
    if me is None:
        me = await context.bot.get_me()
        context.bot_data["me"] = me
    return me


def is_admin(user_id):
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def support_url():
    """সাপোর্ট আইডি থেকে লিংক বানায় — ইউজারনেম হলে t.me, সংখ্যা হলে tg://user।"""
    value = get_support_id()
    if not value:
        return None
    if value.isdigit():
        return f"tg://user?id={value}"
    return f"https://t.me/{value}"


def main_menu_keyboard():
    rows = [
        [InlineKeyboardButton("🔗 আমার রেফারেল লিংক", callback_data="menu_refer", style="primary")],
        [InlineKeyboardButton("💰 আমার ব্যালান্স", callback_data="menu_balance", style="primary")],
        [InlineKeyboardButton("🏦 উইথড্র করুন", callback_data="menu_withdraw", style="primary")],
    ]
    url = support_url()
    if url:
        rows.append([InlineKeyboardButton("💬 সাপোর্ট", url=url, style="primary")])
    return InlineKeyboardMarkup(rows)


def force_join_keyboard():
    channel = get_force_channel()
    rows = []
    if channel and not channel.isdigit():
        rows.append(
            [InlineKeyboardButton("📢 চ্যানেল জয়েন করুন", url=f"https://t.me/{channel}", style="primary")]
        )
    rows.append(
        [InlineKeyboardButton("✅ আমি জয়েন করেছি", callback_data="check_join", style="success")]
    )
    return InlineKeyboardMarkup(rows)


def force_join_text():
    channel = get_force_channel()
    name = f"@{channel}" if channel and not channel.isdigit() else "চ্যানেলটি"
    return (
        f"📢 বট ব্যবহার করতে প্রথমে আমাদের চ্যানেল {name} জয়েন করুন।\n\n"
        f"জয়েন করে নিচের \"আমি জয়েন করেছি\" বোতামে চাপ দিন।"
    )


async def force_join_ok(context, user_id):
    """ফোর্স-জয়েন চ্যানেল সেট না থাকলে True; থাকলে মেম্বারশিপ চেক করে।"""
    channel = get_force_channel()
    if not channel:
        return True
    chat_id = int(channel) if channel.isdigit() else f"@{channel}"
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Force-join check failed (%s): %s", channel, e)
        return True  # চেক করা না গেলে বট আটকে দেবে না


async def banned_reply(update):
    """ব্যান হওয়া ইউজার হলে True — সাথে ব্যান মেসেজ পাঠিয়ে দেওয়া হয়।"""
    user = update.effective_user
    if user is None or not is_banned(user.id):
        return False
    text = "🚫 আপনি এই বট থেকে ব্যান হয়েছেন। বটটি আর ব্যবহার করতে পারবেন না।"
    if update.callback_query:
        await update.callback_query.edit_message_text(text)
    elif update.message:
        await update.message.reply_text(text)
    return True


async def ensure_joined_message(update, context):
    """ফোর্স-জয়েন চ্যানেল জয়েন না করলে প্রম্পট পাঠিয়ে False দেয়।"""
    if await force_join_ok(context, update.effective_user.id):
        return True
    if update.message:
        await update.message.reply_text(
            force_join_text(), reply_markup=force_join_keyboard()
        )
    return False


def build_welcome(user, bot_name):
    return (
        f"👋 স্বাগতম {user.first_name}!\n\n"
        f"আমি {bot_name}। আপনার রেফারেল লিংক শেয়ার করুন, "
        f"কেউ আপনার লিংক থেকে জয়েন করলেই আপনি টাকা পাবেন।\n\n"
        f"🎁 প্রতি রেফারে পাবেন: {get_setting('reward', DEFAULT_REWARD)} টাকা\n"
        f"🏦 সর্বনিম্ন উইথড্র: {get_setting('min_withdraw', DEFAULT_MIN_WITHDRAW)} টাকা\n"
        f"💰 আপনার ব্যালান্স: {get_balance(user.id)} টাকা\n\n"
        f"নিচের বোতাম থেকে লিংক নিন, ব্যালান্স দেখুন বা উইথড্র করুন।"
    )


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    me = await get_bot_info(context)
    bot_name = me.first_name
    payload = context.args[0] if context.args else None

    is_new = add_user(user.id, user.username, user.first_name)
    if await banned_reply(update):
        return
    referred = False
    if is_new and payload and payload.isdigit():
        referred = record_referral(int(payload), user.id)
        if referred:
            reward = get_setting("reward", DEFAULT_REWARD)
            try:
                await context.bot.send_message(
                    chat_id=int(payload),
                    text=f"🎉 অভিনন্দন! আপনার লিংক থেকে একজন নতুন ইউজার জয়েন করেছে।\n"
                    f"আপনার ব্যালান্সে {reward} টাকা যোগ হয়েছে।",
                )
            except Exception as e:
                logger.warning("Referrer notify failed: %s", e)

    if not await force_join_ok(context, user.id):
        if update.message:
            await update.message.reply_text(
                force_join_text(), reply_markup=force_join_keyboard()
            )
        return

    if update.message:
        await update.message.reply_text(
            build_welcome(user, bot_name), reply_markup=main_menu_keyboard()
        )


async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    me = await get_bot_info(context)
    add_user(user.id, user.username, user.first_name)
    if await banned_reply(update):
        return
    if not await ensure_joined_message(update, context):
        return
    link = f"https://t.me/{me.username}?start={user.id}"
    count = get_referral_count(user.id)
    balance = get_balance(user.id)
    text = (
        f"🔗 আপনার রেফারেল লিংক:\n{link}\n\n"
        f"👥 আপনার রেফারেল: {count} জন\n"
        f"💰 আপনার ব্যালান্স: {balance} টাকা\n"
        f"🎁 প্রতি রেফারে পাবেন: {get_setting('reward', DEFAULT_REWARD)} টাকা\n\n"
        f"লিংকটি বন্ধুদের সাথে শেয়ার করুন। নতুন কেউ লিংক থেকে জয়েন করলেই "
        f"আপনার ব্যালান্সে টাকা যোগ হবে।"
    )
    if update.message:
        await update.message.reply_text(text)


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)
    if await banned_reply(update):
        return
    if not await ensure_joined_message(update, context):
        return
    balance = get_balance(user.id)
    count = get_referral_count(user.id)
    text = (
        f"💰 আপনার ব্যালান্স: {balance} টাকা\n"
        f"👥 মোট রেফারেল: {count} জন\n"
        f"🏦 সর্বনিম্ন উইথড্র: {get_setting('min_withdraw', DEFAULT_MIN_WITHDRAW)} টাকা\n\n"
        f"উইথড্র করতে /withdraw লিখুন।"
    )
    if update.message:
        await update.message.reply_text(text)


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)
    if await banned_reply(update):
        return
    if not await ensure_joined_message(update, context):
        return
    balance = get_balance(user.id)
    min_wd = get_setting("min_withdraw", DEFAULT_MIN_WITHDRAW)
    if balance < min_wd:
        await update.message.reply_text(
            f"❌ আপনার ব্যালান্স {balance} টাকা। উইথড্র করতে কমপক্ষে {min_wd} টাকা লাগবে।\n"
            f"আরও রেফার করুন — /refer থেকে লিংক শেয়ার করুন।"
        )
        return
    context.user_data["state"] = "wd_amount"
    await update.message.reply_text(
        f"🏦 উইথড্র\n\nআপনার ব্যালান্স: {balance} টাকা\n"
        f"সর্বনিম্ন উইথড্র: {min_wd} টাকা\n\n"
        f"কত টাকা উইথড্র করতে চান? শুধু সংখ্যা লিখুন (যেমন: {min_wd})।\n"
        f"বাতিল করতে /cancel লিখুন।"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await banned_reply(update):
        return
    context.user_data.pop("state", None)
    context.user_data.pop("wd_amount", None)
    context.user_data.pop("wd_method", None)
    if update.message:
        await update.message.reply_text("✅ বাতিল করা হয়েছে।", reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

ADMIN_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🎁 রিওয়ার্ড সেট করুন", callback_data="admin_reward", style="primary")],
        [InlineKeyboardButton("🏦 সর্বনিম্ন উইথড্র সেট করুন", callback_data="admin_minwd", style="primary")],
        [InlineKeyboardButton("📢 ফোর্স-জয়েন চ্যানেল", callback_data="admin_force", style="primary")],
        [InlineKeyboardButton("💬 সাপোর্ট আইডি সেট করুন", callback_data="admin_support", style="primary")],
        [InlineKeyboardButton("🚫 ইউজার ব্যান / আনব্যান", callback_data="admin_ban", style="primary")],
        [InlineKeyboardButton("📊 স্ট্যাটস দেখুন", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton("⏳ পেন্ডিং উইথড্র", callback_data="admin_pending", style="primary")],
        [InlineKeyboardButton("📣 ব্রডকাস্ট পাঠান", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton("👥 সাম্প্রতিক ইউজার", callback_data="admin_users", style="primary")],
    ]
)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনের জন্য।")
        return
    await update.message.reply_text(
        "🛠 অ্যাডমিন প্যানেল\n\nনিচ থেকে যেটা দরকার সেটা বেছে নিন।",
        reply_markup=ADMIN_MENU,
    )


async def admin_stats_text():
    total_users, total_refs, paid, pending = get_stats()
    return (
        f"📊 স্ট্যাটস\n\n"
        f"👥 মোট ইউজার: {total_users} জন\n"
        f"🔗 মোট রেফারেল: {total_refs} টি\n"
        f"💸 মোট পেইড: {paid} টাকা\n"
        f"⏳ পেন্ডিং উইথড্র: {pending} টি\n"
        f"🎁 প্রতি রেফারে রিওয়ার্ড: {get_setting('reward', DEFAULT_REWARD)} টাকা\n"
        f"🏦 সর্বনিম্ন উইথড্র: {get_setting('min_withdraw', DEFAULT_MIN_WITHDRAW)} টাকা\n"
        f"📢 ফোর্স-জয়েন চ্যানেল: {('@' + get_force_channel()) if get_force_channel() else 'সেট করা নেই'}\n"
        f"💬 সাপোর্ট আইডি: {get_support_id() or 'সেট করা নেই'}"
    )


async def send_pending_list(query, context):
    pending = get_pending_withdrawals()
    if not pending:
        await query.edit_message_text(
            "⏳ কোনো পেন্ডিং উইথড্র নেই।", reply_markup=ADMIN_MENU
        )
        return
    for wd in pending:
        u = get_user(wd["user_id"])
        name = (u["first_name"] if u else "?") + (
            f" (@{u['username']})" if u and u["username"] else ""
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"wd_ok_{wd['id']}", style="success"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"wd_no_{wd['id']}", style="danger"),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"⏳ উইথড্র #{wd['id']}\n"
                f"👤 {name} (ID: {wd['user_id']})\n"
                f"💰 পরিমাণ: {wd['amount']} টাকা\n"
                f"🏦 মাধ্যম: {wd['method']}\n"
                f"📱 নম্বর: {wd['account']}\n"
                f"🕐 {wd['created_at']}"
            ),
            reply_markup=kb,
        )
    await query.edit_message_text(
        f"⏳ মোট {len(pending)}টি পেন্ডিং উইথড্র উপরে পাঠানো হয়েছে।",
        reply_markup=ADMIN_MENU,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    data = query.data
    await query.answer()
    if await banned_reply(update):
        return

    # --- ইউজার মেনু বোতাম ---
    if data == "menu_refer":
        me = await get_bot_info(context)
        add_user(user.id, user.username, user.first_name)
        if not await force_join_ok(context, user.id):
            await query.edit_message_text(
                force_join_text(), reply_markup=force_join_keyboard()
            )
            return
        link = f"https://t.me/{me.username}?start={user.id}"
        await query.edit_message_text(
            f"🔗 আপনার রেফারেল লিংক:\n{link}\n\n"
            f"👥 আপনার রেফারেল: {get_referral_count(user.id)} জন\n"
            f"💰 আপনার ব্যালান্স: {get_balance(user.id)} টাকা",
            reply_markup=main_menu_keyboard(),
        )
        return
    if data == "menu_balance":
        add_user(user.id, user.username, user.first_name)
        if not await force_join_ok(context, user.id):
            await query.edit_message_text(
                force_join_text(), reply_markup=force_join_keyboard()
            )
            return
        await query.edit_message_text(
            f"💰 আপনার ব্যালান্স: {get_balance(user.id)} টাকা\n"
            f"👥 মোট রেফারেল: {get_referral_count(user.id)} জন\n"
            f"🏦 সর্বনিম্ন উইথড্র: {get_setting('min_withdraw', DEFAULT_MIN_WITHDRAW)} টাকা",
            reply_markup=main_menu_keyboard(),
        )
        return
    if data == "menu_withdraw":
        if not await force_join_ok(context, user.id):
            await query.edit_message_text(
                force_join_text(), reply_markup=force_join_keyboard()
            )
            return
        await query.edit_message_text(
            "🏦 উইথড্র করতে /withdraw লিখুন।", reply_markup=main_menu_keyboard()
        )
        return

    # --- ফোর্স-জয়েন চেক বোতাম ---
    if data == "check_join":
        if await force_join_ok(context, user.id):
            me = await get_bot_info(context)
            add_user(user.id, user.username, user.first_name)
            await query.edit_message_text(
                build_welcome(user, me.first_name), reply_markup=main_menu_keyboard()
            )
        else:
            await query.answer(
                "❌ এখনো চ্যানেল জয়েন করেননি — আগে জয়েন করুন।", show_alert=True
            )
        return

    # --- উইথড্র মাধ্যম বোতাম ---
    if data in ("wd_method_bkash", "wd_method_nagad"):
        method = "bKash" if data == "wd_method_bkash" else "Nagad"
        context.user_data["wd_method"] = method
        context.user_data["state"] = "wd_account"
        await query.edit_message_text(
            f"🏦 মাধ্যম: {method}\n\nআপনার {method} নম্বরটি লিখুন (যেমন: 01XXXXXXXXX)।\n"
            f"বাতিল করতে /cancel লিখুন।"
        )
        return

    # --- অ্যাডমিন বোতাম ---
    if not is_admin(user.id):
        await query.edit_message_text("❌ এই বোতাম শুধু অ্যাডমিনের জন্য।")
        return

    if data == "admin_menu":
        await query.edit_message_text("🛠 অ্যাডমিন প্যানেল", reply_markup=ADMIN_MENU)
        return
    if data == "admin_reward":
        context.user_data["state"] = "admin_reward"
        await query.edit_message_text(
            f"🎁 বর্তমান রিওয়ার্ড: {get_setting('reward', DEFAULT_REWARD)} টাকা / রেফার\n\n"
            f"নতুন রিওয়ার্ড কত টাকা হবে? শুধু সংখ্যা লিখুন।\nবাতিল করতে /cancel লিখুন।"
        )
        return
    if data == "admin_minwd":
        context.user_data["state"] = "admin_minwd"
        await query.edit_message_text(
            f"🏦 বর্তমান সর্বনিম্ন উইথড্র: {get_setting('min_withdraw', DEFAULT_MIN_WITHDRAW)} টাকা\n\n"
            f"নতুন সর্বনিম্ন উইথড্র কত টাকা হবে? শুধু সংখ্যা লিখুন।\nবাতিল করতে /cancel লিখুন।"
        )
        return
    if data == "admin_force":
        current = get_force_channel()
        context.user_data["state"] = "admin_force"
        await query.edit_message_text(
            f"📢 বর্তমান ফোর্স-জয়েন চ্যানেল: {('@' + current) if current else 'সেট করা নেই'}\n\n"
            f"চ্যানেলের ইউজারনেম লিখুন (যেমন: @mychannel) — অথবা চ্যানেলের সংখ্যার ID।\n"
            f"বন্ধ করতে লিখুন: বন্ধ\nবাতিল করতে /cancel লিখুন।\n\n"
            f"⚠️ বটকে ওই চ্যানেলের অ্যাডমিন বানাতে হবে, নাহলে জয়েন চেক করা যাবে না।"
        )
        return
    if data == "admin_support":
        current = get_support_id()
        context.user_data["state"] = "admin_support"
        await query.edit_message_text(
            f"💬 বর্তমান সাপোর্ট আইডি: {current if current else 'সেট করা নেই'}\n\n"
            f"সাপোর্টের ইউজারনেম লিখুন (যেমন: @supportuser) অথবা Telegram সংখ্যার ID।\n"
            f"বন্ধ করতে (বোতাম লুকাতে) লিখুন: বন্ধ\nবাতিল করতে /cancel লিখুন।"
        )
        return
    if data == "admin_ban":
        context.user_data["state"] = "admin_ban"
        await query.edit_message_text(
            "🚫 ইউজার ব্যান / আনব্যান\n\n"
            "যে ইউজারকে ব্যান বা আনব্যান করতে চান তার Telegram সংখ্যার ID লিখুন।\n"
            "বাতিল করতে /cancel লিখুন।"
        )
        return
    if data == "admin_stats":
        await query.edit_message_text(
            await admin_stats_text(), reply_markup=ADMIN_MENU
        )
        return
    if data == "admin_pending":
        await send_pending_list(query, context)
        return
    if data == "admin_broadcast":
        context.user_data["state"] = "admin_broadcast"
        await query.edit_message_text(
            "📣 সব ইউজারকে যে মেসেজটি পাঠাতে চান সেটি লিখুন।\nবাতিল করতে /cancel লিখুন।"
        )
        return
    if data == "admin_users":
        users = get_recent_users(10)
        if not users:
            await query.edit_message_text(
                "👥 এখনো কোনো ইউজার নেই।", reply_markup=ADMIN_MENU
            )
            return
        lines = ["👥 সাম্প্রতিক ১০ জন ইউজার:\n"]
        rows = []
        for u in users:
            banned = bool(u["banned"])
            lines.append(
                f"• {u['first_name'] or '?'}"
                + (f" (@{u['username']})" if u["username"] else "")
                + f" — ID: {u['user_id']}, ব্যালান্স: {u['balance']} টাকা"
                + (" 🚫 ব্যান করা" if banned else "")
                + f", {u['created_at']}"
            )
            label = f"✅ আনব্যান {u['user_id']}" if banned else f"🚫 ব্যান {u['user_id']}"
            cb = f"unban_{u['user_id']}" if banned else f"ban_{u['user_id']}"
            rows.append(
                [InlineKeyboardButton(label, callback_data=cb, style="success" if banned else "danger")]
            )
        rows.append(
            [InlineKeyboardButton("⬅️ অ্যাডমিন প্যানেল", callback_data="admin_menu", style="primary")]
        )
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        return

    # --- ইউজার ব্যান / আনব্যান ---
    if data.startswith("ban_") or data.startswith("unban_"):
        ban = data.startswith("ban_")
        target_id = int(data.split("_", 1)[1])
        if set_banned(target_id, ban):
            await query.edit_message_text(
                f"{'🚫' if ban else '✅'} ইউজার {target_id} কে "
                f"{'ব্যান করা হয়েছে' if ban else 'আনব্যান করা হয়েছে'}।",
                reply_markup=ADMIN_MENU,
            )
        else:
            await query.edit_message_text(
                f"❌ ID {target_id} এর কোনো ইউজার বটে নেই।", reply_markup=ADMIN_MENU
            )
        return

    # --- উইথড্র Approve / Reject ---
    if data.startswith("wd_ok_") or data.startswith("wd_no_"):
        approve = data.startswith("wd_ok_")
        wd_id = int(data.split("_")[-1])
        wd = get_withdrawal(wd_id)
        if wd is None or wd["status"] != "pending":
            await query.edit_message_text("ℹ️ এই উইথড্রটি আগেই সিদ্ধান্ত নেওয়া হয়েছে।")
            return
        if approve:
            set_withdrawal_status(wd_id, "paid")
            await query.edit_message_text(
                f"✅ উইথড্র #{wd_id} Approve করা হয়েছে — {wd['amount']} টাকা পেইড হিসেবে চিহ্নিত।\n"
                f"মনে রাখবেন: টাকাটা আপনাকে নিজে {wd['method']} ({wd['account']}) নম্বরে পাঠাতে হবে।"
            )
            try:
                await context.bot.send_message(
                    chat_id=wd["user_id"],
                    text=f"✅ আপনার {wd['amount']} টাকার উইথড্র Approve হয়েছে।\n"
                    f"টাকাটা আপনার {wd['method']} ({wd['account']}) নম্বরে পাঠানো হয়েছে।",
                )
            except Exception as e:
                logger.warning("User notify failed: %s", e)
        else:
            set_withdrawal_status(wd_id, "rejected")
            add_balance(wd["user_id"], wd["amount"])  # টাকা ফেরত
            await query.edit_message_text(
                f"❌ উইথড্র #{wd_id} Reject করা হয়েছে — {wd['amount']} টাকা ইউজারের ব্যালান্সে ফেরত দেওয়া হয়েছে।"
            )
            try:
                await context.bot.send_message(
                    chat_id=wd["user_id"],
                    text=f"❌ আপনার {wd['amount']} টাকার উইথড্র Reject হয়েছে।\n"
                    f"টাকাটা আপনার ব্যালান্সে ফেরত দেওয়া হয়েছে।",
                )
            except Exception as e:
                logger.warning("User notify failed: %s", e)
        return


# ---------------------------------------------------------------------------
# Text input — উইথড্র ফ্লো + অ্যাডমিন সেটিংস + ব্রডকাস্ট
# ---------------------------------------------------------------------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = context.user_data.get("state")
    text = (update.message.text or "").strip()
    if await banned_reply(update):
        return
    if not state:
        return

    # --- উইথড্র: পরিমাণ ---
    if state == "wd_amount":
        if not text.isdigit():
            await update.message.reply_text("❌ শুধু সংখ্যা লিখুন (যেমন: 50)।")
            return
        amount = int(text)
        balance = get_balance(user.id)
        min_wd = get_setting("min_withdraw", DEFAULT_MIN_WITHDRAW)
        if amount < min_wd:
            await update.message.reply_text(
                f"❌ সর্বনিম্ন উইথড্র {min_wd} টাকা। আবার লিখুন।"
            )
            return
        if amount > balance:
            await update.message.reply_text(
                f"❌ আপনার ব্যালান্স {balance} টাকা — এর বেশি উইথড্র করা যাবে না। আবার লিখুন।"
            )
            return
        context.user_data["wd_amount"] = amount
        context.user_data["state"] = "wd_method"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("bKash", callback_data="wd_method_bkash", style="primary"),
                    InlineKeyboardButton("Nagad", callback_data="wd_method_nagad", style="primary"),
                ]
            ]
        )
        await update.message.reply_text(
            f"💰 পরিমাণ: {amount} টাকা\n\nকোন মাধ্যমে টাকা নিতে চান?", reply_markup=kb
        )
        return

    # --- উইথড্র: নম্বর ---
    if state == "wd_account":
        account = text.replace(" ", "").replace("-", "")
        if not account.isdigit() or len(account) < 11:
            await update.message.reply_text(
                "❌ সঠিক নম্বর লিখুন (যেমন: 01XXXXXXXXX)।"
            )
            return
        amount = context.user_data.get("wd_amount")
        method = context.user_data.get("wd_method")
        if amount is None or method is None:
            context.user_data.pop("state", None)
            await update.message.reply_text(
                "❌ কিছু একটা ভুল হয়েছে। আবার /withdraw লিখুন।"
            )
            return
        wd_id = create_withdrawal(user.id, amount, method, account)
        context.user_data.pop("state", None)
        context.user_data.pop("wd_amount", None)
        context.user_data.pop("wd_method", None)
        await update.message.reply_text(
            f"✅ উইথড্র রিকোয়েস্ট পাঠানো হয়েছে!\n\n"
            f"💰 পরিমাণ: {amount} টাকা\n"
            f"🏦 মাধ্যম: {method}\n"
            f"📱 নম্বর: {account}\n\n"
            f"অ্যাডমিন Approve করলেই টাকা পাঠানো হবে।"
        )
        # অ্যাডমিনকে জানানো
        if ADMIN_ID:
            u = get_user(user.id)
            name = (u["first_name"] if u else "?") + (
                f" (@{u['username']})" if u and u["username"] else ""
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"wd_ok_{wd_id}", style="success"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"wd_no_{wd_id}", style="danger"),
                    ]
                ]
            )
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🔔 নতুন উইথড্র রিকোয়েস্ট #{wd_id}\n"
                        f"👤 {name} (ID: {user.id})\n"
                        f"💰 পরিমাণ: {amount} টাকা\n"
                        f"🏦 মাধ্যম: {method}\n"
                        f"📱 নম্বর: {account}"
                    ),
                    reply_markup=kb,
                )
            except Exception as e:
                logger.warning("Admin notify failed: %s", e)
        return

    # --- অ্যাডমিন: রিওয়ার্ড সেট ---
    if state == "admin_reward":
        if not is_admin(user.id):
            context.user_data.pop("state", None)
            return
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("❌ ১ বা তার বেশি সংখ্যা লিখুন।")
            return
        set_setting("reward", int(text))
        context.user_data.pop("state", None)
        await update.message.reply_text(
            f"✅ প্রতি রেফারে রিওয়ার্ড এখন {text} টাকা।", reply_markup=ADMIN_MENU
        )
        return

    # --- অ্যাডমিন: সর্বনিম্ন উইথড্র সেট ---
    if state == "admin_minwd":
        if not is_admin(user.id):
            context.user_data.pop("state", None)
            return
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("❌ ১ বা তার বেশি সংখ্যা লিখুন।")
            return
        set_setting("min_withdraw", int(text))
        context.user_data.pop("state", None)
        await update.message.reply_text(
            f"✅ সর্বনিম্ন উইথড্র এখন {text} টাকা।", reply_markup=ADMIN_MENU
        )
        return

    # --- অ্যাডমিন: ফোর্স-জয়েন চ্যানেল সেট ---
    if state == "admin_force":
        if not is_admin(user.id):
            context.user_data.pop("state", None)
            return
        if text.lower() in ("বন্ধ", "off", "0", "বন্ধ করুন"):
            set_str_setting("force_channel", "")
            context.user_data.pop("state", None)
            await update.message.reply_text(
                "✅ ফোর্স-জয়েন চ্যানেল বন্ধ করা হয়েছে।", reply_markup=ADMIN_MENU
            )
            return
        clean = text.lstrip("@").strip()
        if not clean:
            await update.message.reply_text("❌ চ্যানেলের ইউজারনেম বা ID লিখুন।")
            return
        set_str_setting("force_channel", clean)
        context.user_data.pop("state", None)
        display = f"@{clean}" if not clean.isdigit() else clean
        await update.message.reply_text(
            f"✅ ফোর্স-জয়েন চ্যানেল সেট হয়েছে: {display}\n\n"
            f"⚠️ মনে রাখবেন: বটকে ওই চ্যানেলের অ্যাডমিন বানাতে হবে, নাহলে জয়েন চেক করা যাবে না।",
            reply_markup=ADMIN_MENU,
        )
        return

    # --- অ্যাডমিন: সাপোর্ট আইডি সেট ---
    if state == "admin_support":
        if not is_admin(user.id):
            context.user_data.pop("state", None)
            return
        if text.lower() in ("বন্ধ", "off", "0", "বন্ধ করুন"):
            set_str_setting("support_id", "")
            context.user_data.pop("state", None)
            await update.message.reply_text(
                "✅ সাপোর্ট বোতাম বন্ধ করা হয়েছে (এখন আর দেখাবে না)।",
                reply_markup=ADMIN_MENU,
            )
            return
        clean = text.lstrip("@").strip()
        if not clean:
            await update.message.reply_text("❌ সাপোর্টের ইউজারনেম বা সংখ্যার ID লিখুন।")
            return
        set_str_setting("support_id", clean)
        context.user_data.pop("state", None)
        await update.message.reply_text(
            f"✅ সাপোর্ট আইডি সেট হয়েছে: {clean}\nহোম মেনুতে এখন 💬 সাপোর্ট বোতাম দেখাবে।",
            reply_markup=ADMIN_MENU,
        )
        return

    # --- অ্যাডমিন: ইউজার ব্যান / আনব্যান ---
    if state == "admin_ban":
        if not is_admin(user.id):
            context.user_data.pop("state", None)
            return
        if not text.isdigit():
            await update.message.reply_text("❌ শুধু সংখ্যার Telegram ID লিখুন।")
            return
        target_id = int(text)
        target = get_user(target_id)
        context.user_data.pop("state", None)
        if target is None:
            await update.message.reply_text(
                f"❌ ID {target_id} এর কোনো ইউজার বটে নেই।", reply_markup=ADMIN_MENU
            )
            return
        name = (target["first_name"] or "?") + (
            f" (@{target['username']})" if target["username"] else ""
        )
        banned = is_banned(target_id)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ আনব্যান করুন" if banned else "🚫 ব্যান করুন",
                        callback_data=f"{'unban' if banned else 'ban'}_{target_id}",
                        style="success" if banned else "danger",
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"👤 {name}\n🆔 ID: {target_id}\n💰 ব্যালান্স: {target['balance']} টাকা\n"
            f"অবস্থা: {'🚫 ব্যান করা' if banned else '✅ স্বাভাবিক'}",
            reply_markup=kb,
        )
        return

    # --- অ্যাডমিন: ব্রডকাস্ট ---
    if state == "admin_broadcast":
        if not is_admin(user.id):
            context.user_data.pop("state", None)
            return
        context.user_data.pop("state", None)
        user_ids = get_all_user_ids()
        ok = 0
        fail = 0
        for uid in user_ids:
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                ok += 1
            except Exception:
                fail += 1
        await update.message.reply_text(
            f"📣 ব্রডকাস্ট শেষ।\n\n✅ পৌঁছেছে: {ok} জন\n❌ পৌঁছায়নি: {fail} জন",
            reply_markup=ADMIN_MENU,
        )
        return


# ---------------------------------------------------------------------------
# Health server (শুধু PORT সেট থাকলে) + main
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET", "HEAD"])
def health():
    return "OK", 200


def run_health_server():
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        flask_app.run(host="0.0.0.0", port=int(port))
    except Exception as e:
        logger.warning("Health server failed: %s", e)


async def post_init(application: Application):
    me = await application.bot.get_me()
    application.bot_data["me"] = me
    logger.info("Bot started as @%s (%s)", me.username, me.first_name)
    try:
        await application.bot.set_my_commands(
            [
                ("start", "বট শুরু করুন / রেফারেল লিংক পান"),
                ("refer", "আমার রেফারেল লিংক ও ব্যালান্স"),
                ("balance", "আমার ব্যালান্স দেখুন"),
                ("withdraw", "টাকা উইথড্র করুন"),
                ("admin", "অ্যাডমিন প্যানেল (শুধু অ্যাডমিন)"),
                ("cancel", "চলমান কাজ বাতিল করুন"),
            ]
        )
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN env var সেট করা নেই।")
    if ADMIN_ID == 0:
        logger.warning("ADMIN_ID env var সেট করা নেই — /admin কাজ করবে না।")
    init_db()
    if os.environ.get("PORT"):
        threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refer", refer_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logger.info("Polling শুরু হচ্ছে...")
    app.run_polling()


if __name__ == "__main__":
    main()
