import os
import json
import asyncio
import logging
from threading import Thread
from flask import Flask
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ================= CONFIGURATION =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
DB_FILE = "bot_data.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= KEEP ALIVE SERVER =================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Referral Bot is Running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# ================= DATABASE HELPER =================
DEFAULT_DATA = {
    "users": {},
    "settings": {
        "refer_bonus": 2.0,
        # ---- উইথড্র সিস্টেম (bKash / Nagad শুধু) ----
        "withdraw_enabled": "ON",  # "ON" অথবা "OFF" — OFF থাকলে নতুন উইথড্র বন্ধ
        "wd_bkash_active": "ON",
        "wd_nagad_active": "ON",
        "min_withdraw_bkash": 50.0,
        "max_withdraw_bkash": 5000.0,
        "min_withdraw_nagad": 50.0,
        "max_withdraw_nagad": 5000.0,
        "wd_charge": 5.0,  # প্রতি উইথড্রে ফিক্সড চার্জ (টাকা) — অ্যাডমিন প্যানেল থেকে বদলানো যায়
        # ---- পেআউট লগ (OFF থাকলে শুধু অ্যাডমিনকে নোটিফিকেশন যায়) ----
        "payout_log_active": "OFF",
        "payout_log_chat": "",
        "force_channels": [],  # ফোর্স জয়েন চ্যানেলের লিস্ট (খালি = OFF, কোনো সর্বোচ্চ সীমা নেই)
        "support_id": "@AdminUsername",
    },
    "withdraws": {},
    "withdraw_counter": 1000,
}

def load_data():
    if not os.path.exists(DB_FILE):
        save_data(DEFAULT_DATA)
        return json.loads(json.dumps(DEFAULT_DATA))
    with open(DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # পুরনো DB-তে নতুন উইথড্র সেটিংস না থাকলে ডিফল্ট বসিয়ে নাও
    defaults = DEFAULT_DATA["settings"]
    for key, val in defaults.items():
        data.setdefault("settings", {}).setdefault(key, val)
    data.setdefault("withdraws", {})
    data.setdefault("withdraw_counter", 1000)
    # force_channels সবসময় আলাদা লিস্ট হিসেবে রাখো (ডিফল্ট লিস্ট শেয়ার করলে সেটা নষ্ট হবে)
    fc = data["settings"].get("force_channels")
    if not isinstance(fc, list):
        fc = [fc] if isinstance(fc, str) and fc.strip() else []
    data["settings"]["force_channels"] = list(fc)
    # পুরনো এক-চ্যানেল সেটিং (force_channel) থাকলে লিস্টে নিয়ে নাও (একবারই)
    legacy = str(data["settings"].get("force_channel", "") or "").strip()
    if legacy:
        channels = data["settings"]["force_channels"]
        if legacy not in channels:
            channels.append(legacy)
        data["settings"]["force_channel"] = ""
        save_data(data)
    return data

def save_data(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_user(data, user_id, name="User"):
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "name": name,
            "balance": 0.0,
            "earned": 0.0,  # আজীবন রেফারেল আয় (ওয়ালেটে দেখানোর জন্য)
            "refers": 0,
            "referred_by": None,
            "refer_paid": False,
            "banned": False,
        }
    else:
        data["users"][uid].setdefault("earned", 0.0)
    return data["users"][uid]

# ================= WITHDRAW HELPERS (bKash / Nagad শুধু) =================
WD_METHODS = ["bKash", "Nagad"]

def wd_method_limits(data: dict, method: str):
    """প্রতি মেথডের (সর্বনিম্ন, সর্বোচ্চ) লিমিট দেয়।"""
    key = method.lower()
    s = data["settings"]
    mn = float(s.get(f"min_withdraw_{key}", 50.0))
    mx = float(s.get(f"max_withdraw_{key}", 5000.0))
    return mn, mx

def wd_active_methods(data: dict) -> list:
    """চালু থাকা উইথড্র মেথডগুলো দেয়। গ্লোবাল OFF থাকলে খালি লিস্ট।"""
    s = data["settings"]
    if str(s.get("withdraw_enabled", "ON")).upper() != "ON":
        return []
    active = []
    if str(s.get("wd_bkash_active", "ON")).upper() == "ON":
        active.append("bKash")
    if str(s.get("wd_nagad_active", "ON")).upper() == "ON":
        active.append("Nagad")
    return active

def withdraw_allowed(data: dict) -> bool:
    return len(wd_active_methods(data)) > 0

def wd_charge_amount(data: dict) -> float:
    """অ্যাডমিনের সেট করা উইথড্র চার্জ (ফিক্সড টাকা, ডিফল্ট ০)।"""
    try:
        return max(0.0, float(data["settings"].get("wd_charge", 0) or 0))
    except (TypeError, ValueError):
        return 0.0

def wd_net_amount(data: dict, amount: float) -> float:
    """চার্জ কেটে নিট পেমেন্টের পরিমাণ (০-এর নিচে যাবে না)।"""
    return max(0.0, float(amount) - wd_charge_amount(data))

def wd_amount_lines(data: dict, amount: float) -> str:
    """চার্জ > ০ হলে 'পরিমাণ | চার্জ | পাবেন' লাইন, না হলে শুধু পরিমাণের লাইন।"""
    charge = wd_charge_amount(data)
    if charge > 0:
        net = wd_net_amount(data, amount)
        return f"💰 পরিমাণ: <b>{amount:g} ৳</b> | চার্জ: <b>{charge:g} ৳</b> | পাবেন: <b>{net:g} ৳</b>"
    return f"💰 পরিমাণ: <b>{amount:g} ৳</b>"

def wd_validate_amount(data: dict, method: str, amount: float, balance: float):
    """পরিমাণ যাচাই করে। (ঠিক আছে?, ভুল বার্তা) দেয়।"""
    mn, mx = wd_method_limits(data, method)
    if amount < mn:
        return False, f"❌ সর্বনিম্ন উইথড্র <b>{mn:g} ৳</b>! আবার পরিমাণ লিখুন:"
    if amount > mx:
        return False, f"❌ সর্বোচ্চ উইথড্র <b>{mx:g} ৳</b>! আবার পরিমাণ লিখুন:"
    if amount > balance:
        return False, "❌ আপনার অ্যাকাউন্টে পর্যাপ্ত ব্যালেন্স নেই! /cancel দিয়ে বাতিল করুন।"
    return True, ""

def user_wallet_stats(data: dict, uid: str) -> dict:
    """ওয়ালেটের হিসাব: ব্যালেন্স, পেন্ডিং উইথড্র, আজীবন আয়, উইথড্র গণনা।"""
    user = data["users"].get(uid, {})
    pending = sum(
        float(w["amount"])
        for w in data.get("withdraws", {}).values()
        if w.get("user_id") == uid and w.get("status") == "pending"
    )
    counts = {"approved": 0, "pending": 0, "rejected": 0}
    for w in data.get("withdraws", {}).values():
        if w.get("user_id") == uid and w.get("status") in counts:
            counts[w["status"]] += 1
    return {
        "balance": float(user.get("balance", 0.0)),
        "earned": float(user.get("earned", 0.0)),
        "pending_withdraw": pending,
        "approved": counts["approved"],
        "pending_count": counts["pending"],
        "rejected": counts["rejected"],
    }

def create_withdraw(data: dict, uid: str, user_name: str, method: str, number: str, amount: float) -> str:
    """ব্যালেন্স কেটে পেন্ডিং উইথড্র রিকোয়েস্ট তৈরি করে, আইডি দেয়।"""
    data["users"][uid]["balance"] = float(data["users"][uid]["balance"]) - amount
    data["withdraw_counter"] += 1
    wd_id = str(data["withdraw_counter"])
    data["withdraws"][wd_id] = {
        "user_id": uid,
        "user_name": user_name,
        "method": method,
        "number": number,
        "amount": amount,
        "status": "pending",
    }
    save_data(data)
    return wd_id

def approve_withdraw(data: dict, wd_id: str):
    """পেন্ডিং রিকোয়েস্ট approved করে। (ঠিক আছে?, ভুল বার্তা) দেয়।"""
    wd = data.get("withdraws", {}).get(wd_id)
    if not wd or wd.get("status") != "pending":
        return False, "⚠️ এই রিকোয়েস্ট আগেই প্রসেস করা হয়েছে!"
    wd["status"] = "approved"
    save_data(data)
    return True, ""

def reject_withdraw(data: dict, wd_id: str):
    """পেন্ডিং রিকোয়েস্ট rejected করে, টাকা ব্যালেন্সে ফেরত দেয়।"""
    wd = data.get("withdraws", {}).get(wd_id)
    if not wd or wd.get("status") != "pending":
        return False, "⚠️ এই রিকোয়েস্ট আগেই প্রসেস করা হয়েছে!"
    wd["status"] = "rejected"
    target_uid = wd["user_id"]
    if target_uid in data["users"]:
        data["users"][target_uid]["balance"] = float(data["users"][target_uid]["balance"]) + float(wd["amount"])
    save_data(data)
    return True, ""

async def post_payout_log(context: ContextTypes.DEFAULT_TYPE, data: dict, event: str, wd: dict):
    """পেআউট লগ: লগ চ্যাট সেট ও ON থাকলে সেখানে পাঠায় (pending / paid)।"""
    s = data["settings"]
    if str(s.get("payout_log_active", "OFF")).upper() != "ON":
        return
    chat = str(s.get("payout_log_chat", "")).strip()
    if not chat:
        return
    title = "🆕 নতুন উইথড্র রিকোয়েস্ট" if event == "pending" else "✅ উইথড্র পেমেন্ট সম্পন্ন"
    text = (
        f"{title}\n\n"
        f"🆔 আইডি: <code>#{wd.get('wd_id', '')}</code>\n"
        f"👤 ইউজার: <b>{wd.get('user_name', '')}</b> (<code>{wd.get('user_id', '')}</code>)\n"
        f"💳 মেথড: <b>{wd.get('method', '')}</b>\n"
        f"📱 নম্বর: <code>{wd.get('number', '')}</code>\n"
        f"💰 পরিমাণ: <b>{wd.get('amount', 0)} ৳</b>"
    )
    try:
        target = int(chat) if chat.lstrip("-").isdigit() else chat
        await context.bot.send_message(chat_id=target, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Payout log failed: {e}")

# ================= COLORED BUTTON HELPERS =================
STYLE_MAP = {
    "green": "success",
    "blue": "primary",
    "red": "danger",
}

def btn(text: str, callback_data: str = None, url: str = None, color: str = None) -> dict:
    item = {"text": text}
    if callback_data:
        item["callback_data"] = callback_data
    if url:
        item["url"] = url
    if color and color in STYLE_MAP:
        item["style"] = STYLE_MAP[color]
    return item

def to_inline_markup(rows: list) -> InlineKeyboardMarkup:
    keyboard = []
    for row in rows:
        kb_row = []
        for item in row:
            kwargs = {}
            if "callback_data" in item:
                kwargs["callback_data"] = item["callback_data"]
            if "url" in item:
                kwargs["url"] = item["url"]
            kb_row.append(InlineKeyboardButton(item["text"], **kwargs))
        keyboard.append(kb_row)
    return InlineKeyboardMarkup(keyboard)

def to_reply_markup(rows: list) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(item["text"]) for item in row] for row in rows],
        resize_keyboard=True,
    )

