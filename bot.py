import asyncio
import calendar
import fcntl
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# State constants
(
    ORDER_NAME, ORDER_PHONE, ORDER_SELECT_COOKIE, ORDER_KILOS, ORDER_DATE,
    ORDER_PAYMENT, ORDER_RECEIPT, ORDER_NOTE, ORDER_CONFIRM,
    ADMIN_MENU, ADMIN_ADD_COOKIE_NAME, ADMIN_ADD_COOKIE_PRICE,
    ADMIN_ADD_PAYMENT_METHOD, ADMIN_ADD_PAYMENT_DETAILS, ADMIN_MESSAGE_TEXT
) = range(15)

# Single consolidated Orders sheet headers
ORDERS_HEADERS = [
    "Order ID", "Created At", "Updated At", "Status", "Customer Telegram ID",
    "Customer Username", "Full Name", "Phone Number", "Cookie Items JSON",
    "Cookie Summary", "Total Kilos", "Subtotal", "Payment Method",
    "Delivery Date", "Receipt File ID", "Receipt File Unique ID",
    "Special Note", "Approved At", "Ready At", "Admin Action By",
]

# Cookies and Payments stored as simplified records
COOKIES_HEADERS = ["Cookie Type", "Price Per Kilo", "Active"]
PAYMENTS_HEADERS = ["Payment Method", "Details", "Active"]
BOT_LOCK_PATH = "/tmp/hof_bot.lock"


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: set[int]
    spreadsheet_id: str
    admin_email: str
    service_account_json: str
    business_name: str
    currency: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(value: Decimal, currency: str) -> str:
    return f"{value.quantize(Decimal('0.01'))} {currency}"


