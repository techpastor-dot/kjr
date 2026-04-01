import asyncio
import html
import logging
import re
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import ChatMigrated, NetworkError, TimedOut, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import BOT_TOKEN, ADMIN_GROUP_ID, PAYMENT_PER_EMAIL, ADMIN_USER_IDS, WORKER_USER_IDS

log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler = logging.FileHandler("bot.log", encoding="utf-8")
file_handler.setFormatter(log_formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])
logger = logging.getLogger(__name__)

async def safe_reply_text(message, *args, retries=2, delay=1.5, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return await message.reply_text(*args, **kwargs)
        except (NetworkError, TimedOut) as exc:
            logger.warning("safe_reply_text attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
        except TelegramError:
            logger.exception("safe_reply_text failed")
            raise

def html_escape(text: str) -> str:
    return html.escape(str(text), quote=False)

ADMIN_GROUP_CHAT_ID = ADMIN_GROUP_ID

async def safe_send_message(bot, *args, retries=2, delay=1.5, **kwargs):
    global ADMIN_GROUP_CHAT_ID
    for attempt in range(1, retries + 1):
        try:
            return await bot.send_message(*args, **kwargs)
        except ChatMigrated as exc:
            migrate_to_chat_id = exc.migrate_to_chat_id
            logger.warning(
                "Chat migrated from %s to %s. Updating admin group chat id.",
                kwargs.get("chat_id"),
                migrate_to_chat_id,
            )
            ADMIN_GROUP_CHAT_ID = migrate_to_chat_id
            kwargs["chat_id"] = migrate_to_chat_id
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
        except (NetworkError, TimedOut) as exc:
            logger.warning("safe_send_message attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
        except TelegramError:
            logger.exception("safe_send_message failed")
            raise


async def safe_edit_message_text(query, *args, retries=2, delay=1.5, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return await query.edit_message_text(*args, **kwargs)
        except (NetworkError, TimedOut) as exc:
            logger.warning("safe_edit_message_text attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
        except TelegramError:
            logger.exception("safe_edit_message_text failed")
            raise

ASK_NAME, ASK_ACCOUNT = range(2)
JKR_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]*jkr[a-zA-Z0-9._%+\-]*@gmail\.com$", re.IGNORECASE)


def get_db():
    conn = sqlite3.connect("jkr_bot.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                bank_name TEXT,
                account_no TEXT,
                role TEXT DEFAULT 'customer',
                registered_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                email TEXT,
                status TEXT DEFAULT 'pending',
                rejection_reason TEXT,
                assigned_worker_id INTEGER,
                assigned_worker_username TEXT,
                group_msg_id INTEGER,
                submitted_at TEXT DEFAULT (datetime('now')),
                processed_at TEXT,
                paid INTEGER DEFAULT 0,
                paid_at TEXT
            )
        """)
        apply_db_migrations(conn)
        conn.commit()


def apply_db_migrations(conn):
    existing_user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in existing_user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'customer'")

    existing_submission_columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}
    if "assigned_worker_id" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN assigned_worker_id INTEGER")
    if "assigned_worker_username" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN assigned_worker_username TEXT")
    if "processed_at" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN processed_at TEXT")
    if "paid_at" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN paid_at TEXT")


def is_registered(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def get_user(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("/start from user %s (%s)", user.id, user.username)
    user_id = user.id
    if is_registered(user_id):
        await safe_reply_text(
            update.message,
            "👋 Welcome back! Send me a Gmail with *jkr* in it to submit.\n\nExample: `andysalmonjkr@gmail.com`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    await safe_reply_text(
        update.message,
        "👋 Welcome to the *JKR Mail Submission Bot*!\n\nBefore you start, I need your payment details.\n\nPlease enter your *full name* as it appears on your bank account:",
        parse_mode="Markdown",
    )
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info("ask_name from user %s: %s", update.effective_user.id, text)
    context.user_data["full_name"] = text
    await safe_reply_text(
        update.message,
        "Got it! Now send your *bank name and account number* in this format:\n\n`GTBank 0123456789`",
        parse_mode="Markdown",
    )
    return ASK_ACCOUNT


async def ask_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info("ask_account from user %s: %s", update.effective_user.id, text)
    parts = text.rsplit(" ", 1)
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) < 8:
        await safe_reply_text(
            update.message,
            "❌ Format not recognized. Please send it like:\n`GTBank 0123456789`",
            parse_mode="Markdown",
        )
        return ASK_ACCOUNT
    bank_name, account_no = parts
    user = update.effective_user
    full_name = context.user_data.get("full_name")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, username, full_name, bank_name, account_no, role) VALUES (?, ?, ?, ?, ?, COALESCE((SELECT role FROM users WHERE user_id=?), 'customer'))",
            (user.id, user.username, full_name, bank_name.strip(), account_no.strip(), user.id),
        )
        conn.commit()
    if context.user_data.get("update_profile"):
        context.user_data.pop("update_profile", None)
        await safe_reply_text(
            update.message,
            f"✅ Profile updated successfully!\n\nName: {html_escape(full_name)}\nBank: {html_escape(bank_name.strip())}\nAccount: {html_escape(account_no.strip())}",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    await safe_reply_text(
        update.message,
        f"✅ *Registered successfully!*\n\nName: {context.user_data['full_name']}\nBank: {bank_name.strip()}\nAccount: {account_no.strip()}\n\nYou'll receive ₦{PAYMENT_PER_EMAIL:,} per approved email. Payments go out daily at 6PM.\n\nNow send me a Gmail with *jkr* in it to get started!",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("update_profile", None)
    context.user_data.pop("profile_edit", None)
    context.user_data.pop("full_name", None)
    await safe_reply_text(update.message, "Registration cancelled. Send /start to try again.")
    return ConversationHandler.END


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await safe_reply_text(update.message, "You are not registered yet. Send /start first.")
        return

    username_display = f"@{user['username']}" if user['username'] else f"ID:{user_id}"
    text = (
        f"📋 <b>Your Profile</b>\n\n"
        f"Name: {html_escape(user['full_name'] or 'N/A')}\n"
        f"Bank: {html_escape(user['bank_name'] or 'N/A')}\n"
        f"Account: {html_escape(user['account_no'] or 'N/A')}\n"
        f"Username: {html_escape(username_display)}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Edit", callback_data="editprofile")]
    ])
    await safe_reply_text(
        update.message,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def profile_update_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    if not context.user_data.get("profile_edit"): 
        return

    if context.user_data.get("profile_edit") == "name":
        context.user_data["full_name"] = update.message.text.strip()
        context.user_data["profile_edit"] = "account"
        await safe_reply_text(
            update.message,
            "Got it! Now send your updated bank name and account number in this format:\n`GTBank 0123456789`",
            parse_mode="Markdown",
        )
        return

    if context.user_data.get("profile_edit") == "account":
        text = update.message.text.strip()
        parts = text.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) < 8:
            await safe_reply_text(
                update.message,
                "❌ Format not recognized. Please send it like:\n`GTBank 0123456789`",
                parse_mode="Markdown",
            )
            return

        bank_name, account_no = parts
        user = update.effective_user
        full_name = context.user_data.get("full_name")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, username, full_name, bank_name, account_no, role) VALUES (?, ?, ?, ?, ?, COALESCE((SELECT role FROM users WHERE user_id=?), 'customer'))",
                (user.id, user.username, full_name, bank_name.strip(), account_no.strip(), user.id),
            )
            conn.commit()

        context.user_data.pop("profile_edit", None)
        context.user_data.pop("full_name", None)
        await safe_reply_text(
            update.message,
            f"✅ Profile updated successfully!\n\nName: {html_escape(full_name)}\nBank: {html_escape(bank_name.strip())}\nAccount: {html_escape(account_no.strip())}",
            parse_mode="HTML",
        )
        return


EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def parse_email_batch(text: str):
    raw_tokens = re.split(r"[\s,;]+", text)
    emails = []
    invalid = []
    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue
        if EMAIL_PATTERN.fullmatch(token):
            emails.append(token.lower())
        else:
            invalid.append(token)
    return emails, invalid


worker_reminder_tasks = {}


def cancel_worker_reminder(submission_id: int):
    task = worker_reminder_tasks.pop(submission_id, None)
    if task is not None:
        task.cancel()


async def worker_reminder_loop(bot, submission_id: int, worker_id: int, worker_username: str):
    try:
        await asyncio.sleep(180)
        with get_db() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not sub or sub["status"] != "claimed" or sub["assigned_worker_id"] != worker_id:
            return

        await safe_send_message(
            bot,
            chat_id=worker_id,
            text=(
                f"⏳ Reminder: You claimed mail #{submission_id}, but no action has been taken yet.\n"
                "Please complete this task by selecting Done, Incorrect Password, or Not Found."
            ),
            parse_mode="HTML",
        )
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Worker reminder failed for submission %s", submission_id)
    finally:
        worker_reminder_tasks.pop(submission_id, None)


def schedule_worker_reminder(bot, submission_id: int, worker_id: int, worker_username: str):
    cancel_worker_reminder(submission_id)
    task = asyncio.create_task(worker_reminder_loop(bot, submission_id, worker_id, worker_username))
    worker_reminder_tasks[submission_id] = task


def is_worker(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["role"] == "worker":
        return True
    return False


def set_worker_role_by_username(username: str) -> bool:
    normalized = username.lstrip("@").lower()
    with get_db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE lower(username)=?", (normalized,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE users SET role='worker' WHERE user_id=?", (row["user_id"],))
        conn.commit()
        return True


def remove_worker_role_by_username(username: str) -> bool:
    normalized = username.lstrip("@").lower()
    with get_db() as conn:
        row = conn.execute("SELECT user_id, role FROM users WHERE lower(username)=?", (normalized,)).fetchone()
        if not row or row["role"] != "worker":
            return False
        conn.execute("UPDATE users SET role='customer' WHERE user_id=?", (row["user_id"],))
        conn.commit()
        return True


async def safe_edit_chat_message(bot, *args, retries=2, delay=1.5, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return await bot.edit_message_text(*args, **kwargs)
        except ChatMigrated as exc:
            migrate_to_chat_id = exc.migrate_to_chat_id
            logger.warning(
                "Chat migrated from %s to %s while editing. Updating admin group chat id.",
                kwargs.get("chat_id"),
                migrate_to_chat_id,
            )
            kwargs["chat_id"] = migrate_to_chat_id
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
        except (NetworkError, TimedOut) as exc:
            logger.warning("safe_edit_chat_message attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            await asyncio.sleep(delay)
        except TelegramError:
            logger.exception("safe_edit_chat_message failed")
            raise


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("profile_edit"):
        return
    user = update.effective_user
    text = update.message.text.strip()
    logger.info("handle_email from user %s (%s): %s", user.id, user.username, text)
    if not is_registered(user.id):
        await safe_reply_text(update.message, "Please send /start first to register.")
        return

    emails, invalid_tokens = parse_email_batch(text)
    valid_emails = []
    invalid_emails = []
    for email in emails:
        if not email.endswith("@gmail.com") or "jkr" not in email:
            invalid_emails.append(email)
            continue
        valid_emails.append(email)

    unique_emails = []
    seen_emails = set()
    for email in valid_emails:
        if email in seen_emails:
            continue
        seen_emails.add(email)
        unique_emails.append(email)

    if len(unique_emails) == 0:
        error_text = "❌ Invalid email. The email must:\n• Be a Gmail address\n• Contain *jkr* somewhere in it\n\nExample: `andysalmonjkr@gmail.com`"
        if invalid_tokens or invalid_emails:
            error_text += "\n\nInvalid values: " + ", ".join(invalid_tokens + invalid_emails)
        await safe_reply_text(update.message, error_text, parse_mode="Markdown")
        return

    if len(unique_emails) > 20:
        await safe_reply_text(
            update.message,
            "❌ You can submit between 1 and 20 emails at once. Please send a smaller batch.",
            parse_mode="Markdown",
        )
        return

    with get_db() as conn:
        placeholders = ",".join(["?" for _ in unique_emails])
        existing_rows = conn.execute(
            f"SELECT email FROM submissions WHERE email IN ({placeholders}) AND status IN ('pending','claimed','approved')",
            tuple(unique_emails),
        ).fetchall()
    existing_emails = {row[0] for row in existing_rows}

    skipped_emails = []
    accepted_emails = []
    for email in unique_emails:
        if email in existing_emails:
            skipped_emails.append(email)
            continue
        accepted_emails.append(email)

    if len(accepted_emails) == 0:
        msg = "⚠️ None of the submitted emails could be accepted. They were already submitted or approved."
        if skipped_emails:
            msg += "\nAlready submitted: " + ", ".join(skipped_emails)
        await safe_reply_text(update.message, msg, parse_mode="Markdown")
        return

    db_user = get_user(user.id)
    username_display = f"@{user.username}" if user.username else f"ID:{user.id}"
    any_group_delivery_failed = False
    any_admin_fallback_sent = False
    with get_db() as conn:
        base_count = conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=?", (user.id,)).fetchone()[0]
        for email in accepted_emails:
            cursor = conn.execute(
                "INSERT INTO submissions (user_id, email, status) VALUES (?,?,?)",
                (user.id, email, "pending"),
            )
            submission_id = cursor.lastrowid
            base_count += 1
            total_submissions = base_count
            conn.commit()

            msg = None
            group_delivery_failed = False
            try:
                msg = await safe_send_message(
                    context.bot,
                    chat_id=ADMIN_GROUP_CHAT_ID,
                    text=(
                        f"📧 <b>New Email Submission</b>\n\n"
                        f"From: {html_escape(db_user['full_name'])} ({html_escape(username_display)})\n"
                        f"Bank: {html_escape(db_user['bank_name'])} — {html_escape(db_user['account_no'])}\n"
                        f"Email: <code>{html_escape(email)}</code>\n"
                        f"Status: <b>Unclaimed</b>\n"
                        f"Total submissions by this user: {total_submissions}\n"
                        f"Submission ID: #{submission_id}"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                group_delivery_failed = True
                any_group_delivery_failed = True
                logger.exception(
                    "Failed to deliver submission %s to admin group chat %s.",
                    email,
                    ADMIN_GROUP_CHAT_ID,
                )
                if ADMIN_USER_IDS:
                    for admin_id in ADMIN_USER_IDS:
                        try:
                            await safe_send_message(
                                context.bot,
                                chat_id=admin_id,
                                text=(
                                    f"📧 <b>New Email Submission (Admin DM Fallback)</b>\n\n"
                                    f"From: {html_escape(db_user['full_name'])} ({html_escape(username_display)})\n"
                                    f"Bank: {html_escape(db_user['bank_name'])} — {html_escape(db_user['account_no'])}\n"
                                    f"Email: <code>{html_escape(email)}</code>\n"
                                    f"Status: <b>Unclaimed</b>\n"
                                    f"Submission ID: #{submission_id}"
                                ),
                                parse_mode="HTML",
                            )
                            any_admin_fallback_sent = True
                        except Exception:
                            logger.exception(
                                "Failed to deliver fallback admin DM for submission %s to user %s.",
                                email,
                                admin_id,
                            )

            if msg:
                conn.execute("UPDATE submissions SET group_msg_id=? WHERE id=?", (msg.message_id, submission_id))
                conn.commit()
            elif group_delivery_failed:
                conn.execute("UPDATE submissions SET group_msg_id=NULL WHERE id=?", (submission_id,))
                conn.commit()

    user_message = [f"✅ Submitted {len(accepted_emails)} email(s) for review."]
    if skipped_emails:
        user_message.append("⚠️ These were already submitted: " + ", ".join(skipped_emails))
    if invalid_tokens or invalid_emails:
        user_message.append("❌ Invalid values: " + ", ".join(invalid_tokens + invalid_emails))
    if any_group_delivery_failed:
        if any_admin_fallback_sent:
            user_message.append("⚠️ Admin group notification failed, but admins were notified directly.")
        else:
            user_message.append(
                "⚠️ Submission received, but admin group notification failed. "
                "Please ask the owner to check the group configuration."
            )
    await safe_reply_text(update.message, "\n\n".join(user_message), parse_mode="Markdown")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    parts = data.split("|", 2)
    action = parts[0]
    user_id = query.from_user.id

    if action in {"approve", "reject", "markpaid"}:
        if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
            await query.answer("You are not authorized to perform this action.", show_alert=True)
            return
    elif action in {"claim", "process"}:
        if not is_worker(user_id):
            await query.answer("You are not authorized to process this mail.", show_alert=True)
            return
    elif action == "viewdetails":
        target_user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if target_user_id is None:
            await query.answer("Invalid view details request.", show_alert=True)
            return
        if query.from_user.id != target_user_id and (ADMIN_USER_IDS and query.from_user.id not in ADMIN_USER_IDS):
            await query.answer("You are not authorized to view these details.", show_alert=True)
            return
    elif action == "editprofile":
        if not is_registered(user_id):
            await query.answer("Register first with /start.", show_alert=True)
            return
    else:
        await query.answer("Unknown action.", show_alert=True)
        return

    logger.info("handle_callback from user %s: %s", user_id, data)
    await query.answer()

    if action == "claim":
        sub_id = int(parts[1]) if len(parts) > 1 else None
        with get_db() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        if not sub:
            await safe_edit_message_text(query, "⚠️ Submission not found.")
            return

        if sub["status"] != "pending" or sub["assigned_worker_id"] is not None:
            await safe_edit_message_text(query, "⚠️ This mail has already been claimed or processed.")
            return

        worker_username = query.from_user.username or str(query.from_user.id)
        with get_db() as conn:
            conn.execute(
                "UPDATE submissions SET status='claimed', assigned_worker_id=?, assigned_worker_username=? WHERE id=?",
                (user_id, worker_username, sub_id),
            )
            conn.commit()
        db_user = get_user(sub["user_id"])

        group_text = (
            f"📧 <b>New Email Submission</b>\n\n"
            f"From: {html_escape(db_user['full_name'])} ({html_escape(db_user['username'] or f'ID:{db_user['user_id']}')})\n"
            f"Bank: {html_escape(db_user['bank_name'])} — {html_escape(db_user['account_no'])}\n"
            f"Email: <code>{html_escape(sub['email'])}</code>\n"
            f"Status: <b>Claimed by @{html_escape(worker_username)}</b>\n"
            f"Submission ID: #{sub_id}"
        )
        if sub["group_msg_id"]:
            await safe_edit_chat_message(
                context.bot,
                chat_id=ADMIN_GROUP_CHAT_ID,
                message_id=sub["group_msg_id"],
                text=group_text,
                parse_mode="HTML",
            )

        action_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Done", callback_data=f"process|{sub_id}|done"),
                InlineKeyboardButton("❌ Incorrect Password", callback_data=f"process|{sub_id}|incorrect_password"),
            ],
            [
                InlineKeyboardButton("❌ Not Found", callback_data=f"process|{sub_id}|not_found"),
            ],
        ])

        await safe_send_message(
            context.bot,
            chat_id=user_id,
            text=(
                f"🛠️ <b>Mail Claimed</b>\n\n"
                f"Email: <code>{html_escape(sub['email'])}</code>\n"
                f"Submission ID: #{sub_id}\n"
                f"Please choose the correct result for this mail."
            ),
            parse_mode="HTML",
            reply_markup=action_keyboard,
        )
        schedule_worker_reminder(context.bot, sub_id, user_id, worker_username)
        return

    if action == "editprofile":
        context.user_data["profile_edit"] = "name"
        await safe_send_message(
            context.bot,
            chat_id=user_id,
            text="🔄 Send your updated full name as it appears on your bank account.",
            parse_mode="HTML",
        )
        await safe_edit_message_text(
            query,
            "✏️ Profile edit started. Please check your private chat.",
            parse_mode="HTML",
        )
        return

    if action == "process":
        sub_id = int(parts[1]) if len(parts) > 1 else None
        result = parts[2] if len(parts) > 2 else None
        with get_db() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        if not sub:
            await safe_edit_message_text(query, "⚠️ Submission not found.")
            return
        if sub["status"] != "claimed" or sub["assigned_worker_id"] != user_id:
            await safe_edit_message_text(query, "⚠️ You can only process mails you have claimed.")
            return
        if result not in {"done", "incorrect_password", "not_found"}:
            await safe_edit_message_text(query, "⚠️ Invalid action.")
            return

        new_status = {
            "done": "approved",
            "incorrect_password": "incorrect_password",
            "not_found": "not_found",
        }[result]
        status_text = {
            "done": "✅ Done",
            "incorrect_password": "❌ Incorrect Password",
            "not_found": "❌ Not Found",
        }[result]

        cancel_worker_reminder(sub_id)
        with get_db() as conn:
            conn.execute(
                "UPDATE submissions SET status=?, processed_at=datetime('now') WHERE id=?",
                (new_status, sub_id),
            )
            conn.commit()
        db_user = get_user(sub["user_id"])

        group_text = (
            f"📧 <b>New Email Submission</b>\n\n"
            f"From: {html_escape(db_user['full_name'])} ({html_escape(db_user['username'] or f'ID:{db_user['user_id']}')})\n"
            f"Bank: {html_escape(db_user['bank_name'])} — {html_escape(db_user['account_no'])}\n"
            f"Email: <code>{html_escape(sub['email'])}</code>\n"
            f"Status: <b>{status_text} by @{html_escape(query.from_user.username or str(user_id))}</b>\n"
            f"Submission ID: #{sub_id}"
        )
        if sub["group_msg_id"]:
            await safe_edit_chat_message(
                context.bot,
                chat_id=ADMIN_GROUP_CHAT_ID,
                message_id=sub["group_msg_id"],
                text=group_text,
                parse_mode="HTML",
            )

        customer_text = {
            "approved": (
                f"✅ <b>Mail Processed</b>\n\n"
                f"<code>{html_escape(sub['email'])}</code> has been approved and will be paid.\n"
                f"₦{PAYMENT_PER_EMAIL:,} will be added to your payout at 6PM today."
            ),
            "incorrect_password": (
                f"❌ <b>Mail Not Approved</b>\n\n"
                f"<code>{html_escape(sub['email'])}</code> was marked as Incorrect Password by the worker.\n"
                f"Please verify and resubmit if possible."
            ),
            "not_found": (
                f"❌ <b>Mail Not Approved</b>\n\n"
                f"<code>{html_escape(sub['email'])}</code> was marked as Not Found by the worker.\n"
                f"Please verify the account and resubmit if needed."
            ),
        }[new_status]

        await safe_send_message(
            context.bot,
            chat_id=sub["user_id"],
            text=customer_text,
            parse_mode="HTML",
        )
        await safe_edit_message_text(
            query,
            f"{status_text} — Submission #{sub_id} has been processed.",
            parse_mode="HTML",
        )
        return

    if action == "markpaid":
        target_user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target_day = parts[2] if len(parts) > 2 else None
        if not target_user_id or not target_day:
            await query.answer("Invalid payment request.", show_alert=True)
            return

        with get_db() as conn:
            approved_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM submissions WHERE user_id=? AND paid=0 AND status='approved' AND DATE(submitted_at)=?",
                (target_user_id, target_day),
            ).fetchone()["cnt"]
            if approved_count == 0:
                await safe_edit_message_text(query, "⚠️ No pending approved mails found for this customer/day.")
                return
            amount = approved_count * PAYMENT_PER_EMAIL
            conn.execute(
                "UPDATE submissions SET paid=1, paid_at=datetime('now') WHERE user_id=? AND paid=0 AND status='approved' AND DATE(submitted_at)=?",
                (target_user_id, target_day),
            )
            conn.commit()

        await safe_edit_message_text(
            query,
            f"✅ Marked {approved_count} mail(s) as paid for user {target_user_id} on {target_day}.\nAmount: ₦{amount:,}",
            parse_mode="HTML",
        )

        view_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("View Details", callback_data=f"viewdetails|{target_user_id}|{target_day}")]
        ])
        await safe_send_message(
            context.bot,
            chat_id=target_user_id,
            text=(
                f"💰 Your payment of ₦{amount:,} has been processed successfully ✅\n\n"
                "Tap below to view details 👇"
            ),
            parse_mode="HTML",
            reply_markup=view_keyboard,
        )
        return

    if action == "viewdetails":
        target_user_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target_day = parts[2] if len(parts) > 2 else None
        if not target_user_id or not target_day:
            await query.answer("Invalid details request.", show_alert=True)
            return

        with get_db() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as done_count,
                    SUM(CASE WHEN status='incorrect_password' THEN 1 ELSE 0 END) as incorrect_count,
                    SUM(CASE WHEN status='not_found' THEN 1 ELSE 0 END) as not_found_count
                FROM submissions
                WHERE user_id=? AND DATE(submitted_at)=? AND paid=1
                """,
                (target_user_id, target_day),
            ).fetchone()
        total = row["total"] or 0
        done = row["done_count"] or 0
        incorrect = row["incorrect_count"] or 0
        not_found = row["not_found_count"] or 0
        amount = done * PAYMENT_PER_EMAIL

        await safe_edit_message_text(
            query,
            (
                f"📄 <b>Payment Summary</b>\n\n"
                f"Total Mails Processed: {total}\n"
                f"✅ Successful (Done): {done}\n"
                f"❌ Incorrect Password: {incorrect}\n"
                f"❌ Not Found: {not_found}\n\n"
                f"Rate per Mail: ₦{PAYMENT_PER_EMAIL}\n"
                f"💰 Amount Paid: ₦{amount:,}"
            ),
            parse_mode="HTML",
        )
        return

    if action == "approve":
        sub_id = int(parts[1]) if len(parts) > 1 else None
        with get_db() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        if not sub:
            await safe_edit_message_text(query, "⚠️ Submission not found.")
            return
        db_user = get_user(sub["user_id"])
        with get_db() as conn:
            conn.execute("UPDATE submissions SET status='approved' WHERE id=?", (sub_id,))
            conn.commit()
        await safe_edit_message_text(
            query,
            f"✅ <b>Approved</b> — #{sub_id}\nEmail: <code>{html_escape(sub['email'])}</code>\nUser: {html_escape(db_user['full_name'])}",
            parse_mode="HTML",
        )
        await safe_send_message(
            context.bot,
            chat_id=sub["user_id"],
            text=(
                f"✅ <b>Email Approved!</b>\n\n"
                f"<code>{html_escape(sub['email'])}</code> has been verified.\n"
                f"₦{PAYMENT_PER_EMAIL:,} will be added to your payout at 6PM today."
            ),
            parse_mode="HTML",
        )
    elif action == "reject":
        sub_id = int(parts[1]) if len(parts) > 1 else None
        context.chat_data["awaiting_rejection"] = sub_id
        await safe_edit_message_text(
            query,
            f"⏳ Rejecting #{sub_id}...\nNow type the rejection reason in the group chat."
        )


async def handle_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_IDS and update.effective_user.id not in ADMIN_USER_IDS:
        return
    if "awaiting_rejection" not in context.chat_data:
        return
    sub_id = context.chat_data.pop("awaiting_rejection")
    reason = update.message.text.strip()
    logger.info("handle_rejection_reason from admin %s for submission %s: %s", update.effective_user.id, sub_id, reason)
    with get_db() as conn:
        sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        conn.execute(
            "UPDATE submissions SET status='rejected', rejection_reason=? WHERE id=?", (reason, sub_id)
        )
        conn.commit()
    await safe_reply_text(update.message, f"❌ Submission #{sub_id} rejected.\nReason: {reason}")
    await safe_send_message(
        context.bot,
        chat_id=sub["user_id"],
        text=(
            f"❌ <b>Email Rejected</b>\n\n"
            f"Email: <code>{html_escape(sub['email'])}</code>\n"
            f"Reason: {html_escape(reason)}\n\n"
            f"Please fix and resubmit if possible."
        ),
        parse_mode="HTML",
    )


async def send_payout_summary(bot):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.full_name, u.bank_name, u.account_no, u.user_id, COUNT(s.id) as count
            FROM submissions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.status='approved' AND s.paid=0 AND DATE(s.submitted_at) = ?
            GROUP BY s.user_id
        """, (today,)).fetchall()
    if not rows:
        await safe_send_message(
            bot,
            chat_id=ADMIN_GROUP_CHAT_ID,
            text="📊 <b>Daily Payout Summary</b>\n\nNo approved unpaid submissions today.",
            parse_mode="HTML",
        )
        return
    lines = [f"📊 <b>Daily Payout Summary — {html_escape(today)}</b>\n"]
    total = 0
    for row in rows:
        amount = row["count"] * PAYMENT_PER_EMAIL
        total += amount
        lines.append(
            f"👤 {html_escape(row['full_name'])}\n"
            f"   Bank: {html_escape(row['bank_name'])} — {html_escape(row['account_no'])}\n"
            f"   Emails: {row['count']} × ₦{PAYMENT_PER_EMAIL:,} = <b>₦{amount:,}</b>\n"
        )
    lines.append(f"\n<b>💰 Total to pay: ₦{total:,}</b>")
    await safe_send_message(bot, chat_id=ADMIN_GROUP_CHAT_ID, text="\n".join(lines), parse_mode="HTML")
    with get_db() as conn:
        conn.execute(
            "UPDATE submissions SET paid=1 WHERE status='approved' AND paid=0 AND DATE(submitted_at)=?", (today,)
        )
        conn.commit()


async def handle_request_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    user = update.effective_user
    if not is_worker(user.id):
        await safe_reply_text(update.message, "You are not authorized to request mail.")
        return

    with get_db() as conn:
        submission = conn.execute(
            """
            SELECT s.*, u.full_name, u.bank_name, u.account_no, u.username as submitter_username
            FROM submissions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.status='pending' AND s.assigned_worker_id IS NULL
            ORDER BY s.id
            LIMIT 1
            """
        ).fetchone()

    if not submission:
        await safe_reply_text(update.message, "No unclaimed mails are available right now.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Claim", callback_data=f"claim|{submission['id']}")]
    ])

    await safe_reply_text(
        update.message,
        (
            f"📨 <b>Unclaimed Mail</b>\n\n"
            f"Email: <code>{html_escape(submission['email'])}</code>\n"
            f"Submitter: {html_escape(submission['full_name'])} ({html_escape(submission['submitter_username'] or f'ID:{submission['user_id']}')})\n"
            f"Submission ID: #{submission['id']}\n"
            f"Status: <b>Unclaimed</b>\n\n"
            f"Press Claim to assign it to yourself."
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def worker_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_CHAT_ID:
        return
    user_id = update.effective_user.id
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await safe_reply_text(update.message, "You are not authorized to view worker stats.")
        return

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                assigned_worker_username,
                COUNT(*) as claimed_count,
                SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved_count,
                SUM(CASE WHEN status='incorrect_password' THEN 1 ELSE 0 END) as incorrect_password_count,
                SUM(CASE WHEN status='not_found' THEN 1 ELSE 0 END) as not_found_count
            FROM submissions
            WHERE assigned_worker_id IS NOT NULL
            GROUP BY assigned_worker_id, assigned_worker_username
            ORDER BY claimed_count DESC
            """
        ).fetchall()

    if not rows:
        await safe_reply_text(update.message, "No worker activity has been recorded yet.")
        return

    lines = ["📊 <b>Worker Mail Stats</b>\n"]
    total_claimed = total_approved = total_incorrect = total_not_found = 0
    for row in rows:
        worker_name = row["assigned_worker_username"] or "Unknown"
        claimed = row["claimed_count"] or 0
        approved = row["approved_count"] or 0
        incorrect = row["incorrect_password_count"] or 0
        not_found = row["not_found_count"] or 0
        total_claimed += claimed
        total_approved += approved
        total_incorrect += incorrect
        total_not_found += not_found
        lines.append(
            f"👤 @{html_escape(worker_name)}\n"
            f"   Claimed: {claimed}\n"
            f"   Approved: {approved}\n"
            f"   Incorrect Password: {incorrect}\n"
            f"   Not Found: {not_found}\n"
        )

    lines.append(
        f"\n<b>Total Claimed:</b> {total_claimed}\n"
        f"<b>Total Approved:</b> {total_approved}\n"
        f"<b>Total Incorrect Password:</b> {total_incorrect}\n"
        f"<b>Total Not Found:</b> {total_not_found}"
    )

    await safe_reply_text(update.message, "\n".join(lines), parse_mode="HTML")


async def customer_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_CHAT_ID:
        return
    user_id = update.effective_user.id
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await safe_reply_text(update.message, "You are not authorized to view customer stats.")
        return

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.bank_name,
                u.account_no,
                DATE(s.submitted_at) as day,
                SUM(CASE WHEN s.status='approved' THEN 1 ELSE 0 END) as done_count,
                SUM(CASE WHEN s.status='incorrect_password' THEN 1 ELSE 0 END) as incorrect_count,
                SUM(CASE WHEN s.status='not_found' THEN 1 ELSE 0 END) as not_found_count,
                SUM(CASE WHEN s.status='approved' THEN 1 ELSE 0 END) * ? as pending_amount
            FROM submissions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.paid=0 AND s.status IN ('approved','incorrect_password','not_found')
            GROUP BY u.user_id, day
            ORDER BY day DESC, u.user_id
            """,
            (PAYMENT_PER_EMAIL,),
        ).fetchall()

    if not rows:
        await safe_reply_text(update.message, "No pending customer payments found.")
        return

    await safe_reply_text(update.message, "📊 <b>Customer Payment Stats</b>", parse_mode="HTML")

    for row in rows:
        username_display = f"@{row['username']}" if row['username'] else f"ID:{row['user_id']}"
        done = row['done_count'] or 0
        incorrect = row['incorrect_count'] or 0
        not_found = row['not_found_count'] or 0
        pending_amount = row['pending_amount'] or 0
        text = (
            f"Customer: {html_escape(username_display)}\n"
            f"Bank: {html_escape(row['bank_name'] or 'N/A')}\n"
            f"Account: {html_escape(row['account_no'] or 'N/A')}\n"
            f"Day: {html_escape(row['day'])}\n"
            f"✅ Done: {done}\n"
            f"❌ Incorrect Password: {incorrect}\n"
            f"❌ Not Found: {not_found}\n"
            f"💰 Pending Payment: ₦{pending_amount:,}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Mark as Paid", callback_data=f"markpaid|{row['user_id']}|{row['day']}")]
        ])
        await safe_send_message(
            context.bot,
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        return
    user_id = update.effective_user.id
    if not is_registered(user_id):
        await safe_reply_text(update.message, "Send /start to register first.")
        return

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status != 'pending' THEN 1 ELSE 0 END) as processed_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_count,
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved_count,
                SUM(CASE WHEN status = 'approved' AND paid = 0 THEN 1 ELSE 0 END) as unpaid_count
            FROM submissions
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    processed_count = row['processed_count'] or 0
    pending_count = row['pending_count'] or 0
    approved_count = row['approved_count'] or 0
    unpaid_count = row['unpaid_count'] or 0
    total_balance = approved_count * PAYMENT_PER_EMAIL
    pending_payout = unpaid_count * PAYMENT_PER_EMAIL

    await safe_reply_text(
        update.message,
        (
            f"📊 *Your Balance*\n\n"
            f"✅ Total mails processed: {processed_count}\n"
            f"⏳ Pending mails: {pending_count}\n"
            f"💰 Total balance from approved mails: ₦{total_balance:,}\n"
            f"💳 Pending payout: ₦{pending_payout:,}\n"
            f"🧾 Approved mails: {approved_count}\n"
            f"⛔ Unpaid approved mails: {unpaid_count}"
        ),
        parse_mode="Markdown",
    )


async def add_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_CHAT_ID:
        return
    user_id = update.effective_user.id
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await safe_reply_text(update.message, "You are not authorized to add workers.")
        return

    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply_text(update.message, "Usage: /add-worker @username")
        return

    target = parts[1].strip().split()[0]
    if target.startswith("@"): 
        target_username = target[1:]
    else:
        target_username = target

    if not target_username:
        await safe_reply_text(update.message, "Please provide a valid username. Example: /add-worker @username")
        return

    success = set_worker_role_by_username(target_username)
    if not success:
        await safe_reply_text(update.message, "User not found or not registered. Make sure the target user has interacted with the bot.")
        return

    await safe_reply_text(update.message, f"✅ @{html_escape(target_username)} has been added as a worker.")


async def delete_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_CHAT_ID:
        return
    user_id = update.effective_user.id
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await safe_reply_text(update.message, "You are not authorized to remove workers.")
        return

    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply_text(update.message, "Usage: /delete-worker @username")
        return

    target = parts[1].strip().split()[0]
    if target.startswith("@"):
        target_username = target[1:]
    else:
        target_username = target

    if not target_username:
        await safe_reply_text(update.message, "Please provide a valid username. Example: /delete-worker @username")
        return

    success = remove_worker_role_by_username(target_username)
    if not success:
        await safe_reply_text(update.message, "User not found, not registered, or not currently a worker.")
        return

    await safe_reply_text(update.message, f"✅ @{html_escape(target_username)} has been removed as a worker.")


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_registered(user_id):
        await safe_reply_text(update.message, "Send /start to register first.")
        return
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as approved,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as rejected,
                SUM(CASE WHEN status='approved' AND paid=0 THEN 1 ELSE 0 END) as unpaid
            FROM submissions WHERE user_id=?
        """, (user_id,)).fetchone()
    total = (row["approved"] or 0) + (row["pending"] or 0) + (row["rejected"] or 0)
    unpaid_amount = (row["unpaid"] or 0) * PAYMENT_PER_EMAIL
    await safe_reply_text(
        update.message,
        f"📊 *Your Stats*\n\n✅ Approved: {row['approved'] or 0}\n⏳ Pending: {row['pending'] or 0}\n❌ Rejected: {row['rejected'] or 0}\n\n📨 Total submissions: {total}\n\n💰 Pending payout: ₦{unpaid_amount:,}",
        parse_mode="Markdown",
    )


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_account)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stats", my_stats, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("profile", profile, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.Regex(r"^/profile(?:\s|$)") & filters.ChatType.PRIVATE, profile))
    app.add_handler(CommandHandler("worker_stats", worker_stats, filters=filters.Chat(ADMIN_GROUP_CHAT_ID)))
    app.add_handler(MessageHandler(filters.Regex(r"^/worker\-stats(?:\s|$)") & filters.Chat(ADMIN_GROUP_CHAT_ID), worker_stats))
    app.add_handler(CommandHandler("request", handle_request_mail, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.Regex(r"^/request(?:\s|$)") & filters.ChatType.PRIVATE, handle_request_mail))
    app.add_handler(CommandHandler("customer_stats", customer_stats, filters=filters.Chat(ADMIN_GROUP_CHAT_ID)))
    app.add_handler(MessageHandler(filters.Regex(r"^/customer\-stats(?:\s|$)") & filters.Chat(ADMIN_GROUP_CHAT_ID), customer_stats))
    app.add_handler(CommandHandler("add_worker", add_worker, filters=filters.Chat(ADMIN_GROUP_CHAT_ID)))
    app.add_handler(MessageHandler(filters.Regex(r"^/add\-worker(?:\s|$)") & filters.Chat(ADMIN_GROUP_CHAT_ID), add_worker))
    app.add_handler(CommandHandler("delete_worker", delete_worker, filters=filters.Chat(ADMIN_GROUP_CHAT_ID)))
    app.add_handler(MessageHandler(filters.Regex(r"^/delete\-worker(?:\s|$)") & filters.Chat(ADMIN_GROUP_CHAT_ID), delete_worker))
    app.add_handler(CommandHandler("balance", balance, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.Regex(r"^/balance(?:\s|$)") & filters.ChatType.PRIVATE, balance))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_email
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, profile_update_text
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.ChatType.PRIVATE,
        handle_rejection_reason,
    ))

    scheduler = AsyncIOScheduler()

    async def scheduled_summary():
        await send_payout_summary(app.bot)

    scheduler.add_job(scheduled_summary, "cron", hour=18, minute=0)

    async def post_init(application):
        scheduler.start()

    app.post_init = post_init

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