async def send_reply_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    rows: list,
):
    chat_id = update.effective_chat.id
    reply_markup_payload = {
        "keyboard": rows,
        "resize_keyboard": True,
    }
    try:
        await context.bot._post(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup_payload),
            },
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=to_reply_markup(rows),
        )

async def send_styled_inline(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    rows: list,
):
    try:
        await context.bot._post(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": json.dumps({"inline_keyboard": rows}),
            },
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=to_inline_markup(rows),
        )

async def edit_styled_inline(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    text: str,
    rows: list = None,
):
    if rows is not None:
        try:
            await context.bot._post(
                "editMessageText",
                data={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps({"inline_keyboard": rows}),
                },
            )
            return
        except Exception:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=to_inline_markup(rows),
            )
            return

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=ParseMode.HTML,
    )

# ================= KEYBOARD ROW BUILDERS =================
def main_menu_reply_rows(user_id: int) -> list:
    rows = [
        [btn("🔗 Refer & Earn", color="green")],
        [
            btn("💰 Balance", color="blue"),
            btn("💸 Withdraw", color="red"),
        ],
        [btn("📞 Support", color="green")],
    ]
    if user_id == ADMIN_ID:
        rows.append([btn("⚙️ Admin Panel", color="blue")])
    return rows

def admin_panel_rows() -> list:
    return [
        [
            btn("📊 Bot Stats", callback_data="adm_stats", color="blue"),
            btn("📣 Broadcast", callback_data="adm_broadcast", color="green"),
        ],
        [
            btn("⏳ পেন্ডিং উইথড্র", callback_data="adm_pending_wd", color="blue"),
            btn("💸 উইথড্র সেটিংস", callback_data="adm_wd_settings", color="green"),
        ],
        [
            btn("📋 পেআউট লগ", callback_data="adm_payout_log", color="blue"),
            btn("🎁 Set Refer Bonus", callback_data="adm_set_bonus", color="green"),
        ],
        [
            btn("📢 Force Channels", callback_data="adm_set_channel", color="blue"),
            btn("🆘 Set Support ID", callback_data="adm_set_support", color="green"),
        ],
        [
            btn("➕ Add Balance", callback_data="adm_add_bal", color="green"),
            btn("➖ Cut Balance", callback_data="adm_cut_bal", color="red"),
        ],
        [
            btn("🚫 Ban User", callback_data="adm_ban", color="red"),
            btn("✅ Unban User", callback_data="adm_unban", color="green"),
        ],
    ]

