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
        "min_withdraw": 20.0,
        "force_channel": "",
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
        return json.load(f)

def save_data(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_user(data, user_id, name="User"):
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = {
            "name": name,
            "balance": 0.0,
            "refers": 0,
            "referred_by": None,
            "refer_paid": False,
            "banned": False,
        }
    return data["users"][uid]

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
            btn("🎁 Set Refer Bonus", callback_data="adm_set_bonus", color="green"),
            btn("💳 Set Min Withdraw", callback_data="adm_set_minwd", color="blue"),
        ],
        [
            btn("📢 Set Force Channel", callback_data="adm_set_channel", color="blue"),
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

# ================= FORCE JOIN CHECK =================
async def check_force_join(user_id: int, context: ContextTypes.DEFAULT_TYPE, data: dict) -> bool:
    channel = data["settings"].get("force_channel", "").strip()
    if not channel or user_id == ADMIN_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Force join check error for {channel}: {e}")
        return True

async def send_force_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    channel = data["settings"].get("force_channel", "").strip()
    clean_username = channel.lstrip("@")
    join_url = f"https://t.me/{clean_username}"
    rows = [
        [btn("📢 Join Our Channel", url=join_url, color="blue")],
        [btn("✅ Joined / Check", callback_data="check_join", color="green")],
    ]
    text = (
        "🚫 <b>Access Denied!</b>\n\n"
        "বটটি ব্যবহার করতে হলে অবশ্যই নিচের চ্যানেলে জয়েন করতে হবে।\n"
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
        if method not in ["bKash", "Nagad"]:
            await query.answer("❌ শুধুমাত্র bKash এবং Nagad সাপোর্ট করে।", show_alert=True)
            return
        context.user_data["wd_method"] = method
        context.user_data["state"] = "WAIT_WD_NUMBER"
        await edit_styled_inline(
            context,
            chat_id,
            message_id,
            f"📲 <b>{method}</b> সিলেক্ট করা হয়েছে।\n\n"
            f"এখন আপনার <b>{method} পার্সোনাল নম্বরটি</b> লিখে পাঠান:\n"
            f"<i>(বাতিল করতে /cancel লিখুন)</i>",
        )
        return

    # Admin Withdraw Approve / Reject
    if cb_data.startswith("wd_approve_") or cb_data.startswith("wd_reject_"):
        if user_id != ADMIN_ID:
            return
        parts = cb_data.split("_")
        action, wd_id = parts[1], parts[2]
        wd = data["withdraws"].get(wd_id)
        if not wd or wd["status"] != "pending":
            await query.answer("⚠️ এই রিকোয়েস্ট আগেই প্রসেস করা হয়েছে!", show_alert=True)
            return

        target_uid = wd["user_id"]
        amount = wd["amount"]

        if action == "approve":
            wd["status"] = "approved"
            save_data(data)
            await edit_styled_inline(
                context,
                chat_id,
                message_id,
                query.message.text_html + "\n\n✅ <b>STATUS: APPROVED BY ADMIN</b>",
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=(
                        f"✅ <b>আপনার উইথড্র সফলভাবে পেমেন্ট করা হয়েছে!</b>\n\n"
                        f"🆔 ট্রানজেকশন আইডি: <code>#{wd_id}</code>\n"
                        f"💳 মেথড: <b>{wd['method']}</b>\n"
                        f"📱 নম্বর: <code>{wd['number']}</code>\n"
                        f"💰 পরিমাণ: <b>{amount} ৳</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            wd["status"] = "rejected"
            if target_uid in data["users"]:
                data["users"][target_uid]["balance"] += amount
            save_data(data)
            await edit_styled_inline(
                context,
                chat_id,
                message_id,
                query.message.text_html + "\n\n❌ <b>STATUS: REJECTED (REFUNDED)</b>",
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_uid),
                    text=(
                        f"❌ <b>আপনার উইথড্র রিকোয়েস্ট বাতিল করা হয়েছে!</b>\n"
                        f"💰 <b>{amount} ৳</b> আপনার ব্যালেন্সে ফেরত দেওয়া হয়েছে।"
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
            stats_text = (
                "📊 <b>BOT LIVE STATISTICS</b>\n\n"
                f"👥 মোট ইউজার: <b>{total_users}</b>\n"
                f"🚫 ব্যান ইউজার: <b>{banned_users}</b>\n"
                f"⏳ পেন্ডিং উইথড্র: <b>{pending_wd}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🎁 রেফার বোনাস: <b>{s['refer_bonus']} ৳</b>\n"
                f"💳 মিনিমাম উইথড্র: <b>{s['min_withdraw']} ৳</b>\n"
                f"📢 ফোর্স চ্যানেল: <b>{s['force_channel'] or 'Off'}</b>\n"
                f"🆘 সাপোর্ট আইডি: <b>{s['support_id']}</b>"
            )
            await edit_styled_inline(context, chat_id, message_id, stats_text, admin_panel_rows())
            return

        prompts = {
            "adm_set_bonus": ("WAIT_ADM_BONUS", "🎁 প্রতি রেফারে কত টাকা বোনাস দিতে চান? (যেমন: <code>2</code> বা <code>5</code>)"),
            "adm_set_minwd": ("WAIT_ADM_MINWD", "💳 মিনিমাম উইথড্র কত টাকা সেট করতে চান? (যেমন: <code>20</code> বা <code>50</code>)"),
            "adm_set_channel": ("WAIT_ADM_CHANNEL", "📢 ফোর্স জয়েন চ্যানেলের <code>@username</code> দিন (বটকে চ্যানেলে অ্যাডমিন রাখবেন)। অফ করতে <code>off</code> লিখুন:"),
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

            elif state == "WAIT_ADM_MINWD":
                val = float(text)
                data["settings"]["min_withdraw"] = val
                save_data(data)
                await update.message.reply_text(f"✅ মিনিমাম উইথড্র <b>{val} ৳</b> সেট করা হয়েছে!", parse_mode=ParseMode.HTML)

            elif state == "WAIT_ADM_CHANNEL":
                data["settings"]["force_channel"] = "" if text.lower() == "off" else text
                save_data(data)
                await update.message.reply_text(f"✅ ফোর্স চ্যানেল আপডেট হয়েছে: <b>{data['settings']['force_channel'] or 'Off'}</b>", parse_mode=ParseMode.HTML)

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
        context.user_data["wd_number"] = text
        context.user_data["state"] = "WAIT_WD_AMOUNT"
        min_wd = data["settings"]["min_withdraw"]
        await update.message.reply_text(
            f"💰 আপনার বর্তমান ব্যালেন্স: <b>{user['balance']} ৳</b>\n"
            f"📉 মিনিমাম উইথড্র: <b>{min_wd} ৳</b>\n\n"
            f"কত টাকা উইথড্র করতে চান তা লিখুন:",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "WAIT_WD_AMOUNT":
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("⚠️ দয়া করে সঠিক সংখ্যায় টাকার পরিমাণ লিখুন (যেমন: 20):")
            return

        min_wd = float(data["settings"]["min_withdraw"])
        if amount < min_wd:
            await update.message.reply_text(f"❌ মিনিমাম উইথড্র <b>{min_wd} ৳</b>! আবার পরিমাণ লিখুন:", parse_mode=ParseMode.HTML)
            return
        if amount > user["balance"]:
            await update.message.reply_text("❌ আপনার অ্যাকাউন্টে পর্যাপ্ত ব্যালেন্স নেই! /cancel দিয়ে বাতিল করুন।")
            return

        method = context.user_data.get("wd_method", "bKash")
        number = context.user_data.get("wd_number", "")
        context.user_data.clear()

        user["balance"] -= amount
        data["withdraw_counter"] += 1
        wd_id = str(data["withdraw_counter"])
        data["withdraws"][wd_id] = {
            "user_id": uid,
            "user_name": user_obj.first_name,
            "method": method,
            "number": number,
            "amount": amount,
            "status": "pending",
        }
        save_data(data)

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
                f"🔔 <b>NEW WITHDRAW REQUEST (#{wd_id})</b>\n\n"
                f"👤 ইউজার: <b>{user_obj.first_name}</b> (<code>{uid}</code>)\n"
                f"👥 মোট রেফার: <b>{user['refers']}</b>\n"
                f"💳 মেথড: <b>{method}</b>\n"
                f"📱 নম্বর: <code>{number}</code>\n"
                f"💰 পরিমাণ: <b>{amount} ৳</b>",
                rows=admin_rows,
            )
        except Exception as e:
            logger.error(f"Failed to send admin withdraw alert: {e}")
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
        bal_text = (
            f"👤 <b>আপনার অ্যাকাউন্ট তথ্য</b>\n\n"
            f"🆔 ইউজার আইডি: <code>{uid}</code>\n"
            f"💰 বর্তমান ব্যালেন্স: <b>{user['balance']} ৳</b>\n"
            f"👥 মোট সফল রেফার: <b>{user['refers']} জন</b>\n"
            f"🎁 প্রতি রেফার বোনাস: <b>{data['settings']['refer_bonus']} ৳</b>"
        )
        await update.message.reply_text(bal_text, parse_mode=ParseMode.HTML)

    elif text == "💸 Withdraw":
        min_wd = float(data["settings"]["min_withdraw"])
        if user["balance"] < min_wd:
            await update.message.reply_text(
                f"❌ <b>পর্যাপ্ত ব্যালেন্স নেই!</b>\n\n"
                f"💰 আপনার ব্যালেন্স: <b>{user['balance']} ৳</b>\n"
                f"📉 মিনিমাম উইথড্র: <b>{min_wd} ৳</b>",
                parse_mode=ParseMode.HTML,
            )
            return

        rows = [[
            btn("🩷 bKash", callback_data="wd_method_bKash", color="red"),
            btn("🧡 Nagad", callback_data="wd_method_Nagad", color="green"),
        ]]
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