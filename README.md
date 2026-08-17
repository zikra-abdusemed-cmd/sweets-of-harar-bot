# Home Bakery Telegram Bot

Free-stack Telegram order bot for a small home bakery. Customers can place cookie orders, upload a payment receipt screenshot, and admins can manage orders, cookie types, prices, and payment methods. Orders are written automatically to Google Sheets with timestamps.

## Free Stack

- Telegram Bot API for chat and receipt uploads.
- Google Sheets API for order/config storage.
- A Google service account for bot-to-sheet access.
- Run the bot on your own computer for free with polling.

Google's current Sheets API docs list standard use at no additional cost within quota, with per-minute request limits. Keep order volume modest and avoid exposing the bot publicly to spam.

## Setup

1. Create a Telegram bot with `@BotFather` and copy the token.
2. Create a Google Cloud project, enable Google Sheets API and Google Drive API, create a service account, and download its JSON key.
3. Create a Google Sheet manually and share it with the service account email as Editor.
4. Copy `.env.example` to `.env` and fill:
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_TELEGRAM_IDS`
   - `SPREADSHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`
5. Install and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Use `/whoami` in Telegram to get your admin Telegram ID.

## Customer Flow

- `/order`
- Full name
- Phone number
- Select one or more cookie types
- Enter kilos for each selected type
- Delivery date
- Payment method
- Receipt screenshot
- Optional note
- Confirm order

## Admin Commands

- `/admin` - show admin help
- `/add_cookie Chocolate Chip 950` - add cookie type and price per kilo
- `/set_price Chocolate Chip 1000` - update price per kilo
- `/add_payment Bank Transfer | CBE 1000123456789 - Your Name` - add payment option
- `/prices` - list cookie prices
- `/payments` - list payment methods
- `/approve ORDER_ID` - approve an order
- `/ready ORDER_ID` - mark an approved order as ready and message the customer
- `/orders` - show recent orders

Admins also receive order notifications with inline approve/ready buttons.

## Security Notes

- Keep `.env` and the Google service-account JSON private. They are ignored by git.
- Only Telegram IDs listed in `ADMIN_TELEGRAM_IDS` can run admin commands or press admin buttons.
- The bot stores Telegram receipt `file_id` values in Sheets instead of downloading receipt images to disk.
- Share the Google Sheet only with trusted admins and the service account.
- Rotate the Telegram token and service-account key if either is exposed.
- Run this in a private admin-controlled environment. For production, use a locked-down VPS/container, firewall rules, logs without secrets, and regular dependency updates.