def parse_decimal(raw: str, field_name: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return value


def clean_phone(raw: str) -> str:
    phone = raw.strip()
    if not re.fullmatch(r"[+\d][\d\s().-]{6,24}", phone):
        raise ValueError("Please send a valid phone number, like +251911123456.")
    return phone


def parse_settings() -> Settings:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin_ids_raw = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in .env")
    if not admin_ids_raw:
        raise RuntimeError("ADMIN_TELEGRAM_IDS is missing in .env")
    admin_ids = {int(item.strip()) for item in admin_ids_raw.split(",") if item.strip()}
    if not admin_ids:
        raise RuntimeError("ADMIN_TELEGRAM_IDS must include at least one Telegram user ID")
    return Settings(
        token=token,
        admin_ids=admin_ids,
        spreadsheet_id=os.getenv("SPREADSHEET_ID", "").strip(),
        admin_email=os.getenv("ADMIN_EMAIL", "").strip(),
        service_account_json=service_account_json,
        business_name=os.getenv("BUSINESS_NAME", "Home Bakery").strip(),
        currency=os.getenv("CURRENCY", "ETB").strip(),
    )


class SheetStore:
    def __init__(self, settings: Settings):
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_file(settings.service_account_json, scopes=scopes)
        self.client = gspread.authorize(credentials)
        self.settings = settings
        self.sheet = self._open_or_create_sheet()
        # Single consolidated data worksheet
        self.orders = self._ensure_worksheet("Orders", ORDERS_HEADERS)
        self.cookies = self._ensure_worksheet("Cookies", COOKIES_HEADERS)
        self.payments = self._ensure_worksheet("Payments", PAYMENTS_HEADERS)

    def _open_or_create_sheet(self) -> gspread.Spreadsheet:
        if self.settings.spreadsheet_id:
            return self.client.open_by_key(self.settings.spreadsheet_id)
        if not self.settings.admin_email:
            raise RuntimeError("Set SPREADSHEET_ID or ADMIN_EMAIL in .env")
        spreadsheet = self.client.create(f"{self.settings.business_name} Orders")
        spreadsheet.share(self.settings.admin_email, perm_type="user", role="writer", notify=True)
        logging.warning("Created Google Sheet: %s", spreadsheet.url)
        logging.warning("Paste this spreadsheet ID into .env as SPREADSHEET_ID: %s", spreadsheet.id)
        return spreadsheet

    def _ensure_worksheet(self, title: str, headers: list[str]) -> gspread.Worksheet:
        try:
            worksheet = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = self.sheet.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
        first_row = worksheet.row_values(1)
        if first_row != headers:
            worksheet.resize(rows=max(worksheet.row_count, 1000), cols=max(len(headers), worksheet.col_count))
            worksheet.update(range_name="1:1", values=[headers])
        return worksheet

    def active_cookies(self) -> list[dict[str, Any]]:
        records = self.cookies.get_all_records()
        return [row for row in records if str(row.get("Active", "")).upper() == "TRUE"]

    def active_payments(self) -> list[dict[str, Any]]:
        records = self.payments.get_all_records()
        return [row for row in records if str(row.get("Active", "")).upper() == "TRUE"]

    def upsert_cookie(self, name: str, price: Decimal) -> None:
        records = self.cookies.get_all_records()
        for idx, row in enumerate(records, start=2):
            if str(row.get("Cookie Type", "")).casefold() == name.casefold():
                self.cookies.update(
                    range_name=f"A{idx}:C{idx}",
                    values=[[name, str(price), "TRUE"]],
                )
                return
        self.cookies.append_row([name, str(price), "TRUE"], value_input_option="USER_ENTERED")

    def add_payment(self, method: str, details: str) -> None:
        records = self.payments.get_all_records()
        for idx, row in enumerate(records, start=2):
            if str(row.get("Payment Method", "")).casefold() == method.casefold():
                self.payments.update(
                    range_name=f"A{idx}:C{idx}",
                    values=[[method, details, "TRUE"]],
                )
                return
        self.payments.append_row([method, details, "TRUE"], value_input_option="USER_ENTERED")

    def append_order(self, order: dict[str, Any]) -> None:
        values = [order.get(header, "") for header in ORDERS_HEADERS]
        self.orders.append_row(values, value_input_option="USER_ENTERED")

    def recent_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        records = self.orders.get_all_records()
        return list(reversed(records[-limit:]))

    def find_order_row(self, order_id: str) -> tuple[int, dict[str, Any]] | None:
        records = self.orders.get_all_records()
        for idx, row in enumerate(records, start=2):
            if str(row.get("Order ID", "")) == order_id:
                return idx, row
        return None

    def set_order_status(self, order_id: str, status: str, admin_label: str) -> dict[str, Any]:
        found = self.find_order_row(order_id)
        if not found:
            raise ValueError("Order not found.")
        row_idx, row = found
        timestamp = now_iso()
        updates = {
            "Updated At": timestamp,
            "Status": status,
            "Admin Action By": admin_label,
        }
        if status == "APPROVED":
            updates["Approved At"] = timestamp
        if status == "READY":
            updates["Ready At"] = timestamp
        header_to_col = {header: idx + 1 for idx, header in enumerate(ORDERS_HEADERS)}
        cells = []
        for header, value in updates.items():
            cell = gspread.Cell(row_idx, header_to_col[header], value)
            cells.append(cell)
            row[header] = value
        self.orders.update_cells(cells, value_input_option="USER_ENTERED")
        return row


def get_store(context: ContextTypes.DEFAULT_TYPE) -> SheetStore:
    return context.application.bot_data["store"]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return bool(user_id and user_id in settings.admin_ids)


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings = get_settings(context)
        user_id = update.effective_user.id if update.effective_user else None
        if update.effective_chat and update.effective_chat.type != "private":
            if update.callback_query:
                await update.callback_query.answer("Use admin actions in private chat.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("For security, admin commands only work in a private chat with the bot.")
            return ConversationHandler.END
        if not is_admin(user_id, settings):
            if update.callback_query:
                await update.callback_query.answer("Admin only.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("This command is only available to admins.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


async def run_sheet(context: ContextTypes.DEFAULT_TYPE, method_name: str, *args):
    store = get_store(context)
    method = getattr(store, method_name)
    return await asyncio.to_thread(method, *args)


def main_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    """Main menu with buttons for customer and admin options."""
    buttons = [
        [InlineKeyboardButton("📝 Place Order", callback_data="start_order")],
        [
            InlineKeyboardButton("🍪 Prices", callback_data="view_prices"),
            InlineKeyboardButton("💳 Payment Methods", callback_data="view_payments"),
        ],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_menu")])
    return InlineKeyboardMarkup(buttons)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Admin panel with all management options."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍪 Add Cookie", callback_data="admin_add_cookie")],
        [InlineKeyboardButton("💳 Add Payment Method", callback_data="admin_add_payment")],
        [InlineKeyboardButton("📋 View Orders", callback_data="admin_view_orders")],
        [InlineKeyboardButton("💰 View Cookies & Prices", callback_data="admin_view_cookies")],
        [InlineKeyboardButton("🏦 View Payment Methods", callback_data="admin_view_payments")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")],
    ])


def back_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    """Small navigation keyboard for informational pages."""
    rows = []
    if is_admin:
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_menu")])
    rows.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")])
    return InlineKeyboardMarkup(rows)


def calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    """Inline delivery-date calendar."""
    today = date.today()
    prev_year, prev_month = shift_month(year, month, -1)
    next_year, next_month = shift_month(year, month, 1)
    rows = [
        [InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="cal:noop")],
        [
            InlineKeyboardButton("Mon", callback_data="cal:noop"),
            InlineKeyboardButton("Tue", callback_data="cal:noop"),
            InlineKeyboardButton("Wed", callback_data="cal:noop"),
            InlineKeyboardButton("Thu", callback_data="cal:noop"),
            InlineKeyboardButton("Fri", callback_data="cal:noop"),
            InlineKeyboardButton("Sat", callback_data="cal:noop"),
            InlineKeyboardButton("Sun", callback_data="cal:noop"),
        ],
    ]

    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal:noop"))
                continue
            selected_date = date(year, month, day)
            if selected_date < today:
                row.append(InlineKeyboardButton("·", callback_data="cal:noop"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal:date:{selected_date.isoformat()}"))
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton("Prev", callback_data=f"cal:month:{prev_year}:{prev_month}"),
            InlineKeyboardButton("Next", callback_data=f"cal:month:{next_year}:{next_month}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Move a year/month pair by whole months."""
    month_index = year * 12 + (month - 1) + delta
    return month_index // 12, month_index % 12 + 1


def order_keyboard(cookies: list[dict[str, Any]], selected: set[str], currency: str) -> InlineKeyboardMarkup:
    """Cookie selection with toggle buttons."""
    rows = []
    for idx, cookie in enumerate(cookies):
        name = str(cookie["Cookie Type"])
        price = money(Decimal(str(cookie["Price Per Kilo"])), currency)
        prefix = "✓ " if name in selected else "  "
        rows.append([InlineKeyboardButton(f"{prefix}{name} ({price}/kg)", callback_data=f"cookie:{idx}")])
    rows.append([InlineKeyboardButton("✅ Done Selecting", callback_data="cookie_done")])
    return InlineKeyboardMarkup(rows)


def payment_keyboard(payments: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Payment method selection."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(str(row["Payment Method"]), callback_data=f"payment:{idx}")] for idx, row in enumerate(payments)]
    )


def admin_order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    """Admin action buttons for orders."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{order_id}"),
                InlineKeyboardButton("🚀 Ready", callback_data=f"admin:ready:{order_id}"),
            ],
            [
                InlineKeyboardButton("🧾 Receipt Issue", callback_data=f"admin:receipt_issue:{order_id}"),
                InlineKeyboardButton("💬 Message", callback_data=f"adminmsg:{order_id}"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main entry point with button menu."""
    settings = get_settings(context)
    user_id = update.effective_user.id if update.effective_user else None
    is_user_admin = is_admin(user_id, settings)
    text = f"Welcome to {settings.business_name}! 👋"

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, reply_markup=main_menu_keyboard(is_user_admin))
        return

    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard(is_user_admin))


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the same menu from Telegram's command menu."""
    await start(update, context)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's Telegram ID for admin setup."""
    user = update.effective_user
    await update.effective_message.reply_text(f"Your Telegram ID is: {user.id}")


async def send_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active cookies and prices."""
    settings = get_settings(context)
    rows = await run_sheet(context, "active_cookies")
    is_user_admin = is_admin(update.effective_user.id if update.effective_user else None, settings)
    if not rows:
        text = "No cookie types are available yet."
    else:
        text = "🍪 Cookies & Prices:\n\n" + "\n".join(
            f"• {row['Cookie Type']}: {money(Decimal(str(row['Price Per Kilo'])), settings.currency)}/kg"
            for row in rows
        )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard(is_user_admin))
    else:
        await update.effective_message.reply_text(text, reply_markup=back_keyboard(is_user_admin))


async def send_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active payment methods."""
    settings = get_settings(context)
    rows = await run_sheet(context, "active_payments")
    is_user_admin = is_admin(update.effective_user.id if update.effective_user else None, settings)
    if not rows:
        text = "No payment methods are available yet."
    else:
        text = "💳 Payment Methods:\n\n" + "\n\n".join(
            f"• {row['Payment Method']}\n{row.get('Details', '')}"
            for row in rows
        )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard(is_user_admin))
    else:
        await update.effective_message.reply_text(text, reply_markup=back_keyboard(is_user_admin))


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle main menu button clicks."""
    query = update.callback_query
    await query.answer()

    if query.data == "view_prices":
        await send_prices(update, context)
    elif query.data == "view_payments":
        await send_payments(update, context)
    elif query.data == "back_to_main":
        await start(update, context)


@admin_only
async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display admin panel."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "Admin Panel 🔧",
            reply_markup=admin_menu_keyboard()
        )
        return ADMIN_MENU

    if update.effective_message:
        await update.effective_message.reply_text(
            "Admin Panel 🔧",
            reply_markup=admin_menu_keyboard()
        )
    return ADMIN_MENU


@admin_only
async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle admin menu interactions."""
    query = update.callback_query
    await query.answer()
    
    if query.data in {"admin_menu", "admin_back"}:
        await query.edit_message_text("Admin Panel 🔧", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU

    if query.data == "admin_add_cookie":
        await query.edit_message_text("Enter cookie name:")
        return ADMIN_ADD_COOKIE_NAME
    
    elif query.data == "admin_add_payment":
        await query.edit_message_text("Enter payment method name:")
        return ADMIN_ADD_PAYMENT_METHOD
    
    elif query.data == "admin_view_orders":
        rows = await run_sheet(context, "recent_orders", 10)
        if not rows:
            await query.edit_message_text("No orders yet.", reply_markup=back_keyboard(True))
        else:
            text = "📋 Recent Orders:\n\n" + "\n\n".join(
                f"🔹 {row['Order ID']}\n📛 {row['Full Name']}\n💰 {row['Cookie Summary']}\n"
                f"📅 {row['Delivery Date']} | 🔴 {row['Status']}"
                for row in rows
            )
            await query.edit_message_text(text[:3900], reply_markup=back_keyboard(True))
        return ADMIN_MENU
    
    elif query.data == "admin_view_cookies":
        rows = await run_sheet(context, "active_cookies")
        if not rows:
            await query.edit_message_text("No cookies available yet.", reply_markup=back_keyboard(True))
        else:
            currency = get_settings(context).currency
            text = "🍪 Available Cookies:\n\n" + "\n".join(
                f"🔹 {row['Cookie Type']}: {money(Decimal(str(row['Price Per Kilo'])), currency)}/kg"
                for row in rows
            )
            await query.edit_message_text(text, reply_markup=back_keyboard(True))
        return ADMIN_MENU
    
    elif query.data == "admin_view_payments":
        rows = await run_sheet(context, "active_payments")
        if not rows:
            await query.edit_message_text("No payment methods available yet.", reply_markup=back_keyboard(True))
        else:
            text = "💳 Payment Methods:\n\n" + "\n\n".join(
                f"🔹 {row['Payment Method']}\n{row.get('Details', '')}"
                for row in rows
            )
            await query.edit_message_text(text, reply_markup=back_keyboard(True))
        return ADMIN_MENU
    
    elif query.data == "back_to_main":
        await start(update, context)
        return ConversationHandler.END
    
    return ADMIN_MENU


async def add_cookie_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect cookie name."""
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 80:
        await update.message.reply_text("Cookie name must be 2-80 characters.")
        return ADMIN_ADD_COOKIE_NAME
    context.user_data["cookie_name"] = name
    await update.message.reply_text(f"Enter price per kilo for {name}:")
    return ADMIN_ADD_COOKIE_PRICE


async def add_cookie_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect and save cookie price."""
    try:
        price = parse_decimal(update.message.text, "Price")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ADMIN_ADD_COOKIE_PRICE
    
    name = context.user_data.get("cookie_name", "")
    await run_sheet(context, "upsert_cookie", name, price)
    
    settings = get_settings(context)
    await update.message.reply_text(
        f"✅ Saved {name} at {money(price, settings.currency)}/kg"
    )
    context.user_data.pop("cookie_name", None)
    await show_admin_menu(update, context)
    return ADMIN_MENU


async def add_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect payment method name."""
    method = update.message.text.strip()
    if len(method) < 2 or len(method) > 80:
        await update.message.reply_text("Payment method must be 2-80 characters.")
        return ADMIN_ADD_PAYMENT_METHOD
    context.user_data["payment_method"] = method
    await update.message.reply_text(f"Enter payment details for {method}:")
    return ADMIN_ADD_PAYMENT_DETAILS


async def add_payment_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect and save payment method."""
    details = update.message.text.strip()
    if len(details) < 3 or len(details) > 200:
        await update.message.reply_text("Details must be 3-200 characters.")
        return ADMIN_ADD_PAYMENT_DETAILS
    
    method = context.user_data.get("payment_method", "")
    await run_sheet(context, "add_payment", method, details)
    
    await update.message.reply_text(f"✅ Saved payment method: {method}")
    context.user_data.pop("payment_method", None)
    await show_admin_menu(update, context)
    return ADMIN_MENU


async def begin_order_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start customer order."""
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("For privacy, please place orders in a private chat with the bot.")
        return ConversationHandler.END
    
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Let's place your order! What's your full name?")
    else:
        await update.message.reply_text("Let's place your order! What's your full name?")
    
    context.user_data["order"] = {"selected": set(), "kilos": {}}
    return ORDER_NAME


async def order_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect customer name."""
    name = update.message.text.strip()
    if len(name) < 3 or len(name) > 80:
        await update.message.reply_text("Please enter your full name (3-80 characters).")
        return ORDER_NAME
    context.user_data["order"]["full_name"] = name
    await update.message.reply_text("What's your phone number?")
    return ORDER_PHONE


async def order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect and validate phone."""
    try:
        phone = clean_phone(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ORDER_PHONE
    context.user_data["order"]["phone"] = phone
    cookies = await run_sheet(context, "active_cookies")
    if not cookies:
        await update.message.reply_text("No cookie types are available yet. Please contact the bakery.")
        return ConversationHandler.END
    context.user_data["order"]["cookies"] = cookies
    settings = get_settings(context)
    await update.message.reply_text(
        "Select one or more cookie types:",
        reply_markup=order_keyboard(cookies, context.user_data["order"]["selected"], settings.currency),
    )
    return ORDER_SELECT_COOKIE


async def select_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle cookie selection."""
    query = update.callback_query
    await query.answer()
    data = query.data
    order = context.user_data["order"]
    selected: set[str] = order["selected"]
    
    if data == "cookie_done":
        if not selected:
            await query.answer("Select at least one cookie type.", show_alert=True)
            return ORDER_SELECT_COOKIE
        order["selected_list"] = [str(cookie["Cookie Type"]) for cookie in order["cookies"] if str(cookie["Cookie Type"]) in selected]
        order["kilo_index"] = 0
        await query.edit_message_text(f"How many kilos of {order['selected_list'][0]}?")
        return ORDER_KILOS
    
    idx = int(data.removeprefix("cookie:"))
    name = str(order["cookies"][idx]["Cookie Type"])
    if name in selected:
        selected.remove(name)
    else:
        selected.add(name)
    settings = get_settings(context)
    await query.edit_message_reply_markup(reply_markup=order_keyboard(order["cookies"], selected, settings.currency))
    return ORDER_SELECT_COOKIE


async def order_kilos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect kilos for each cookie."""
    order = context.user_data["order"]
    cookie_name = order["selected_list"][order["kilo_index"]]
    try:
        kilos = parse_decimal(update.message.text, "Kilos")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ORDER_KILOS
    order["kilos"][cookie_name] = str(kilos)
    order["kilo_index"] += 1
    if order["kilo_index"] < len(order["selected_list"]):
        next_cookie = order["selected_list"][order["kilo_index"]]
        await update.message.reply_text(f"How many kilos of {next_cookie}?")
        return ORDER_KILOS
    today = date.today()
    await update.message.reply_text(
        "Select your delivery date:",
        reply_markup=calendar_keyboard(today.year, today.month),
    )
    return ORDER_DATE


async def select_delivery_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect delivery date from the inline calendar."""
    query = update.callback_query
    await query.answer()
    _, action, *parts = query.data.split(":")

    if action == "noop":
        return ORDER_DATE

    if action == "month":
        year = int(parts[0])
        month = int(parts[1])
        await query.edit_message_reply_markup(reply_markup=calendar_keyboard(year, month))
        return ORDER_DATE

    delivery_date = date.fromisoformat(parts[0])
    if delivery_date < date.today():
        await query.answer("Please choose today or a future date.", show_alert=True)
        return ORDER_DATE

    context.user_data["order"]["delivery_date"] = delivery_date.isoformat()
    await query.edit_message_text(f"Delivery date selected: {delivery_date.isoformat()}")
    return await send_payment_options(update, context)


async def send_payment_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show payment methods after delivery date selection."""
    payments = await run_sheet(context, "active_payments")
    if not payments:
        await update.effective_message.reply_text("No payment methods are available yet. Please contact the bakery.")
        return ConversationHandler.END
    context.user_data["order"]["payments"] = payments
    details = "\n\n".join(f"🔹 {row['Payment Method']}\n{row.get('Details', '')}" for row in payments)
    await update.effective_message.reply_text(
        f"Transfer payment using one of these methods:\n\n{details}\n\nThen select the method:",
        reply_markup=payment_keyboard(payments),
    )
    return ORDER_PAYMENT


async def order_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect payment method."""
    query = update.callback_query
    await query.answer()
    idx = int(query.data.removeprefix("payment:"))
    payment = context.user_data["order"]["payments"][idx]
    context.user_data["order"]["payment_method"] = str(payment["Payment Method"])
    await query.edit_message_text("Upload a screenshot/photo of your payment receipt:")
    return ORDER_RECEIPT


async def order_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect receipt image."""
    if update.message.photo:
        photo = update.message.photo[-1]
        context.user_data["order"]["receipt_file_id"] = photo.file_id
        context.user_data["order"]["receipt_file_unique_id"] = photo.file_unique_id
    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("image/"):
        document = update.message.document
        context.user_data["order"]["receipt_file_id"] = document.file_id
        context.user_data["order"]["receipt_file_unique_id"] = document.file_unique_id
    else:
        await update.message.reply_text("Please send the receipt as a photo or image file.")
        return ORDER_RECEIPT
    await update.message.reply_text("Any special note? (or type /skip to continue)")
    return ORDER_NOTE


async def order_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect optional note."""
    note = update.message.text.strip()
    if len(note) > 500:
        await update.message.reply_text("Please keep the note under 500 characters.")
        return ORDER_NOTE
    context.user_data["order"]["note"] = note
    return await show_confirmation(update, context)


async def skip_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip note and confirm."""
    context.user_data["order"]["note"] = ""
    return await show_confirmation(update, context)


def build_order_totals(order: dict[str, Any], currency: str) -> tuple[list[dict[str, str]], Decimal, Decimal, str]:
    """Calculate order totals."""
    price_map = {str(row["Cookie Type"]): Decimal(str(row["Price Per Kilo"])) for row in order["cookies"]}
    items = []
    total_kilos = Decimal("0")
    subtotal = Decimal("0")
    for cookie_name in order["selected_list"]:
        kilos = Decimal(order["kilos"][cookie_name])
        price = price_map[cookie_name]
        line_total = kilos * price
        total_kilos += kilos
        subtotal += line_total
        items.append({
            "cookie_type": cookie_name,
            "kilos": str(kilos),
            "price_per_kilo": str(price),
            "line_total": str(line_total),
        })
    summary = "\n".join(
        f"🍪 {item['cookie_type']}: {item['kilos']} kg × {money(Decimal(item['price_per_kilo']), currency)} = {money(Decimal(item['line_total']), currency)}"
        for item in items
    )
    return items, total_kilos, subtotal, summary


async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show order confirmation."""
    settings = get_settings(context)
    order = context.user_data["order"]
    _, total_kilos, subtotal, summary = build_order_totals(order, settings.currency)
    text = (
        "✅ CONFIRM YOUR ORDER:\n\n"
        f"👤 Name: {order['full_name']}\n"
        f"📱 Phone: {order['phone']}\n\n"
        f"🍪 Cookies:\n{summary}\n\n"
        f"⚖️ Total: {total_kilos} kg\n"
        f"💰 Amount: {money(subtotal, settings.currency)}\n"
        f"📅 Delivery: {order['delivery_date']}\n"
        f"💳 Payment: {order['payment_method']}\n"
        f"📝 Note: {order.get('note') or '(none)'}"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Confirm", callback_data="confirm_order"), InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]]
    )
    await update.message.reply_text(text, reply_markup=keyboard)
    return ORDER_CONFIRM


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finalize order."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_order":
        context.user_data.pop("order", None)
        await query.edit_message_text("❌ Order cancelled.")
        return ConversationHandler.END

    settings = get_settings(context)
    order = context.user_data["order"]
    items, total_kilos, subtotal, summary = build_order_totals(order, settings.currency)
    user = update.effective_user
    order_id = f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    timestamp = now_iso()
    
    row = {
        "Order ID": order_id,
        "Created At": timestamp,
        "Updated At": timestamp,
        "Status": "PENDING",
        "Customer Telegram ID": str(user.id),
        "Customer Username": user.username or "",
        "Full Name": order["full_name"],
        "Phone Number": order["phone"],
        "Cookie Items JSON": json.dumps(items, ensure_ascii=True),
        "Cookie Summary": summary,
        "Total Kilos": str(total_kilos),
        "Subtotal": str(subtotal),
        "Payment Method": order["payment_method"],
        "Delivery Date": order["delivery_date"],
        "Receipt File ID": order["receipt_file_id"],
        "Receipt File Unique ID": order["receipt_file_unique_id"],
        "Special Note": order.get("note", ""),
    }
    
    await run_sheet(context, "append_order", row)
    await query.edit_message_text(f"🎉 Thank you! Your order was submitted.\n\n🔹 Order ID: {order_id}\n\nYou will be notified when your order is ready.")
    await notify_admins(context, row, settings)
    context.user_data.pop("order", None)
    return ConversationHandler.END


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, row: dict[str, Any], settings: Settings) -> None:
    """Notify admins of new order."""
    caption = (
        f"🆕 NEW ORDER: {row['Order ID']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 {row['Full Name']}\n"
        f"📱 {row['Phone Number']}\n\n"
        f"🍪 Items:\n{row['Cookie Summary']}\n\n"
        f"💰 Total: {money(Decimal(row['Subtotal']), settings.currency)}\n"
        f"📅 Delivery: {row['Delivery Date']}\n"
        f"💳 Payment: {row['Payment Method']}\n"
        f"📝 Note: {row.get('Special Note') or '(none)'}"
    )
    for admin_id in settings.admin_ids:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=row["Receipt File ID"],
                caption=caption,
                reply_markup=admin_order_keyboard(row["Order ID"]),
            )
        except Exception:
            logging.exception("Failed to notify admin %s", admin_id)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel order flow."""
    context.user_data.pop("order", None)
    context.user_data.pop("cookie_name", None)
    context.user_data.pop("payment_method", None)
    context.user_data.pop("admin_message_order_id", None)
    await update.message.reply_text("❌ Cancelled. Use /start to begin again.")
    return ConversationHandler.END


async def cancel_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel any active flow and show the menu."""
    context.user_data.pop("order", None)
    context.user_data.pop("cookie_name", None)
    context.user_data.pop("payment_method", None)
    context.user_data.pop("admin_message_order_id", None)
    await start(update, context)
    return ConversationHandler.END


