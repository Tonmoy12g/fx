import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
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

# ==================== কনফিগারেশন (Environment Variables) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "123456789").split(",")
    if x.strip().isdigit()
]
PAYOUT_LOG_CHANNEL = os.getenv("PAYOUT_LOG_CHANNEL", "")  # যেমন: "-1001234567890"
DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")

DEFAULT_DATA: Dict[str, Any] = {
    "users": {},
    "withdraws": {},
    "next_wid": 1001,
    "settings": {
        "bkash": {
            "enabled": True,
            "min": 100.0,
            "max": 25000.0,
            "fee_type": "fixed",  # "fixed" অথবা "percent"
            "fee": 10.0,
        },
        "nagad": {
            "enabled": True,
            "min": 100.0,
            "max": 25000.0,
            "fee_type": "fixed",  # "fixed" অথবা "percent"
            "fee": 10.0,
        },
    },
}


def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k, v in DEFAULT_DATA.items():
                if k not in loaded:
                    loaded[k] = v
            for m in ("bkash", "nagad"):
                if m not in loaded["settings"]:
                    loaded["settings"][m] = DEFAULT_DATA["settings"][m].copy()
            return loaded
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    return json.loads(json.dumps(DEFAULT_DATA))


def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


DB = load_data()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def ensure_user(user_id: int, username: str = "", full_name: str = "") -> Dict[str, Any]:
    uid = str(user_id)
    if uid not in DB["users"]:
        DB["users"][uid] = {
            "balance": 500.0,  # নতুন ইউজারের ডিফল্ট ব্যালেন্স (প্রয়োজনে 0.0 করুন)
            "username": username or "",
            "name": full_name or "",
        }
        save_data(DB)
    else:
        if username:
            DB["users"][uid]["username"] = username
        if full_name:
            DB["users"][uid]["name"] = full_name
    return DB["users"][uid]


def calc_fee(method: str, amount: float) -> float:
    cfg = DB["settings"][method]
    fee_val = float(cfg.get("fee", 0.0))
    if cfg.get("fee_type") == "percent":
        return round((amount * fee_val) / 100.0, 2)
    return round(fee_val, 2)


def method_label(method: str) -> str:
    return "bKash" if method == "bkash" else "Nagad"


# ==================== কীবোর্ড বিল্ডার ====================
def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💸 Withdraw", callback_data="wd_menu")],
        [InlineKeyboardButton("📜 My Withdraws", callback_data="my_wd")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def withdraw_method_kb() -> InlineKeyboardMarkup:
    rows = []
    for m in ("bkash", "nagad"):
        cfg = DB["settings"][m]
        status_txt = "" if cfg["enabled"] else " (OFF)"
        rows.append(
            [InlineKeyboardButton(f"{method_label(m)}{status_txt}", callback_data=f"wd_pick:{m}")]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    pending_count = sum(1 for w in DB["withdraws"].values() if w["status"] == "pending")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"📥 Pending Withdraws ({pending_count})", callback_data="adm_pending:0"
                )
            ],
            [InlineKeyboardButton("⚙️ System Settings", callback_data="adm_settings")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")],
        ]
    )


def admin_settings_kb() -> InlineKeyboardMarkup:
    bk = DB["settings"]["bkash"]
    ng = DB["settings"]["nagad"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"bKash: {'ON ✅' if bk['enabled'] else 'OFF ❌'}",
                    callback_data="adm_toggle:bkash",
                ),
                InlineKeyboardButton(
                    f"Nagad: {'ON ✅' if ng['enabled'] else 'OFF ❌'}",
                    callback_data="adm_toggle:nagad",
                ),
            ],
            [
                InlineKeyboardButton("✏️ Edit bKash", callback_data="adm_edit_menu:bkash"),
                InlineKeyboardButton("✏️ Edit Nagad", callback_data="adm_edit_menu:nagad"),
            ],
            [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")],
        ]
    )


def edit_method_kb(method: str) -> InlineKeyboardMarkup:
    lbl = method_label(method)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"Min/Max ({lbl})", callback_data=f"adm_set:{method}:limits"),
                InlineKeyboardButton(f"Charge ({lbl})", callback_data=f"adm_set:{method}:fee"),
            ],
            [InlineKeyboardButton("⬅️ Settings", callback_data="adm_settings")],
        ]
    )


