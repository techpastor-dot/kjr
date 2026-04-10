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
OWNER_REFERRAL_TAG = "techpastor"
OWNER_REFERRAL_RATE = 50

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
                referred_by TEXT,
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS owner_referral_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                mail_id INTEGER NOT NULL UNIQUE,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                paid_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS owner_referral_withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount INTEGER NOT NULL,
                mail_count INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        apply_db_migrations(conn)
        conn.commit()


def apply_db_migrations(conn):
    existing_user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in existing_user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'customer'")
    if "referred_by" not in existing_user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN referred_by TEXT")

    existing_submission_columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}
    if "assigned_worker_id" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN assigned_worker_id INTEGER")
    if "assigned_worker_username" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN assigned_worker_username TEXT")
    if "processed_at" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN processed_at TEXT")
    if "paid_at" not in existing_submission_columns:
        conn.execute("ALTER TABLE submissions ADD COLUMN paid_at TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS owner_referral_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            mail_id INTEGER NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            paid_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS owner_referral_withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount INTEGER NOT NULL,
            mail_count INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_referral_mail_unique ON owner_referral_earnings(mail_id)"
    )


def is_owner_user(user) -> bool:
    return (user.username or "").lower() == OWNER_REFERRAL_TAG


def is_referred_by_owner(user_row) -> bool:
    if not user_row:
        return False
    return (user_row["referred_by"] or "").lower() == OWNER_REFERRAL_TAG


def upsert_user_profile(user, full_name: str, bank_name: str, account_no: str, referred_by: str = None):
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO users
            (user_id, username, full_name, bank_name, account_no, role, referred_by)
            VALUES
            (?, ?, ?, ?, ?, COALESCE((SELECT role FROM users WHERE user_id=?), 'customer'),
             COALESCE((SELECT referred_by FROM users WHERE user_id=?), ?))
            """,
            (
                user.id,
                user.username,
                full_name,
                bank_name.strip(),
                account_no.strip(),
                user.id,
                user.id,
                referred_by,
            ),
        )
        conn.commit()


def maybe_tag_user_as_referred(user_id: int, payload: str):
    if payload != OWNER_REFERRAL_TAG:
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET referred_by=? WHERE user_id=? AND (referred_by IS NULL OR referred_by='')",
            (OWNER_REFERRAL_TAG, user_id),
        )
        conn.commit()


def credit_owner_referral_earning(mail_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT s.user_id, u.referred_by
            FROM submissions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.id=?
            """,
            (mail_id,),
        ).fetchone()
        if not row:
            return False
        if (row["referred_by"] or "").lower() != OWNER_REFERRAL_TAG:
            return False
        conn.execute(
            """
            INSERT OR IGNORE INTO owner_referral_earnings (customer_id, mail_id, amount, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (row["user_id"], mail_id, OWNER_REFERRAL_RATE),
        )
        inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
        conn.commit()
        return inserted


def get_daily_unpaid_customer_rows(target_day: str):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.bank_name,
                u.account_no,
                COUNT(s.id) as approved_count
            FROM submissions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.paid=0 AND s.status='approved' AND DATE(s.submitted_at)=?
            GROUP BY u.user_id
            ORDER BY approved_count DESC
            """,
            (target_day,),
        ).fetchall()


