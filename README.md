# JKR Mail Submission Bot

Telegram bot for collecting Gmail submissions, letting workers process them, and handling daily payouts with confirmation and notifications.

## What It Does

- Registers customers with bank details (`/start`)
- Accepts Gmail submissions that contain `jkr`
- Lets workers claim and process mails as:
  - Done
  - Incorrect Password
  - Not Found
- Tracks balances and stats
- Sends daily payout summary at 6:00 PM
- Adds an admin `PAID ✅` button to confirm payouts and notify all listed customers
- Tracks single-owner referral earnings for TechPastor

## Requirements

- Python 3.10+
- Telegram bot token from @BotFather
- Admin group where payout/work activity is managed

## Install

```bash
pip install -r requirements.txt
```

## Config (`config.py`)

Set these values:

```python
BOT_TOKEN = "..."
ADMIN_GROUP_ID = -100...
ADMIN_USER_IDS = []
WORKER_USER_IDS = []
PAYMENT_PER_EMAIL = 200
```

Notes:
- `ADMIN_USER_IDS = []` means any member in the admin group can use admin-only callbacks.
- Worker access is role-based in the database (`/add-worker`, `/delete-worker`).

## Main Flow

### 1) Customer onboarding

- User sends `/start`
- Bot collects:
  - Full name
  - Bank name + account number
- User is stored in SQLite (`users`)

### 2) Customer submissions

- Customer sends one or more emails in private chat
- Bot validates:
  - valid email format
  - `@gmail.com`
  - contains `jkr`
  - batch size <= 20
- New valid mails are inserted as `pending`

### 3) Worker processing

- Worker sends `/request` in private chat
- Bot returns oldest unclaimed mail with `Claim`
- After claim, worker gets action buttons:
  - `Done`
  - `Incorrect Password`
  - `Not Found`
- Customer is notified after processing

### 4) Daily payout confirmation

- At 6:00 PM the bot posts **Daily Payout Summary** in admin group
- Summary includes `PAID ✅` button
- When admin taps `PAID ✅`:
  - all approved/unpaid submissions for that date are marked paid
  - each listed customer gets a payment notification
  - each customer gets a `View Details` button

## Commands

### Customer (private)

- `/start`
- `/profile`
- `/stats`
- `/balance`

### Worker

- `/request` (private)

### Admin

- `/customer_stats [YYYY-MM-DD]`
- `/worker_stats`
- `/add-worker @username`
- `/delete-worker @username`

### Owner (TechPastor, private)

- `/my_referral_stats` or `/my-referral-stats`
- `/withdraw_referrals` or `/withdraw-referrals`

## Referral System (Single Owner)

Only one referrer exists: **TechPastor**.

Referral entry link format:

```text
https://t.me/<YourBotUsername>?start=techpastor
```

Behavior:
- If a user joins with `start=techpastor`, user is tagged `referred_by="techpastor"`
- Referred users are shown as `⭐ Referred User` in profile
- Every successful mail (`Done` => `approved`) from referred users creates one owner earning row

### Referral earning rules

- Rate: `₦50` per successful mail
- Stored in `owner_referral_earnings`
- Duplicate protection: one earning per mail (`mail_id` is unique)
- Status lifecycle: `pending` -> `paid` (via owner withdrawal)

### Owner withdrawal flow

- Owner sends `/withdraw-referrals`
- Bot shows pending total with `Confirm` / `Cancel`
- Confirm marks all pending referral earnings as paid
- Withdrawal is logged in `owner_referral_withdrawals`

## Database

SQLite file: `jkr_bot.db`

Main tables:
- `users`
- `submissions`
- `owner_referral_earnings`
- `owner_referral_withdrawals`

## Run

```bash
python bot.py
```

## Persistence / Redeploy Notes

- Data is stored in `jkr_bot.db`.
- Pushing code to GitHub does **not** clear mails.
- Redeploying does **not** clear mails **unless** `jkr_bot.db` is deleted/replaced or you deploy on ephemeral storage.
- Keep regular DB backups.
