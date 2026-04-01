# JKR Mail Submission Bot — Setup Guide

## 1. Prerequisites
- Python 3.10+
- A VPS (or local machine for testing)
- A Telegram bot token from @BotFather
- Your admin Telegram group ID

---

## 2. Installation

```bash
# Clone or upload the bot files to your VPS
cd jkr_bot

# Install dependencies
pip install -r requirements.txt
```

---

## 3. Configuration

Open `config.py` and fill in:

```python
BOT_TOKEN = "your_token_from_botfather"
ADMIN_GROUP_ID = -1001234567890   # How to get this: see below
PAYMENT_PER_EMAIL = 200
```

### How to get your Group ID:
1. Add @userinfobot to your Telegram group
2. It will print the group's ID (starts with -100...)
3. Paste that into `config.py`

### Important: Add your bot to the group
- Add the bot as an admin in your Telegram group
- It needs permission to send messages

---

## 4. Run the bot

```bash
python bot.py
```

### Run with PM2 (recommended for VPS):
```bash
npm install -g pm2
pm2 start bot.py --interpreter python3 --name jkr-bot
pm2 save
pm2 startup
```

---

## 5. How it works

### Users:
- `/start` → enter name + bank details → registered
- Send any Gmail with `jkr` in it → submitted for review
- `/stats` → see their approved/pending/rejected counts and pending payout

### You (admin group):
- Each submission arrives with ✅ Approve / ❌ Reject buttons
- Approve → user gets notified, email queued for 6PM payout
- Reject → type the reason in the group chat → user gets notified with reason

### 6PM daily:
- Bot posts a full payout summary in your group
- Shows: name, bank, account number, email count, amount owed
- You pay manually, bot marks them as paid automatically

---

## 6. File structure

```
jkr_bot/
├── bot.py          # Main bot logic
├── config.py       # Your tokens and settings
├── requirements.txt
├── jkr_bot.db      # Auto-created SQLite database
└── README.md
```

---

## 7. Notes
- Duplicate emails (already pending or approved) are automatically blocked
- The database file `jkr_bot.db` holds all user data — back it up regularly
- Payments from previous days that weren't processed carry over to the next summary