def admin_label(update: Update) -> str:
    """Get admin identifier."""
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else ""
    return f"{user.id} {username}".strip() if user else "unknown"


async def apply_status(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str, status: str) -> None:
    """Update order status."""
    row = await run_sheet(context, "set_order_status", order_id, status, admin_label(update))
    customer_id = int(row["Customer Telegram ID"])
    if status == "APPROVED":
        message = f"✅ Your order {order_id} has been approved! We'll notify you when it's ready."
    else:
        message = f"🚀 Your order {order_id} is ready for pickup!"
    try:
        await context.bot.send_message(chat_id=customer_id, text=message)
    except Exception:
        logging.exception("Failed to message customer for order %s", order_id)


async def send_customer_message(context: ContextTypes.DEFAULT_TYPE, row: dict[str, Any], message: str) -> None:
    """Send a bakery/admin message to the customer for an order."""
    customer_id = int(row["Customer Telegram ID"])
    order_id = row["Order ID"]
    await context.bot.send_message(
        chat_id=customer_id,
        text=f"Message about order {order_id}:\n\n{message}",
    )


@admin_only
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin button actions."""
    query = update.callback_query
    await query.answer()
    _, action, order_id = query.data.split(":", 2)
    if action == "receipt_issue":
        row = await run_sheet(context, "set_order_status", order_id, "NEEDS_RECEIPT_CORRECTION", admin_label(update))
        message = (
            "Your payment receipt could not be verified. Please send a clear/correct receipt screenshot, "
            "or contact the bakery if you already made the transfer."
        )
        await send_customer_message(context, row, message)
        await query.message.reply_text(f"🧾 Receipt issue message sent for {order_id}.")
        return

    status = "APPROVED" if action == "approve" else "READY"
    await apply_status(update, context, order_id, status)
    await query.edit_message_reply_markup(reply_markup=admin_order_keyboard(order_id))
    await query.message.reply_text(f"✅ {order_id} is now {status}.")


@admin_only
async def begin_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a custom admin-to-customer message."""
    query = update.callback_query
    await query.answer()
    order_id = query.data.removeprefix("adminmsg:").strip()
    context.user_data["admin_message_order_id"] = order_id
    await query.message.reply_text(
        f"Type the message you want to send to the customer for {order_id}.\n\nUse /cancel to stop."
    )
    return ADMIN_MESSAGE_TEXT