def get_force_channels(data: dict) -> list:
    """ফোর্স চ্যানেলের লিস্ট দেয় (পুরনো এক-চ্যানেল সেটিং থেকেও পড়ে)।"""
    s = data["settings"]
    channels = s.get("force_channels", [])
    if isinstance(channels, str):
        channels = [channels] if channels.strip() else []
    channels = [str(c).strip() for c in channels if str(c).strip()]
    legacy = str(s.get("force_channel", "") or "").strip()
    if legacy and legacy not in channels:
        channels.append(legacy)
    return channels


# ================= FORCE JOIN CHECK =================
async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE, data: dict) -> bool:
    channels = get_force_channels(data)
    if not channels or user_id == ADMIN_ID:
        return True
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            logger.warning(f"Force join check error for {channel}: {e}")
            continue  # এই চ্যানেলের চেকে সমস্যা হলে ধরে নেওয়া হয় জয়েন করেছে (আগের মতোই fail-open)
    return True

async def send_force_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    channels = get_force_channels(data)
    rows = []
    for channel in channels:
        clean_username = channel.lstrip("@")
        rows.append([btn(f"📢 Join {channel}", url=f"https://t.me/{clean_username}", color="blue")])
    rows.append([btn("✅ Joined / Check", callback_data="check_join", color="green")])
    text = (
        "🚫 <b>Access Denied!</b>\n\n"
        "বটটি ব্যবহার করতে হলে অবশ্যই নিচের সব চ্যানেলে জয়েন করতে হবে।\n"
        "জয়েন করার পর <b>✅ Joined / Check</b> বাটনে ক্লিক করুন।"
    )
    await send_styled_inline(context, update.effective_chat.id, text, rows)