def build_daily_payout_summary(target_day: str, rows):
    if not rows:
        return (
            f"📊 <b>Daily Payout Summary</b>\n\nNo approved unpaid mails found for <b>{html_escape(target_day)}</b>.",
            0,
            0,
            0,
        )
    lines = [
        f"📊 <b>Daily Payout Summary</b>\n\nDate: <b>{html_escape(target_day)}</b>\n",
    ]
    total_customers = len(rows)
    total_count = 0
    total_amount = 0
    for row in rows:
        approved_count = row["approved_count"] or 0
        amount = approved_count * PAYMENT_PER_EMAIL
        total_count += approved_count
        total_amount += amount
        username_display = f"@{row['username']}" if row["username"] else f"ID:{row['user_id']}"
        lines.append(
            f"👤 {html_escape(username_display)} — {approved_count} mail(s) — ₦{amount:,}\n"
            f"   Bank: {html_escape(row['bank_name'] or 'N/A')} — {html_escape(row['account_no'] or 'N/A')}\n"
        )

    lines.append(
        f"\n<b>Total customers:</b> {total_customers}\n"
        f"<b>Total mails:</b> {total_count}\n"
        f"<b>Total payout:</b> ₦{total_amount:,}\n\n"
        "After bank transfer, tap <b>PAID ✅</b> to mark these payouts as paid and notify customers."
    )
    return "\n".join(lines), total_customers, total_count, total_amount


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
    start_payload = ""
    if context.args:
        start_payload = (context.args[0] or "").strip().lower()
        if start_payload == OWNER_REFERRAL_TAG:
            context.user_data["referred_by"] = OWNER_REFERRAL_TAG

    if is_registered(user_id):
        maybe_tag_user_as_referred(user_id, start_payload)
        db_user = get_user(user_id)
        referred_note = (
            "\n\n⭐ Referral tag active: Referred by TechPastor."
            if is_referred_by_owner(db_user)
            else ""
        )
        await safe_reply_text(
            update.message,
            "👋 Welcome back! Send me a Gmail with *jkr* in it to submit.\n\nExample: `andysalmonjkr@gmail.com`"
            + referred_note,
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
    referred_by = context.user_data.pop("referred_by", None)
    upsert_user_profile(user, full_name, bank_name, account_no, referred_by=referred_by)
    db_user = get_user(user.id)

    if context.user_data.get("update_profile"):
        context.user_data.pop("update_profile", None)
        await safe_reply_text(
            update.message,
            f"✅ Profile updated successfully!\n\nName: {html_escape(full_name)}\nBank: {html_escape(bank_name.strip())}\nAccount: {html_escape(account_no.strip())}",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    referral_line = "\n⭐ Referred User: Yes" if is_referred_by_owner(db_user) else ""
    await safe_reply_text(
        update.message,
        f"✅ *Registered successfully!*\n\nName: {context.user_data['full_name']}\nBank: {bank_name.strip()}\nAccount: {account_no.strip()}{referral_line}\n\nYou'll receive ₦{PAYMENT_PER_EMAIL:,} per approved email. Payments go out daily at 6PM.\n\nNow send me a Gmail with *jkr* in it to get started!",
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
        f"Username: {html_escape(username_display)}\n"
        f"Referral: {'⭐ Referred User' if is_referred_by_owner(user) else 'None'}"
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
        upsert_user_profile(user, full_name, bank_name, account_no)

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
pending_request_messages = {}


def cancel_worker_reminder(submission_id: int):
    task = worker_reminder_tasks.pop(submission_id, None)
    if task is not None:
        task.cancel()


def add_pending_request_message(submission_id: int, chat_id: int, message_id: int):
    pending_request_messages.setdefault(submission_id, []).append((chat_id, message_id))


def clear_pending_request_messages(submission_id: int):
    return pending_request_messages.pop(submission_id, [])


async def invalidate_pending_request_messages(bot, submission_id: int, claimer_username: str, claimer_id: int, exclude_message_id: int = None):
    messages = clear_pending_request_messages(submission_id)
    for chat_id, message_id in messages:
        if exclude_message_id is not None and message_id == exclude_message_id:
            continue
        text = (
            f"✅ This mail has been claimed by @{html_escape(claimer_username)}."
            if chat_id == claimer_id else
            f"⚠️ This mail was claimed by @{html_escape(claimer_username)} and is no longer available."
        )
        try:
            await safe_edit_chat_message(
                bot,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Failed to invalidate pending request message %s for submission %s.",
                message_id,
                submission_id,
            )


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

    if action in {"approve", "reject", "markpaid", "markpaidbatch"}:
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
    elif action == "withdrawrefs":
        if not is_owner_user(query.from_user):
            await query.answer("Only TechPastor can use this action.", show_alert=True)
            return
    else:
        await query.answer("Unknown action.", show_alert=True)
        return

    logger.info("handle_callback from user %s: %s", user_id, data)
    await query.answer()

    if action == "editprofile":
        db_user = get_user(user_id)
        context.user_data["profile_edit"] = "name"
        context.user_data["full_name"] = db_user["full_name"] if db_user else ""
        await safe_edit_message_text(
            query,
            "📝 Send your updated *full name* as it appears on your bank account.",
            parse_mode="Markdown",
        )
        return

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

        await invalidate_pending_request_messages(
            context.bot,
            sub_id,
            worker_username,
            user_id,
            exclude_message_id=query.message.message_id,
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

        await safe_edit_message_text(
            query,
            (
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

        if new_status == "approved":
            credit_owner_referral_earning(sub_id)

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

    if action == "markpaidbatch":
        target_day = parts[1].strip() if len(parts) > 1 and parts[1].strip() else datetime.now().strftime("%Y-%m-%d")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", target_day):
            await safe_edit_message_text(query, "⚠️ Invalid payout date.")
            return

        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    COUNT(s.id) as approved_count
                FROM submissions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.paid=0 AND s.status='approved' AND DATE(s.submitted_at)=?
                GROUP BY u.user_id
                ORDER BY approved_count DESC
                """,
                (target_day,),
            ).fetchall()

            if not rows:
                await safe_edit_message_text(
                    query,
                    f"⚠️ No pending approved mails found for <b>{html_escape(target_day)}</b>.",
                    parse_mode="HTML",
                )
                return

            total_customers = len(rows)
            total_mails = sum((row["approved_count"] or 0) for row in rows)
            total_amount = total_mails * PAYMENT_PER_EMAIL

            conn.execute(
                "UPDATE submissions SET paid=1, paid_at=datetime('now') WHERE status='approved' AND paid=0 AND DATE(submitted_at)=?",
                (target_day,),
            )
            conn.commit()

        notified = 0
        notify_failed = 0
        for row in rows:
            user_amount = (row["approved_count"] or 0) * PAYMENT_PER_EMAIL
            view_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("View Details", callback_data=f"viewdetails|{row['user_id']}|{target_day}")]
            ])
            try:
                await safe_send_message(
                    context.bot,
                    chat_id=row["user_id"],
                    text=(
                        f"💰 <b>Payment Update</b>\n\n"
                        f"Your payout for <b>{html_escape(target_day)}</b> has been paid ✅\n"
                        f"Approved mails: {row['approved_count']}\n"
                        f"Amount paid: <b>₦{user_amount:,}</b>\n\n"
                        "Tap below to view details."
                    ),
                    parse_mode="HTML",
                    reply_markup=view_keyboard,
                )
                notified += 1
            except Exception:
                notify_failed += 1
                logger.exception("Failed to notify paid customer %s for date %s", row["user_id"], target_day)

        actor_display = f"@{query.from_user.username}" if query.from_user.username else str(user_id)
        await safe_edit_message_text(
            query,
            (
                f"✅ <b>PAID Recorded</b>\n\n"
                f"Date: <b>{html_escape(target_day)}</b>\n"
                f"Customers: {total_customers}\n"
                f"Mails: {total_mails}\n"
                f"Total paid: <b>₦{total_amount:,}</b>\n"
                f"Customers notified: {notified}\n"
                f"Notification failures: {notify_failed}\n"
                f"Marked by: {html_escape(actor_display)}"
            ),
            parse_mode="HTML",
        )
        return

    if action == "withdrawrefs":
        decision = parts[1] if len(parts) > 1 else ""
        if decision == "cancel":
            await safe_edit_message_text(query, "Withdrawal cancelled.")
            return

        if decision != "confirm":
            await safe_edit_message_text(query, "Invalid withdrawal action.")
            return

        with get_db() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as pending_count,
                    COALESCE(SUM(amount), 0) as pending_amount
                FROM owner_referral_earnings
                WHERE status='pending'
                """
            ).fetchone()
            pending_count = row["pending_count"] or 0
            pending_amount = row["pending_amount"] or 0

            if pending_count == 0:
                await safe_edit_message_text(query, "No pending referral earnings to withdraw.")
                return

            conn.execute(
                "UPDATE owner_referral_earnings SET status='paid', paid_at=datetime('now') WHERE status='pending'"
            )
            conn.execute(
                "INSERT INTO owner_referral_withdrawals (amount, mail_count) VALUES (?, ?)",
                (pending_amount, pending_count),
            )
            conn.commit()

        await safe_edit_message_text(
            query,
            (
                "✅ <b>Referral Withdrawal Successful</b>\n\n"
                f"Successful mails settled: {pending_count}\n"
                f"Amount withdrawn: <b>₦{pending_amount:,}</b>"
            ),
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
        credit_owner_referral_earning(sub_id)
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
    rows = get_daily_unpaid_customer_rows(today)
    summary_text, _, _, _ = build_daily_payout_summary(today, rows)

    if not rows:
        await safe_send_message(
            bot,
            chat_id=ADMIN_GROUP_CHAT_ID,
            text=summary_text,
            parse_mode="HTML",
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("PAID ✅", callback_data=f"markpaidbatch|{today}")]
    ])
    await safe_send_message(
        bot,
        chat_id=ADMIN_GROUP_CHAT_ID,
        text=summary_text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


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

    msg = await safe_reply_text(
        update.message,
        (
            f"📨 <b>Unclaimed Mail</b>\n\n"
            f"Email: <code>{html_escape(submission['email'])}</code>\n"
            f"Submission ID: #{submission['id']}\n"
            f"Status: <b>Unclaimed</b>\n\n"
            f"Press Claim to assign it to yourself."
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    add_pending_request_message(submission['id'], update.effective_chat.id, msg.message_id)


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
    user_id = update.effective_user.id
    if update.effective_chat.type == 'private':
        if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
            await safe_reply_text(update.message, "You are not authorized to view customer stats.")
            return
    elif update.effective_chat.id != ADMIN_GROUP_CHAT_ID:
        return
    else:
        if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
            await safe_reply_text(update.message, "You are not authorized to view customer stats.")
            return

    target_day = datetime.now().strftime("%Y-%m-%d")
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) > 1 and args[1].strip():
        requested_day = args[1].strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", requested_day):
            target_day = requested_day
        else:
            await safe_reply_text(
                update.message,
                "❌ Invalid date format. Use /customer_stats YYYY-MM-DD or send the command with no date for today.",
                parse_mode="HTML",
            )
            return

    rows = get_daily_unpaid_customer_rows(target_day)
    summary_text, _, _, _ = build_daily_payout_summary(target_day, rows)

    if not rows:
        await safe_reply_text(update.message, summary_text, parse_mode="HTML")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("PAID ✅", callback_data=f"markpaidbatch|{target_day}")]
    ])
    await safe_reply_text(
        update.message,
        summary_text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def my_referral_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        await safe_reply_text(update.message, "Use this command in private chat with the bot.")
        return
    if not is_owner_user(update.effective_user):
        await safe_reply_text(update.message, "Only TechPastor can use this command.")
        return

    with get_db() as conn:
        referred_users = conn.execute(
            "SELECT COUNT(*) as count FROM users WHERE lower(COALESCE(referred_by, ''))=?",
            (OWNER_REFERRAL_TAG,),
        ).fetchone()["count"]
        earnings = conn.execute(
            """
            SELECT
                COUNT(*) as successful_mails,
                SUM(CASE WHEN status='pending' THEN amount ELSE 0 END) as pending_earnings,
                SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) as paid_earnings
            FROM owner_referral_earnings
            """
        ).fetchone()

    successful_mails = earnings["successful_mails"] or 0
    pending_earnings = earnings["pending_earnings"] or 0
    paid_earnings = earnings["paid_earnings"] or 0

    await safe_reply_text(
        update.message,
        (
            "<b>Referral Earnings Summary</b>\n\n"
            f"Total Referred Users: {referred_users}\n"
            f"Successful Mails: {successful_mails}\n"
            f"Pending Earnings: ₦{pending_earnings:,}\n"
            f"Paid Earnings: ₦{paid_earnings:,}\n\n"
            f"Rate per successful mail: ₦{OWNER_REFERRAL_RATE}"
        ),
        parse_mode="HTML",
    )


async def withdraw_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        await safe_reply_text(update.message, "Use this command in private chat with the bot.")
        return
    if not is_owner_user(update.effective_user):
        await safe_reply_text(update.message, "Only TechPastor can use this command.")
        return

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) as pending_count,
                COALESCE(SUM(amount), 0) as pending_amount
            FROM owner_referral_earnings
            WHERE status='pending'
            """
        ).fetchone()

    pending_count = row["pending_count"] or 0
    pending_amount = row["pending_amount"] or 0
    if pending_count == 0:
        await safe_reply_text(update.message, "No pending referral earnings to withdraw.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirm", callback_data="withdrawrefs|confirm")],
        [InlineKeyboardButton("Cancel", callback_data="withdrawrefs|cancel")],
    ])
    await safe_reply_text(
        update.message,
        (
            "You are about to withdraw:\n\n"
            f"Total Earnings: ₦{pending_amount:,}\n"
            f"Successful mails: {pending_count}"
        ),
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
    app.add_handler(CommandHandler("customer_stats", customer_stats))
    app.add_handler(MessageHandler(filters.Regex(r"^/customer\-stats(?:\s|$)"), customer_stats))
    app.add_handler(CommandHandler("my_referral_stats", my_referral_stats, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.Regex(r"^/my\-referral\-stats(?:\s|$)") & filters.ChatType.PRIVATE, my_referral_stats))
    app.add_handler(CommandHandler("withdraw_referrals", withdraw_referrals, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.Regex(r"^/withdraw\-referrals(?:\s|$)") & filters.ChatType.PRIVATE, withdraw_referrals))
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