@admin_only
async def begin_admin_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start custom messaging from /message ORDER_ID."""
    if not context.args:
        await update.message.reply_text("Use: /message ORDER_ID")
        return ConversationHandler.END
    order_id = context.args[0].strip()
    context.user_data["admin_message_order_id"] = order_id
    await update.message.reply_text(
        f"Type the message you want to send to the customer for {order_id}.\n\nUse /cancel to stop."
    )
    return ADMIN_MESSAGE_TEXT


@admin_only
async def send_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a custom admin message to the customer."""
    order_id = context.user_data.get("admin_message_order_id")
    message = update.message.text.strip()
    if not order_id:
        await update.message.reply_text("No order selected. Use /message ORDER_ID.")
        return ConversationHandler.END
    if len(message) < 2 or len(message) > 1000:
        await update.message.reply_text("Message must be 2-1000 characters.")
        return ADMIN_MESSAGE_TEXT

    found = await run_sheet(context, "find_order_row", order_id)
    if not found:
        await update.message.reply_text("Order not found.")
        context.user_data.pop("admin_message_order_id", None)
        return ConversationHandler.END

    _, row = found
    await send_customer_message(context, row, message)
    context.user_data.pop("admin_message_order_id", None)
    await update.message.reply_text(f"💬 Message sent to customer for {order_id}.")
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors."""
    logging.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Sorry, something went wrong. Please try again.")


async def configure_bot_commands(app) -> None:
    """Register Telegram's native menu commands."""
    settings = app.bot_data["settings"]
    default_commands = [
        BotCommand("menu", "Open the bakery menu"),
        BotCommand("order", "Place a cookie order"),
        BotCommand("prices", "View cookie prices"),
        BotCommand("payments", "View payment methods"),
        BotCommand("cancel", "Cancel the current flow"),
        BotCommand("whoami", "Show your Telegram ID"),
    ]
    admin_commands = [
        BotCommand("menu", "Open the bakery menu"),
        BotCommand("admin", "Open the admin panel"),
        BotCommand("message", "Message a customer by order ID"),
        BotCommand("order", "Place a test order"),
        BotCommand("prices", "View cookie prices"),
        BotCommand("payments", "View payment methods"),
        BotCommand("cancel", "Cancel the current flow"),
        BotCommand("whoami", "Show your Telegram ID"),
    ]
    await app.bot.set_my_commands(default_commands)
    for admin_id in settings.admin_ids:
        try:
            await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logging.exception("Could not set admin menu commands for %s", admin_id)