def pending_action_kb(wid: str, idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_app:{wid}:{idx}"),
                InlineKeyboardButton("📸 Pay + Proof", callback_data=f"adm_proof:{wid}:{idx}"),
            ],
            [InlineKeyboardButton("❌ Reject (Refund)", callback_data=f"adm_rej:{wid}:{idx}")],
            [
                InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_pending:{max(0, idx - 1)}"),
                InlineKeyboardButton("Next ➡️", callback_data=f"adm_pending:{idx + 1}"),
            ],
            [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
        ]
    )


# ==================== ইউজার কমান্ড ও ফ্লো ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    u_data = ensure_user(user.id, user.username or "", user.full_name or "")
    context.user_data.clear()

    text = (
        f"👋 স্বাগতম, <b>{user.full_name}</b>!\n\n"
        f"💰 বর্তমান ব্যালেন্স: <b>{u_data['balance']:.2f} BDT</b>\n"
        f"নিচের মেনু থেকে Withdraw বা অপশন বেছে নিন।"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_kb(user.id))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ এই কমান্ডটি শুধু অ্যাডমিনের জন্য।")
        return
    await update.message.reply_text(
        "🛠️ <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_panel_kb()
    )


async def addbal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """অ্যাডমিন হেল্পার: /addbal <user_id> <amount>"""
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("ব্যবহার: <code>/addbal 123456789 500</code>", parse_mode="HTML")
        return
    target_uid = context.args[0].strip()
    try:
        amt = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ সঠিক অ্যামাউন্ট দিন।")
        return

    if target_uid not in DB["users"]:
        DB["users"][target_uid] = {"balance": 0.0, "username": "", "name": ""}
    DB["users"][target_uid]["balance"] = round(DB["users"][target_uid]["balance"] + amt, 2)
    save_data(DB)
    await update.message.reply_text(
        f"✅ ইউজার <code>{target_uid}</code>-এর নতুন ব্যালেন্স: <b>{DB['users'][target_uid]['balance']:.2f} BDT</b>",
        parse_mode="HTML",
    )


async def show_withdraw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    u_data = ensure_user(user.id, user.username or "", user.full_name or "")
    context.user_data.pop("wd_flow", None)

    lines = [
        "💸 <b>Withdraw Method নির্বাচন করুন</b>",
        f"💰 ব্যালেন্স: <b>{u_data['balance']:.2f} BDT</b>\n",
    ]
    for m in ("bkash", "nagad"):
        cfg = DB["settings"][m]
        st = "🟢 Active" if cfg["enabled"] else "🔴 Disabled"
        fee_str = (
            f"{cfg['fee']}%" if cfg.get("fee_type") == "percent" else f"{cfg['fee']:.2f} BDT"
        )
        lines.append(
            f"• <b>{method_label(m)}</b> ({st}): Min <b>{cfg['min']:.2f}</b> | Max <b>{cfg['max']:.2f}</b> | Fee: <b>{fee_str}</b>"
        )

    await query.edit_message_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=withdraw_method_kb()
    )


async def pick_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[1]
    cfg = DB["settings"][method]

    if not cfg["enabled"]:
        await query.edit_message_text(
            f"❌ বর্তমানে <b>{method_label(method)}</b> উইথড্র বন্ধ আছে।",
            parse_mode="HTML",
            reply_markup=withdraw_method_kb(),
        )
        return

    context.user_data["wd_flow"] = {"step": "account", "method": method}
    await query.edit_message_text(
        f"📱 <b>{method_label(method)} Withdraw</b>\n\n"
        f"আপনার <b>{method_label(method)}</b> অ্যাকাউন্ট নম্বরটি লিখে পাঠান (যেমন: <code>017XXXXXXXX</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="wd_menu")]]
        ),
    )