# ================= REWARD REFERRER HELPER =================
async def process_referral_reward(user_id: int, data: dict, context: ContextTypes.DEFAULT_TYPE):
    uid = str(user_id)
    user = data["users"].get(uid)
    if not user:
        return
    ref_by = user.get("referred_by")
    if ref_by and not user.get("refer_paid"):
        if ref_by in data["users"] and ref_by != uid:
            bonus = float(data["settings"]["refer_bonus"])
            data["users"][ref_by]["balance"] += bonus
            data["users"][ref_by]["earned"] = float(data["users"][ref_by].get("earned", 0.0)) + bonus
            data["users"][ref_by]["refers"] += 1
            user["refer_paid"] = True
            save_data(data)
            try:
                await context.bot.send_message(
                    chat_id=int(ref_by),
                    text=(
                        f"🎉 <b>নতুন রেফারেল সফল হয়েছে!</b>\n\n"
                        f"👤 ইউজার: <b>{user['name']}</b>\n"
                        f"💰 বোনাস যোগ হয়েছে: <b>+{bonus} ৳</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

# ================= HOME TEXT BUILDER =================
def build_home_text(first_name: str, data: dict) -> str:
    return (
        f"👋 <b>স্বাগতম, {first_name}!</b>\n\n"
        f"🔥 বন্ধুদের রেফার করুন এবং প্রতি রেফারে আয় করুন <b>{data['settings']['refer_bonus']} ৳</b>!\n"
        f"নিচের বাটনগুলো থেকে আপনার অপশন বেছে নিন 👇"
    )

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    uid = str(user_obj.id)
    data = load_data()

    is_new = uid not in data["users"]
    user = get_user(data, user_obj.id, user_obj.first_name)

    if user.get("banned") and user_obj.id != ADMIN_ID:
        await update.message.reply_text("🚫 <b>আপনাকে এই বট থেকে ব্যান করা হয়েছে।</b>", parse_mode=ParseMode.HTML)
        return

    if is_new and context.args:
        ref_id = context.args[0]
        if ref_id.isdigit() and ref_id != uid and ref_id in data["users"]:
            user["referred_by"] = ref_id

    save_data(data)

    joined = await check_force_join(user_obj.id, context, data)
    if not joined:
        await send_force_join_message(update, context, data)
        return

    await process_referral_reward(user_obj.id, data, context)

    await send_reply_menu(
        update,
        context,
        build_home_text(user_obj.first_name, data),
        main_menu_reply_rows(user_obj.id),
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await send_styled_inline(
        context,
        update.effective_chat.id,
        "⚙️ <b>ADMIN CONTROL PANEL</b>\n\nনিচের বাটনগুলো থেকে বটের সব সেটিংস কন্ট্রোল করুন:",
        admin_panel_rows(),
    )

# ================= CALLBACK QUERIES =================
async def show_wd_settings_page(context, chat_id, message_id, data):
    s = data["settings"]
    mn_b, mx_b = wd_method_limits(data, "bKash")
    mn_n, mx_n = wd_method_limits(data, "Nagad")
    charge = wd_charge_amount(data)
    text = (
        "💸 <b>উইথড্র সেটিংস</b>\n\n"
        f"🔘 উইথড্র: <b>{s.get('withdraw_enabled', 'ON')}</b>\n"
        f"🩷 bKash: <b>{s.get('wd_bkash_active', 'ON')}</b> | লিমিট: <b>{mn_b:g}–{mx_b:g} ৳</b>\n"
        f"🧡 Nagad: <b>{s.get('wd_nagad_active', 'ON')}</b> | লিমিট: <b>{mn_n:g}–{mx_n:g} ৳</b>\n"
        f"💸 চার্জ: <b>{charge:g} ৳</b> (প্রতি উইথড্রে)\n\n"
        "যেকোনো বাটনে চাপ দিয়ে ON/OFF করুন বা লিমিট বদলান:"
    )
    rows = [
        [btn(f"🔘 উইথড্র: {s.get('withdraw_enabled', 'ON')}", callback_data="adm_wd_toggle_all", color="blue")],
        [
            btn(f"🩷 bKash: {s.get('wd_bkash_active', 'ON')}", callback_data="adm_wd_toggle_bkash", color="green"),
            btn(f"🧡 Nagad: {s.get('wd_nagad_active', 'ON')}", callback_data="adm_wd_toggle_nagad", color="green"),
        ],
        [
            btn("✏️ bKash লিমিট", callback_data="adm_wd_edit_bkash", color="blue"),
            btn("✏️ Nagad লিমিট", callback_data="adm_wd_edit_nagad", color="blue"),
        ],
        [btn(f"✏️ চার্জ সেট করুন ({charge:g} ৳)", callback_data="adm_wd_edit_charge", color="blue")],
        [btn("⬅️ অ্যাডমিন প্যানেল", callback_data="adm_menu", color="blue")],
    ]
    await edit_styled_inline(context, chat_id, message_id, text, rows)

async def show_force_channels_page(context, chat_id, message_id, data):
    channels = get_force_channels(data)
    if channels:
        text = "📢 <b>ফোর্স চ্যানেল ম্যানেজমেন্ট</b>\n\n"
        rows = []
        for i, ch in enumerate(channels):
            text += f"{i+1}. <code>{ch}</code>\n"
            rows.append([btn(f"🗑️ মুছুন: {ch}", callback_data=f"adm_fc_del_{i}", color="red")])
    else:
        text = (
            "📢 <b>ফোর্স চ্যানেল ম্যানেজমেন্ট</b>\n\n"
            "❌ কোনো ফোর্স চ্যানেল সেট করা নেই — ফোর্স জয়েন এখন <b>Off</b>।"
        )
        rows = []
    rows.append([btn("➕ চ্যানেল যোগ করুন", callback_data="adm_fc_add", color="green")])
    if channels:
        rows.append([btn("🗑️ সব মুছে ফেলুন", callback_data="adm_fc_clear", color="red")])
    rows.append([btn("⬅️ অ্যাডমিন প্যানেল", callback_data="adm_menu", color="blue")])
    await edit_styled_inline(context, chat_id, message_id, text, rows)

async def show_payout_log_page(context, chat_id, message_id, data):
    s = data["settings"]
    text = (
        "📋 <b>পেআউট লগ</b>\n\n"
        f"🔘 স্ট্যাটাস: <b>{s.get('payout_log_active', 'OFF')}</b>\n"
        f"💬 লগ চ্যাট: <b>{s.get('payout_log_chat') or 'সেট করা নেই'}</b>\n\n"
        "ON থাকলে প্রতিটি নতুন উইথড্র ও পেমেন্টের লগ এই চ্যাটে যাবে।"
    )
    rows = [
        [btn(f"🔘 লগ: {s.get('payout_log_active', 'OFF')}", callback_data="adm_log_toggle", color="green")],
        [btn("💬 লগ চ্যাট সেট করুন", callback_data="adm_log_set_chat", color="blue")],
        [btn("⬅️ অ্যাডমিন প্যানেল", callback_data="adm_menu", color="blue")],
    ]
    await edit_styled_inline(context, chat_id, message_id, text, rows)
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_obj = query.from_user
    user_id = user_obj.id
    cb_data = query.data
    data = load_data()
    user = get_user(data, user_id, user_obj.first_name)

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    if user.get("banned") and user_id != ADMIN_ID:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🚫 <b>আপনাকে এই বট থেকে ব্যান করা হয়েছে।</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if cb_data == "check_join":
        joined = await check_force_join(user_id, context, data)
        if joined:
            await process_referral_reward(user_id, data, context)
            await query.message.delete()
            await send_reply_menu(
                update,
                context,
                build_home_text(user_obj.first_name, data),
                main_menu_reply_rows(user_id),
            )
        else:
            await query.answer("❌ আপনি এখনো চ্যানেলে জয়েন করেননি!", show_alert=True)
        return

    # Withdraw Method Selection (bKash & Nagad Only)
    if cb_data.startswith("wd_method_"):
        method = cb_data.replace("wd_method_", "")
        if method not in WD_METHODS:
            await query.answer("❌ শুধুমাত্র bKash এবং Nagad সাপোর্ট করে।", show_alert=True)
            return
        if method not in wd_active_methods(data):
            await query.answer("❌ এই মেথডের উইথড্র এখন বন্ধ আছে।", show_alert=True)
            return
        mn, mx = wd_method_limits(data, method)
        context.user_data["wd_method"] = method
        context.user_data["state"] = "WAIT_WD_NUMBER"
        await edit_styled_inline(
            context,
            chat_id,
            message_id,
            f"📲 <b>{method}</b> সিলেক্ট করা হয়েছে।\n\n"
            f"🔻 সর্বনিম্ন: <b>{mn:g} ৳</b> | 🔺 সর্বোচ্চ: <b>{mx:g} ৳</b>\n\n"
            f"এখন আপনার <b>{method} পার্সোনাল নম্বরটি</b> লিখে পাঠান:\n"
            f"<i>(১১ ডিজিটের নম্বর দিন, বাতিল করতে /cancel লিখুন)</i>",
            rows=[[btn("✖️ বাতিল", callback_data="wd_cancel", color="red")]],
        )
        return

    # Withdraw Cancel (one-tap cancel from any withdraw step)
    if cb_data == "wd_cancel":
        context.user_data.clear()
        try:
            await query.message.delete()
        except Exception:
            pass
        await send_reply_menu(
            update,
            context,
            "✅ উইথড্র বাতিল করা হয়েছে।",
            main_menu_reply_rows(user_id),
        )
        return

    # Admin Withdraw Approve / Reject
    if cb_data.startswith("wd_approve_") or cb_data.startswith("wd_reject_"):
        if user_id != ADMIN_ID:
            return
        parts = cb_data.split("_")
        action, wd_id = parts[1], parts[2]
        wd = data["withdraws"].get(wd_id)
        if not wd:
            await query.answer("⚠️ এই রিকোয়েস্ট পাওয়া যায়নি!", show_alert=True)
            return

        if action == "approve":
            ok, err = approve_withdraw(data, wd_id)
            if not ok:
                await query.answer(err, show_alert=True)
                return
            await edit_styled_inline(
                context,
                chat_id,
                message_id,
                query.message.text_html + "\n\n✅ <b>STATUS: APPROVED BY ADMIN</b>",
            )
            await post_payout_log(context, data, "paid", {**wd, "wd_id": wd_id})
            try:
                await context.bot.send_message(
                    chat_id=int(wd["user_id"]),
                    text=(
                        f"✅ <b>আপনার উইথড্র সফলভাবে পেমেন্ট করা হয়েছে!</b>\n\n"
                        f"💳 মেথড: <b>{wd['method']}</b>\n"
                        f"📱 নম্বর: <code>{wd['number']}</code>\n"
                        f"{wd_amount_lines(data, float(wd['amount']))}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            ok, err = reject_withdraw(data, wd_id)
            if not ok:
                await query.answer(err, show_alert=True)
                return
            await edit_styled_inline(
                context,
                chat_id,
                message_id,
                query.message.text_html + "\n\n❌ <b>STATUS: REJECTED (REFUNDED)</b>",
            )
            try:
                await context.bot.send_message(
                    chat_id=int(wd["user_id"]),
                    text=(
                        f"❌ <b>আপনার উইথড্র রিকোয়েস্ট বাতিল করা হয়েছে!</b>\n"
                        f"💰 <b>{wd['amount']} ৳</b> আপনার ব্যালেন্সে ফেরত দেওয়া হয়েছে।"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    # Admin Panel Callbacks
    if cb_data.startswith("adm_"):
        if user_id != ADMIN_ID:
            return

        if cb_data == "adm_stats":
            total_users = len(data["users"])
            banned_users = sum(1 for u in data["users"].values() if u.get("banned"))
            pending_wd = sum(1 for w in data["withdraws"].values() if w["status"] == "pending")
            s = data["settings"]
            mn_b, mx_b = wd_method_limits(data, "bKash")
            mn_n, mx_n = wd_method_limits(data, "Nagad")
            wd_status = "✅ ON" if withdraw_allowed(data) else "❌ OFF"
            fc = get_force_channels(data)
            fc_text = ", ".join(fc) if fc else "Off"
            stats_text = (
                "📊 <b>BOT LIVE STATISTICS</b>\n\n"
                f"👥 মোট ইউজার: <b>{total_users}</b>\n"
                f"🚫 ব্যান ইউজার: <b>{banned_users}</b>\n"
                f"⏳ পেন্ডিং উইথড্র: <b>{pending_wd}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🎁 রেফার বোনাস: <b>{s['refer_bonus']} ৳</b>\n"
                f"💸 উইথড্র: <b>{wd_status}</b>\n"
                f"🩷 bKash: <b>{mn_b:g}–{mx_b:g} ৳</b> ({s.get('wd_bkash_active', 'ON')})\n"
                f"🧡 Nagad: <b>{mn_n:g}–{mx_n:g} ৳</b> ({s.get('wd_nagad_active', 'ON')})\n"
                f"📢 ফোর্স চ্যানেল ({len(fc)}টি): <b>{fc_text}</b>\n"
                f"🆘 সাপোর্ট আইডি: <b>{s['support_id']}</b>"
            )
            await edit_styled_inline(context, chat_id, message_id, stats_text, admin_panel_rows())
            return

        if cb_data == "adm_menu":
            await edit_styled_inline(
                context,
                chat_id,
                message_id,
                "⚙️ <b>ADMIN CONTROL PANEL</b>\n\nনিচের বাটনগুলো থেকে বটের সব সেটিংস কন্ট্রোল করুন:",
                admin_panel_rows(),
            )
            return

        if cb_data == "adm_pending_wd":
            pending = sorted(
                [(int(wid), w) for wid, w in data["withdraws"].items() if w.get("status") == "pending"],
                key=lambda x: x[0],
                reverse=True,
            )[:10]
            if not pending:
                await edit_styled_inline(
                    context, chat_id, message_id,
                    "✅ <b>কোনো পেন্ডিং উইথড্র রিকোয়েস্ট নেই।</b>",
                    [[btn("⬅️ অ্যাডমিন প্যানেল", callback_data="adm_menu", color="blue")]],
                )
                return
            text = "⏳ <b>পেন্ডিং উইথড্র রিকোয়েস্ট</b>\n\n"
            rows = []
            for wid, w in pending:
                text += (
                    f"🆔 <code>#{wid}</code> | {w['method']} | <b>{w['amount']} ৳</b> | "
                    f"<code>{w['number']}</code> | {w.get('user_name', '')}\n"
                )
                rows.append([
                    btn(f"✅ App #{wid}", callback_data=f"wd_approve_{wid}", color="green"),
                    btn(f"❌ Rej #{wid}", callback_data=f"wd_reject_{wid}", color="red"),
                ])
            rows.append([btn("⬅️ অ্যাডমিন প্যানেল", callback_data="adm_menu", color="blue")])
            await edit_styled_inline(context, chat_id, message_id, text, rows)
            return

        if cb_data == "adm_wd_settings":
            await show_wd_settings_page(context, chat_id, message_id, data)
            return

        if cb_data in ("adm_wd_toggle_all", "adm_wd_toggle_bkash", "adm_wd_toggle_nagad"):
            key = {
                "adm_wd_toggle_all": "withdraw_enabled",
                "adm_wd_toggle_bkash": "wd_bkash_active",
                "adm_wd_toggle_nagad": "wd_nagad_active",
            }[cb_data]
            cur = str(data["settings"].get(key, "ON")).upper()
            data["settings"][key] = "OFF" if cur == "ON" else "ON"
            save_data(data)
            await query.answer(f"✅ {key}: {data['settings'][key]}")
            await show_wd_settings_page(context, chat_id, message_id, data)
            return

        if cb_data in ("adm_wd_edit_bkash", "adm_wd_edit_nagad"):
            method = "bKash" if cb_data == "adm_wd_edit_bkash" else "Nagad"
            mn, mx = wd_method_limits(data, method)
            context.user_data["state"] = f"WAIT_ADM_WD_{method.upper()}"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✏️ <b>{method} উইথড্র লিমিট সেট করুন:</b>\n\n"
                    f"বর্তমান সর্বনিম্ন: <code>{mn:g}</code> | সর্বোচ্চ: <code>{mx:g}</code>\n\n"
                    f"ফরম্যাট: <code>সর্বনিম্ন সর্বোচ্চ</code> (যেমন: <code>50 5000</code>)"
                    "\n\n<i>বাতিল করতে /cancel লিখুন</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if cb_data == "adm_wd_edit_charge":
            charge = wd_charge_amount(data)
            context.user_data["state"] = "WAIT_ADM_WD_CHARGE"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✏️ <b>উইথড্র চার্জ সেট করুন:</b>\n\n"
                    f"বর্তমান চার্জ: <code>{charge:g} ৳</code> (প্রতি উইথড্রে)\n\n"
                    "চার্জের পরিমাণ লিখুন (যেমন: <code>10</code> বা <code>0</code>)।\n"
                    "চার্জ ০ থাকলে কোনো টাকা কাটা হবে না।"
                    "\n\n<i>বাতিল করতে /cancel লিখুন</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if cb_data == "adm_payout_log":
            await show_payout_log_page(context, chat_id, message_id, data)
            return

        if cb_data == "adm_log_toggle":
            cur = str(data["settings"].get("payout_log_active", "OFF")).upper()
            data["settings"]["payout_log_active"] = "OFF" if cur == "ON" else "ON"
            save_data(data)
            await query.answer(f"✅ পেআউট লগ: {data['settings']['payout_log_active']}")
            await show_payout_log_page(context, chat_id, message_id, data)
            return

        if cb_data == "adm_log_set_chat":
            context.user_data["state"] = "WAIT_ADM_LOGCHAT"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "💬 <b>পেআউট লগ চ্যাট সেট করুন:</b>\n\n"
                    "লগ চ্যানেল/গ্রুপের <code>chat ID</code> দিন (যেমন: <code>-1001234567890</code>)।\n"
                    "বন্ধ করতে <code>off</code> লিখুন।"
                    "\n\n<i>বাতিল করতে /cancel লিখুন</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if cb_data == "adm_set_channel":
            await show_force_channels_page(context, chat_id, message_id, data)
            return

        if cb_data == "adm_fc_add":
            context.user_data["state"] = "WAIT_ADM_CHANNEL"
            await context.bot.send_message(
                chat_id=chat_id,
                text="📢 ফোর্স জয়েন চ্যানেলের <code>@username</code> দিন (বটকে চ্যানেলে অ্যাডমিন রাখবেন)। সব মুছে ফেলতে <code>off</code> লিখুন:\n\n<i>বাতিল করতে /cancel লিখুন</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        if cb_data.startswith("adm_fc_del_"):
            try:
                idx = int(cb_data.replace("adm_fc_del_", ""))
                channels = get_force_channels(data)
                if 0 <= idx < len(channels):
                    removed = channels.pop(idx)
                    data["settings"]["force_channels"] = channels
                    save_data(data)
                    await query.answer(f"✅ {removed} মুছে ফেলা হয়েছে।")
                else:
                    await query.answer("⚠️ এই চ্যানেলটি পাওয়া যায়নি!", show_alert=True)
            except ValueError:
                await query.answer("⚠️ ভুল বাটন!", show_alert=True)
            await show_force_channels_page(context, chat_id, message_id, data)
            return

        if cb_data == "adm_fc_clear":
            data["settings"]["force_channels"] = []
            data["settings"]["force_channel"] = ""
            save_data(data)
            await query.answer("✅ সব ফোর্স চ্যানেল মুছে ফেলা হয়েছে।")
            await show_force_channels_page(context, chat_id, message_id, data)
            return

        prompts = {
            "adm_set_bonus": ("WAIT_ADM_BONUS", "🎁 প্রতি রেফারে কত টাকা বোনাস দিতে চান? (যেমন: <code>2</code> বা <code>5</code>)"),
            "adm_set_support": ("WAIT_ADM_SUPPORT", "🆘 সাপোর্ট অ্যাডমিনের <code>@username</code> বা লিংক দিন:"),
            "adm_add_bal": ("WAIT_ADM_ADDBAL", "➕ টাকা যোগ করতে <code>USER_ID AMOUNT</code> লিখুন:\n(উদাহরণ: <code>123456789 50</code>)"),
            "adm_cut_bal": ("WAIT_ADM_CUTBAL", "➖ টাকা কাটতে <code>USER_ID AMOUNT</code> লিখুন:\n(উদাহরণ: <code>123456789 20</code>)"),
            "adm_ban": ("WAIT_ADM_BAN", "🚫 ব্যান করতে ইউজারের <code>USER_ID</code> দিন:"),
            "adm_unban": ("WAIT_ADM_UNBAN", "✅ আনব্যান করতে ইউজারের <code>USER_ID</code> দিন:"),
            "adm_broadcast": ("WAIT_ADM_BROADCAST", "📣 সব ইউজারকে পাঠানোর জন্য আপনার মেসেজটি লিখুন:"),
        }

        if cb_data in prompts:
            state, msg = prompts[cb_data]
            context.user_data["state"] = state
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg + "\n\n<i>বাতিল করতে /cancel লিখুন</i>",
                parse_mode=ParseMode.HTML,
            )

# ================= TEXT MESSAGE HANDLER =================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_obj = update.effective_user
    uid = str(user_obj.id)
    text = update.message.text.strip()
    data = load_data()
    user = get_user(data, user_obj.id, user_obj.first_name)

    if user.get("banned") and user_obj.id != ADMIN_ID:
        await update.message.reply_text("🚫 <b>আপনাকে এই বট থেকে ব্যান করা হয়েছে।</b>", parse_mode=ParseMode.HTML)
        return

    if text == "/cancel":
        context.user_data.clear()
        await send_reply_menu(
            update,
            context,
            "✅ বর্তমান কাজ বাতিল করা হয়েছে।",
            main_menu_reply_rows(user_obj.id),
        )
        return

    state = context.user_data.get("state")

    # Admin Input States
    if user_obj.id == ADMIN_ID and state and state.startswith("WAIT_ADM_"):
        context.user_data.clear()
        try:
            if state == "WAIT_ADM_BONUS":
                val = float(text)
                data["settings"]["refer_bonus"] = val
                save_data(data)
                await update.message.reply_text(f"✅ রেফার বোনাস <b>{val} ৳</b> সেট করা হয়েছে!", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_WD_BKASH":
                mn, mx = [float(x) for x in text.split()]
                if mn < 0 or mx <= mn:
                    raise ValueError("সর্বনিম্ন ০ বা তার বেশি এবং সর্বোচ্চ সর্বনিম্নের চেয়ে বড় হতে হবে।")
                data["settings"]["min_withdraw_bkash"] = mn
                data["settings"]["max_withdraw_bkash"] = mx
                save_data(data)
                await update.message.reply_text(f"✅ bKash লিমিট সেট হয়েছে: <b>{mn:g}–{mx:g} ৳</b>", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_WD_NAGAD":
                mn, mx = [float(x) for x in text.split()]
                if mn < 0 or mx <= mn:
                    raise ValueError("সর্বনিম্ন ০ বা তার বেশি এবং সর্বোচ্চ সর্বনিম্নের চেয়ে বড় হতে হবে।")
                data["settings"]["min_withdraw_nagad"] = mn
                data["settings"]["max_withdraw_nagad"] = mx
                save_data(data)
                await update.message.reply_text(f"✅ Nagad লিমিট সেট হয়েছে: <b>{mn:g}–{mx:g} ৳</b>", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_WD_CHARGE":
                val = float(text)
                if val < 0:
                    raise ValueError("চার্জ ০ বা তার বেশি হতে হবে।")
                data["settings"]["wd_charge"] = val
                save_data(data)
                await update.message.reply_text(f"✅ উইথড্র চার্জ সেট হয়েছে: <b>{val:g} ৳</b>", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_LOGCHAT":
                data["settings"]["payout_log_chat"] = "" if text.lower() == "off" else text
                save_data(data)
                await update.message.reply_text(f"✅ পেআউট লগ চ্যাট আপডেট হয়েছে: <b>{data['settings']['payout_log_chat'] or 'Off'}</b>", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_CHANNEL":
                if text.lower() == "off":
                    data["settings"]["force_channels"] = []
                    data["settings"]["force_channel"] = ""
                    save_data(data)
                    await update.message.reply_text("✅ সব ফোর্স চ্যানেল মুছে ফেলা হয়েছে — ফোর্স জয়েন এখন <b>Off</b>।", parse_mode=ParseMode.HTML)
                else:
                    ch = text.strip()
                    if not ch.startswith("@"):
                        ch = "@" + ch
                    channels = get_force_channels(data)
                    if ch.lower() in [c.lower() for c in channels]:
                        await update.message.reply_text(f"❌ <code>{ch}</code> চ্যানেলটি আগেই যোগ করা আছে।", parse_mode=ParseMode.HTML)
                    else:
                        channels.append(ch)
                        data["settings"]["force_channels"] = channels
                        data["settings"]["force_channel"] = ""
                        save_data(data)
                        await update.message.reply_text(f"✅ ফোর্স চ্যানেল যোগ হয়েছে: <code>{ch}</code> (মোট <b>{len(channels)}টি</b>)", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_SUPPORT":
                data["settings"]["support_id"] = text
                save_data(data)
                await update.message.reply_text(f"✅ সাপোর্ট আইডি আপডেট হয়েছে: <b>{text}</b>", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_ADDBAL":
                target_id, amt = text.split()
                amt = float(amt)
                if target_id in data["users"]:
                    data["users"][target_id]["balance"] += amt
                    save_data(data)
                    await update.message.reply_text(f"✅ ইউজার <code>{target_id}</code>-এর ব্যালেন্সে <b>+{amt} ৳</b> যোগ হয়েছে!", parse_mode=ParseMode.HTML)
                else:
                    await update.message.reply_text("❌ এই আইডির কোনো ইউজার পাওয়া যায়নি।")

            elif state == "WAIT_ADM_CUTBAL":
                target_id, amt = text.split()
                amt = float(amt)
                if target_id in data["users"]:
                    data["users"][target_id]["balance"] = max(0.0, data["users"][target_id]["balance"] - amt)
                    save_data(data)
                    await update.message.reply_text(f"✅ ইউজার <code>{target_id}</code>-এর ব্যালেন্স থেকে <b>-{amt} ৳</b> কাটা হয়েছে!", parse_mode=ParseMode.HTML)
                else:
                    await update.message.reply_text("❌ এই আইডির কোনো ইউজার পাওয়া যায়নি।")

            elif state == "WAIT_ADM_BAN":
                if text in data["users"]:
                    data["users"][text]["banned"] = True
                    save_data(data)
                    await update.message.reply_text(f"🚫 ইউজার <code>{text}</code>-কে ব্যান করা হয়েছে!", parse_mode=ParseMode.HTML)
                else:
                    await update.message.reply_text("❌ ইউজার পাওয়া যায়নি।")

            elif state == "WAIT_ADM_UNBAN":
                if text in data["users"]:
                    data["users"][text]["banned"] = False
                    save_data(data)
                    await update.message.reply_text(f"✅ ইউজার <code>{text}</code>-কে আনব্যান করা হয়েছে!", parse_mode=ParseMode.HTML)
                else:
                    await update.message.reply_text("❌ ইউজার পাওয়া যায়নি।")

            elif state == "WAIT_ADM_BROADCAST":
                sent, failed = 0, 0
                await update.message.reply_text("⏳ ব্রডকাস্ট পাঠানো শুরু হয়েছে...")
                for u_key in list(data["users"].keys()):
                    try:
                        await context.bot.send_message(
                            chat_id=int(u_key),
                            text=f"📢 <b>Admin Notice:</b>\n\n{text}",
                            parse_mode=ParseMode.HTML,
                        )
                        sent += 1
                    except Exception:
                        failed += 1
                await update.message.reply_text(
                    f"✅ <b>ব্রডকাস্ট সম্পন্ন!</b>\n📤 সফল: <b>{sent}</b>\n❌ ব্যর্থ: <b>{failed}</b>",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            await update.message.reply_text(f"⚠️ ভুল ফরম্যাট দেওয়া হয়েছে! আবার চেষ্টা করুন।\nError: {e}")
        return

    if not await check_force_join(user_obj.id, context, data):
        await send_force_join_message(update, context, data)
        return

    # User Withdraw States
    if state == "WAIT_WD_NUMBER":
        method = context.user_data.get("wd_method", "bKash")
        if not (text.isdigit() and len(text) == 11):
            await update.message.reply_text(
                "❌ সঠিক ১১ ডিজিটের পার্সোনাল নম্বর দিন (যেমন: 01XXXXXXXXX):"
            )
            return
        context.user_data["wd_number"] = text
        context.user_data["state"] = "WAIT_WD_AMOUNT"
        mn, mx = wd_method_limits(data, method)
        await send_styled_inline(
            context,
            update.effective_chat.id,
            f"💰 আপনার বর্তমান ব্যালেন্স: <b>{user['balance']} ৳</b>\n"
            f"🔻 সর্বনিম্ন: <b>{mn:g} ৳</b> | 🔺 সর্বোচ্চ: <b>{mx:g} ৳</b>\n\n"
            f"কত টাকা উইথড্র করতে চান তা লিখুন:",
            rows=[[btn("✖️ বাতিল", callback_data="wd_cancel", color="red")]],
        )
        return

    if state == "WAIT_WD_AMOUNT":
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("⚠️ দয়া করে সঠিক সংখ্যায় টাকার পরিমাণ লিখুন (যেমন: 100):")
            return

        method = context.user_data.get("wd_method", "bKash")
        ok, err = wd_validate_amount(data, method, amount, float(user["balance"]))
        if not ok:
            await update.message.reply_text(err, parse_mode=ParseMode.HTML)
            return

        number = context.user_data.get("wd_number", "")
        context.user_data.clear()

        wd_id = create_withdraw(data, uid, user_obj.first_name, method, number, amount)

        await send_reply_menu(
            update,
            context,
            f"✅ <b>আপনার উইথড্র রিকোয়েস্ট সফলভাবে জমা হয়েছে!</b>\n\n"
            f"🆔 রিকোয়েস্ট আইডি: <code>#{wd_id}</code>\n"
            f"💳 মেথড: <b>{method}</b>\n"
            f"📱 নম্বর: <code>{number}</code>\n"
            f"💰 পরিমাণ: <b>{amount} ৳</b>\n\n"
            f"⏳ অ্যাডমিন চেক করে দ্রুত পেমেন্ট করে দেবেন।",
            main_menu_reply_rows(user_obj.id),
        )

        admin_rows = [
            [
                btn("✅ Approve", callback_data=f"wd_approve_{wd_id}", color="green"),
                btn("❌ Reject", callback_data=f"wd_reject_{wd_id}", color="red"),
            ]
        ]
        try:
            await send_styled_inline(
                context,
                ADMIN_ID,
                f"🔔 <b>NEW WITHDRAW REQUEST</b>\n\n"
                f"👤 ইউজার: <b>{user_obj.first_name}</b> (<code>{uid}</code>)\n"
                f"👥 মোট রেফার: <b>{user['refers']}</b>\n"
                f"💳 মেথড: <b>{method}</b>\n"
                f"📱 নম্বর: <code>{number}</code>\n"
                f"{wd_amount_lines(data, amount)}",
                rows=admin_rows,
            )
        except Exception as e:
            logger.error(f"Failed to send admin withdraw alert: {e}")
        await post_payout_log(context, data, "pending", {
            "wd_id": wd_id, "user_id": uid, "user_name": user_obj.first_name,
            "method": method, "number": number, "amount": amount,
        })
        return

    # Reply Keyboard Menu Handlers
    if text == "🔗 Refer & Earn":
        bot_username = context.bot.username
        ref_link = f"https://t.me/{bot_username}?start={uid}"
        bonus = data["settings"]["refer_bonus"]
        share_url = f"https://t.me/share/url?url={ref_link}&text=🔥 প্রতি রেফারে {bonus} টাকা আয় করুন!"
        rows = [[btn("📤 বন্ধুদের সাথে শেয়ার করুন", url=share_url, color="green")]]
        ref_text = (
            f"🔗 <b>আপনার রেফারেল লিংক:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"🎁 প্রতি সফল রেফারে পাবেন: <b>{bonus} ৳</b>\n"
            f"👥 আপনার মোট রেফার: <b>{user['refers']} জন</b>"
        )
        await send_styled_inline(context, update.effective_chat.id, ref_text, rows)

    elif text == "💰 Balance":
        stats = user_wallet_stats(data, uid)
        bal_text = (
            f"👤 <b>আপনার অ্যাকাউন্ট তথ্য</b>\n\n"
            f"🆔 ইউজার আইডি: <code>{uid}</code>\n"
            f"💰 বর্তমান ব্যালেন্স: <b>{stats['balance']} ৳</b>\n"
            f"⏳ পেন্ডিং উইথড্র: <b>{stats['pending_withdraw']} ৳</b>\n"
            f"🎁 আজীবন আয়: <b>{stats['earned']} ৳</b>\n"
            f"👥 মোট সফল রেফার: <b>{user['refers']} জন</b>\n"
            f"🎁 প্রতি রেফার বোনাস: <b>{data['settings']['refer_bonus']} ৳</b>\n\n"
            f"💸 উইথড্র হিসাব — ✅ অনুমোদিত: <b>{stats['approved']}</b> | "
            f"⏳ পেন্ডিং: <b>{stats['pending_count']}</b> | "
            f"❌ বাতিল: <b>{stats['rejected']}</b>"
        )
        await update.message.reply_text(bal_text, parse_mode=ParseMode.HTML)

    elif text == "💸 Withdraw":
        if not withdraw_allowed(data):
            await update.message.reply_text(
                "❌ <b>উইথড্র এখন বন্ধ আছে!</b>\n\nঅ্যাডমিন উইথড্র বন্ধ রেখেছেন। পরে আবার চেষ্টা করুন।",
                parse_mode=ParseMode.HTML,
            )
            return
        active = wd_active_methods(data)
        min_wd = min(wd_method_limits(data, m)[0] for m in active)
        if user["balance"] < min_wd:
            await update.message.reply_text(
                f"❌ <b>পর্যাপ্ত ব্যালেন্স নেই!</b>\n\n"
                f"💰 আপনার ব্যালেন্স: <b>{user['balance']} ৳</b>\n"
                f"📉 সর্বনিম্ন উইথড্র: <b>{min_wd:g} ৳</b>",
                parse_mode=ParseMode.HTML,
            )
            return

        charge = wd_charge_amount(data)
        rows = []
        for m in active:
            mn, mx = wd_method_limits(data, m)
            color = "red" if m == "bKash" else "green"
            display = "Bkash" if m == "bKash" else "Nagad"
            rows.append([btn(f"💳 {display} -> Min: {mn:g}৳ (-{charge:g}৳)", callback_data=f"wd_method_{m}", color=color)])
        rows.append([btn("✖️ বাতিল", callback_data="wd_cancel", color="red")])
        await send_styled_inline(context, update.effective_chat.id, "💳 <b>পেমেন্ট মেথড সিলেক্ট করুন:</b>", rows)

    elif text == "📞 Support":
        sup = data["settings"]["support_id"]
        url = sup if sup.startswith("http") else f"https://t.me/{sup.lstrip('@')}"
        rows = [[btn("💬 Contact Support", url=url, color="green")]]
        await send_styled_inline(
            context,
            update.effective_chat.id,
            f"🆘 <b>যেকোনো সমস্যা বা সাহায্যের জন্য সাপোর্টে যোগাযোগ করুন:</b>\n👉 {sup}",
            rows,
        )

    elif text == "⚙️ Admin Panel" and user_obj.id == ADMIN_ID:
        await admin_command(update, context)

# ================= MAIN RUNNER =================
def main():
    Thread(target=run_flask, daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logger.info("Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)

if __name__ == "__main__":
    main()