def build_app(settings: Settings, store: SheetStore):
    """Build the bot application."""
    app = (
        ApplicationBuilder()
        .token(settings.token)
        .concurrent_updates(False)
        .post_init(configure_bot_commands)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["store"] = store

    # Order conversation - handles customer order flow only
    order_conv = ConversationHandler(
        entry_points=[
            CommandHandler("order", begin_order_flow),
            CallbackQueryHandler(begin_order_flow, pattern=r"^start_order$"),
        ],
        states={
            ORDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_name)],
            ORDER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_phone)],
            ORDER_SELECT_COOKIE: [CallbackQueryHandler(select_cookie, pattern=r"^cookie")],
            ORDER_KILOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_kilos)],
            ORDER_DATE: [CallbackQueryHandler(select_delivery_date, pattern=r"^cal:")],
            ORDER_PAYMENT: [CallbackQueryHandler(order_payment, pattern=r"^payment:")],
            ORDER_RECEIPT: [MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, order_receipt)],
            ORDER_NOTE: [
                CommandHandler("skip", skip_note),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_note),
            ],
            ORDER_CONFIRM: [CallbackQueryHandler(confirm_order, pattern=r"^(confirm_order|cancel_order)$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("menu", cancel_to_menu),
            CommandHandler("start", cancel_to_menu),
        ],
        per_message=False,
    )

    # Admin conversation - handles admin menu and management
    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", show_admin_menu),
            CallbackQueryHandler(show_admin_menu, pattern=r"^admin_menu$"),
            CallbackQueryHandler(begin_admin_message, pattern=r"^adminmsg:"),
            CommandHandler("message", begin_admin_message_command),
        ],
        states={
            ADMIN_MENU: [
                CallbackQueryHandler(begin_admin_message, pattern=r"^adminmsg:"),
                CallbackQueryHandler(admin_menu_callback, pattern=r"^(admin_|back_to_main)"),
            ],
            ADMIN_ADD_COOKIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cookie_name)],
            ADMIN_ADD_COOKIE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cookie_price)],
            ADMIN_ADD_PAYMENT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment_method)],
            ADMIN_ADD_PAYMENT_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment_details)],
            ADMIN_MESSAGE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_admin_message)],
        },
        fallbacks=[
            CommandHandler("message", begin_admin_message_command),
            CommandHandler("cancel", cancel),
            CommandHandler("menu", cancel_to_menu),
            CommandHandler("start", cancel_to_menu),
        ],
        per_message=False,
    )

    # Add handlers in order of priority
    app.add_handler(order_conv)
    app.add_handler(admin_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("prices", send_prices))
    app.add_handler(CommandHandler("payments", send_payments))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern=r"^(view_prices|view_payments|back_to_main)$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:(approve|ready|receipt_issue):"))
    app.add_error_handler(on_error)
    return app


def acquire_single_instance_lock() -> object:
    """Ensure only one bot process can poll Telegram for this token."""
    lock_file = open(BOT_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError("Another Bot instance is already running. Only one instance is allowed.")
    return lock_file


def main() -> None:
    """Start the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        lock_file = acquire_single_instance_lock()
    except RuntimeError as exc:
        logging.error(str(exc))
        raise SystemExit(1) from exc

    settings = parse_settings()
    store = SheetStore(settings)
    app = build_app(settings, store)
    logging.info("Starting %s Telegram bot", settings.business_name)
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


if __name__ == "__main__":
    main()