async def show_my_withdraws(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid_str = str(query.from_user.id)
    user_wds = [w for w in DB["withdraws"].values() if w["user_id"] == uid_str]
    user_wds.sort(key=lambda x: x["id"], reverse=True)

    if not user_wds:
        text = "📜 আপনার কোনো উইথড্র রিকোয়েস্ট নেই।"
    else:
        lines = ["📜 <b>আপনার সাম্প্রতিক উইথড্রসমূহ:</b>\n"]
        badge = {"pending": "⏳ Pending", "approved": "✅ Approved", "rejected": "❌ Rejected"}
        for w in user_wds[:10]:
            lines.append(
                f"#{w['id']} | <b>{method_label(w['method'])}</b> | "
                f"<b>{w['amount']:.2f} BDT</b> | {badge.get(w['status'], w['status'])}"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]]
        ),
    )


async def send_payout_log(context: ContextTypes.DEFAULT_TYPE, wd: Dict[str, Any], extra_note: str = "") -> None:
    if not PAYOUT_LOG_CHANNEL:
        return
    try:
        status_map = {"pending": "⏳ PENDING", "approved": "✅ PAID", "rejected": "❌ REJECTED"}
        msg = (
            f"🧾 <b>Withdraw Log #{wd['id']}</b>\n"
            f"Status: <b>{status_map.get(wd['status'], wd['status'])}</b>\n"
            f"User: <code>{wd['user_id']}</code> ({wd.get('username') or wd.get('name') or 'N/A'})\n"
            f"Method: <b>{method_label(wd['method'])}</b>\n"
            f"Account: <code>{wd['account']}</code>\n"
            f"Amount: <b>{wd['amount']:.2f} BDT</b>\n"
            f"Fee: <b>{wd['fee']:.2f} BDT</b>\n"
            f"Net Payable: <b>{wd['net_amount']:.2f} BDT</b>\n"
            f"Time: <code>{wd['created_at']}</code>"
        )
        if extra_note:
            msg += f"\nNote: {extra_note}"
        await context.bot.send_message(chat_id=PAYOUT_LOG_CHANNEL, text=msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to post payout log: {e}")


# ==================== অ্যাডমিন কন্ট্রোলস ====================
async def show_pending_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int) -> None:
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admin only", show_alert=True)
        return
    await query.answer()

    pending_list = [w for w in DB["withdraws"].values() if w["status"] == "pending"]
    if not pending_list:
        await query.edit_message_text(
            "✅ বর্তমানে কোনো <b>Pending</b> উইথড্র রিকোয়েস্ট নেই।",
            parse_mode="HTML",
            reply_markup=admin_panel_kb(),
        )
        return

    idx = max(0, min(idx, len(pending_list) - 1))
    wd = pending_list[idx]
    text = (
        f"📥 <b>Pending Withdraw #{wd['id']}</b> ({idx + 1}/{len(pending_list)})\n\n"
        f"👤 User ID: <code>{wd['user_id']}</code>\n"
        f"🏷️ Username: {wd.get('username') or 'N/A'}\n"
        f"💳 Method: <b>{method_label(wd['method'])}</b>\n"
        f"📱 Account: <code>{wd['account']}</code>\n"
        f"💵 Request Amount: <b>{wd['amount']:.2f} BDT</b>\n"
        f"🧾 Fee: <b>{wd['fee']:.2f} BDT</b>\n"
        f"💸 Payable to User: <b>{wd['net_amount']:.2f} BDT</b>\n"
        f"🕒 Time: <code>{wd['created_at']}</code>"
    )
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=pending_action_kb(str(wd["id"]), idx)
    )


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user = query.from_user

    if data == "main_menu":
        await query.answer()
        u_data = ensure_user(user.id, user.username or "", user.full_name or "")
        context.user_data.clear()
        await query.edit_message_text(
            f"👋 <b>Main Menu</b>\n💰 ব্যালেন্স: <b>{u_data['balance']:.2f} BDT</b>",
            parse_mode="HTML",
            reply_markup=main_menu_kb(user.id),
        )
        return

    if data == "wd_menu":
        await show_withdraw_menu(update, context)
        return

    if data.startswith("wd_pick:"):
        await pick_withdraw_method(update, context)
        return

    if data == "my_wd":
        await show_my_withdraws(update, context)
        return

    # --- Admin Panel ---
    if data == "admin_panel":
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        await query.answer()
        context.user_data.pop("admin_action", None)
        await query.edit_message_text(
            "🛠️ <b>Admin Panel</b>", parse_mode="HTML", reply_markup=admin_panel_kb()
        )
        return

    if data.startswith("adm_pending:"):
        idx = int(data.split(":")[1])
        await show_pending_withdraw(update, context, idx)
        return

    if data == "adm_settings":
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        await query.answer()
        context.user_data.pop("admin_action", None)
        bk = DB["settings"]["bkash"]
        ng = DB["settings"]["nagad"]
        text = (
            "⚙️ <b>Withdraw System Settings (bKash & Nagad)</b>\n\n"
            f"<b>bKash</b>: {'ON' if bk['enabled'] else 'OFF'} | Min: {bk['min']} | Max: {bk['max']} | Fee: {bk['fee']} ({bk['fee_type']})\n"
            f"<b>Nagad</b>: {'ON' if ng['enabled'] else 'OFF'} | Min: {ng['min']} | Max: {ng['max']} | Fee: {ng['fee']} ({ng['fee_type']})"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_settings_kb())
        return

    if data.startswith("adm_toggle:"):
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        method = data.split(":")[1]
        DB["settings"][method]["enabled"] = not DB["settings"][method]["enabled"]
        save_data(DB)
        await query.answer(f"{method_label(method)} updated!")
        query.data = "adm_settings"
        await handle_callbacks(update, context)
        return

    if data.startswith("adm_edit_menu:"):
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        await query.answer()
        method = data.split(":")[1]
        await query.edit_message_text(
            f"✏️ <b>{method_label(method)} Settings</b>\nকী আপডেট করতে চান তা বেছে নিন:",
            parse_mode="HTML",
            reply_markup=edit_method_kb(method),
        )
        return

    if data.startswith("adm_set:"):
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        await query.answer()
        _, method, field = data.split(":")
        context.user_data["admin_action"] = {"type": "set_config", "method": method, "field": field}
        if field == "limits":
            prompt = (
                f"<b>{method_label(method)} Min/Max Limit</b> লিখুন\n"
                f"ফরম্যাট: <code>min max</code> (যেমন: <code>100 20000</code>)"
            )
        else:
            prompt = (
                f"<b>{method_label(method)} Charge</b> লিখুন\n"
                f"ফরম্যাট: <code>fixed 10</code> অথবা <code>percent 2</code>"
            )
        await query.edit_message_text(
            prompt,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="adm_settings")]]
            ),
        )
        return

    if data.startswith("adm_app:"):
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        _, wid, idx_s = data.split(":")
        wd = DB["withdraws"].get(wid)
        if not wd or wd["status"] != "pending":
            await query.answer("Already processed or not found", show_alert=True)
            await show_pending_withdraw(update, context, int(idx_s))
            return
        wd["status"] = "approved"
        wd["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_data(DB)
        await query.answer("✅ Approved!")

        try:
            await context.bot.send_message(
                chat_id=int(wd["user_id"]),
                text=(
                    f"✅ <b>Withdraw Approved (#{wd['id']})</b>\n\n"
                    f"Method: <b>{method_label(wd['method'])}</b>\n"
                    f"Account: <code>{wd['account']}</code>\n"
                    f"Paid Amount: <b>{wd['net_amount']:.2f} BDT</b>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Notify user error: {e}")

        await send_payout_log(context, wd, f"Approved by Admin {user.id}")
        await show_pending_withdraw(update, context, int(idx_s))
        return

    if data.startswith("adm_proof:"):
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        await query.answer()
        _, wid, idx_s = data.split(":")
        context.user_data["admin_action"] = {
            "type": "proof_photo",
            "wid": wid,
            "idx": int(idx_s),
        }
        await query.edit_message_text(
            f"📸 Withdraw <b>#{wid}</b>-এর পেমেন্ট স্ক্রিনশট (Photo) পাঠান:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_pending:{idx_s}")]]
            ),
        )
        return

    if data.startswith("adm_rej:"):
        if not is_admin(user.id):
            await query.answer("⛔ Admin only", show_alert=True)
            return
        _, wid, idx_s = data.split(":")
        wd = DB["withdraws"].get(wid)
        if not wd or wd["status"] != "pending":
            await query.answer("Already processed or not found", show_alert=True)
            await show_pending_withdraw(update, context, int(idx_s))
            return

        wd["status"] = "rejected"
        wd["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uid_str = wd["user_id"]
        if uid_str in DB["users"]:
            DB["users"][uid_str]["balance"] = round(
                DB["users"][uid_str]["balance"] + float(wd["amount"]), 2
            )
        save_data(DB)
        await query.answer("❌ Rejected & Refunded!")

        try:
            await context.bot.send_message(
                chat_id=int(uid_str),
                text=(
                    f"❌ <b>Withdraw Rejected (#{wd['id']})</b>\n\n"
                    f"Amount: <b>{wd['amount']:.2f} BDT</b> আপনার ব্যালেন্সে রিফান্ড করা হয়েছে।"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Refund notify error: {e}")

        await send_payout_log(context, wd, f"Rejected & Refunded by Admin {user.id}")
        await show_pending_withdraw(update, context, int(idx_s))
        return


# ==================== টেক্সট ও ফটো হ্যান্ডলার ====================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()

    # 1. অ্যাডমিন কনফিগ সেটিংস ইনপুট
    adm_act = context.user_data.get("admin_action")
    if adm_act and is_admin(user.id) and adm_act.get("type") == "set_config":
        method = adm_act["method"]
        field = adm_act["field"]
        parts = text.split()
        try:
            if field == "limits" and len(parts) == 2:
                min_v, max_v = float(parts[0]), float(parts[1])
                if min_v < 0 or max_v < min_v:
                    raise ValueError
                DB["settings"][method]["min"] = min_v
                DB["settings"][method]["max"] = max_v
            elif field == "fee" and len(parts) == 2:
                f_type = parts[0].lower()
                f_val = float(parts[1])
                if f_type not in ("fixed", "percent") or f_val < 0:
                    raise ValueError
                DB["settings"][method]["fee_type"] = f_type
                DB["settings"][method]["fee"] = f_val
            else:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ ভুল ফরম্যাট! আবার সঠিক মান লিখে পাঠান।")
            return

        save_data(DB)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"✅ <b>{method_label(method)}</b> সেটিংস আপডেট হয়েছে!",
            parse_mode="HTML",
            reply_markup=admin_settings_kb(),
        )
        return

    # 2. ইউজার উইথড্র ফ্লো
    wd_flow = context.user_data.get("wd_flow")
    if wd_flow:
        method = wd_flow["method"]
        cfg = DB["settings"][method]
        u_data = ensure_user(user.id, user.username or "", user.full_name or "")

        if wd_flow["step"] == "account":
            if len(text) < 6:
                await update.message.reply_text("❌ সঠিক অ্যাকাউন্ট নম্বর দিন।")
                return
            wd_flow["account"] = text
            wd_flow["step"] = "amount"
            await update.message.reply_text(
                f"💵 কত টাকা উইথড্র করতে চান?\n"
                f"• Balance: <b>{u_data['balance']:.2f} BDT</b>\n"
                f"• Min: <b>{cfg['min']:.2f} BDT</b> | Max: <b>{cfg['max']:.2f} BDT</b>",
                parse_mode="HTML",
            )
            return

        if wd_flow["step"] == "amount":
            try:
                amount = round(float(text), 2)
            except ValueError:
                await update.message.reply_text(
                    "❌ বৈধ সংখ্যায় অ্যামাউন্ট লিখুন (যেমন: <code>500</code>)।",
                    parse_mode="HTML",
                )
                return

            if amount < cfg["min"] or amount > cfg["max"]:
                await update.message.reply_text(
                    f"❌ অ্যামাউন্ট অবশ্যই <b>{cfg['min']:.2f}</b> থেকে <b>{cfg['max']:.2f} BDT</b>-এর মধ্যে হতে হবে।",
                    parse_mode="HTML",
                )
                return

            if amount > u_data["balance"]:
                await update.message.reply_text(
                    f"❌ পর্যাপ্ত ব্যালেন্স নেই! আপনার ব্যালেন্স: <b>{u_data['balance']:.2f} BDT</b>।",
                    parse_mode="HTML",
                )
                return

            fee = calc_fee(method, amount)
            net_amount = round(max(0.0, amount - fee), 2)
            if net_amount <= 0:
                await update.message.reply_text("❌ চার্জ বাদ দেওয়ার পর নেট অ্যামাউন্ট শূন্য বা কম হচ্ছে।")
                return

            # ব্যালেন্স কেটে Pending রিকোয়েস্ট তৈরি
            u_data["balance"] = round(u_data["balance"] - amount, 2)
            wid = DB["next_wid"]
            DB["next_wid"] += 1

            wd_record = {
                "id": wid,
                "user_id": str(user.id),
                "username": f"@{user.username}" if user.username else "",
                "name": user.full_name or "",
                "method": method,
                "account": wd_flow["account"],
                "amount": amount,
                "fee": fee,
                "net_amount": net_amount,
                "status": "pending",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            DB["withdraws"][str(wid)] = wd_record
            save_data(DB)
            context.user_data.pop("wd_flow", None)

            # ইউজারকে রিসিপ্ট পাঠানো
            receipt = (
                f"🧾 <b>Withdraw Request Submitted (#{wid})</b>\n\n"
                f"Status: ⏳ <b>Pending</b>\n"
                f"Method: <b>{method_label(method)}</b>\n"
                f"Account: <code>{wd_record['account']}</code>\n"
                f"Amount: <b>{amount:.2f} BDT</b>\n"
                f"Fee: <b>{fee:.2f} BDT</b>\n"
                f"Net Payable: <b>{net_amount:.2f} BDT</b>\n"
                f"Remaining Balance: <b>{u_data['balance']:.2f} BDT</b>"
            )
            await update.message.reply_text(
                receipt, parse_mode="HTML", reply_markup=main_menu_kb(user.id)
            )
            await send_payout_log(context, wd_record)
            return

    await update.message.reply_text(
        "মেনু দেখতে /start অথবা অ্যাডমিন প্যানেলের জন্য /admin চাপুন।"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    adm_act = context.user_data.get("admin_action")
    if not (adm_act and is_admin(user.id) and adm_act.get("type") == "proof_photo"):
        return

    wid = adm_act["wid"]
    wd = DB["withdraws"].get(wid)
    context.user_data.pop("admin_action", None)

    if not wd or wd["status"] != "pending":
        await update.message.reply_text("❌ এই রিকোয়েস্টটি আর Pending অবস্থায় নেই।")
        return

    photo_id = update.message.photo[-1].file_id
    wd["status"] = "approved"
    wd["proof_photo_id"] = photo_id
    wd["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data(DB)

    caption_user = (
        f"✅ <b>Withdraw Paid & Approved (#{wd['id']})</b>\n\n"
        f"Method: <b>{method_label(wd['method'])}</b>\n"
        f"Account: <code>{wd['account']}</code>\n"
        f"Paid Amount: <b>{wd['net_amount']:.2f} BDT</b>"
    )
    try:
        await context.bot.send_photo(
            chat_id=int(wd["user_id"]),
            photo=photo_id,
            caption=caption_user,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Send proof photo error: {e}")

    if PAYOUT_LOG_CHANNEL:
        try:
            await context.bot.send_photo(
                chat_id=PAYOUT_LOG_CHANNEL,
                photo=photo_id,
                caption=(
                    f"📸 <b>Paid Proof (#{wd['id']})</b>\n"
                    f"User: <code>{wd['user_id']}</code> | Method: <b>{method_label(wd['method'])}</b>\n"
                    f"Account: <code>{wd['account']}</code> | Paid: <b>{wd['net_amount']:.2f} BDT</b>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Channel proof error: {e}")

    await update.message.reply_text(
        f"✅ Withdraw <b>#{wid}</b> স্ক্রিনশটসহ Approve করা হয়েছে!",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN environment variable সেট করুন।")

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("addbal", addbal_cmd))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()