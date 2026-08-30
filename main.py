"""
╔══════════════════════════════════════════════════════════════════╗
║   TELEGRAM DIGITAL PRODUCTS & SUBSCRIPTION STORE BOT             ║
║   Single-file edition — everything in main.py                    ║
║   Stack: python-telegram-bot v21 + SQLAlchemy async + FastAPI    ║
╚══════════════════════════════════════════════════════════════════╝

SETUP:
  1) pip install python-telegram-bot[ext] SQLAlchemy[asyncio] aiosqlite \
        fastapi "uvicorn[standard]" python-dotenv aiohttp "qrcode[pil]" pydantic
  2) Set env vars below (or create .env)
  3) python main.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta

import aiohttp
import qrcode
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum as SAEnum, Float, ForeignKey, Integer,
    String, Text, func, or_, select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
import enum

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    level=logging.INFO)
log = logging.getLogger("store-bot")

# ══════════════════════════ 1. CONFIG (.env) ══════════════════════════

BOT_TOKEN: str = os.environ["8629098498:AAHnnf00Kzk8ZLmDZXtRfC1TVXjttesIgDI"]
ADMIN_IDS: set[int] = {int(x) for x in os.getenv("6045528121", "").split(",")
                       if x.strip().isdigit()}
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./store.db")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RATE_LIMIT_SECONDS: float = float(os.getenv("RATE_LIMIT_SECONDS", "1.2"))

UPI_ID: str = os.getenv("UPI_ID", "yourname@upi")
UPI_PAYEE_NAME: str = os.getenv("UPI_PAYEE_NAME", "DigitalStore")
CRYPTOBOT_TOKEN: str = os.getenv("CRYPTOBOT_TOKEN", "")
STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "@support")

WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
PUBLIC_URL: str = os.getenv("PUBLIC_URL", "")
REFERRAL_PERCENT: float = float(os.getenv("REFERRAL_PERCENT", "5"))

CURRENCY: str = os.getenv("CURRENCY", "₹")
DIVIDER: str = "━━━━━━━━━━━━"

# ══════════════════════════ 2. DATABASE MODELS ══════════════════════════


class Base(DeclarativeBase):
    pass


class TxnType(str, enum.Enum):
    TOPUP = "topup"; PURCHASE = "purchase"; REFERRAL = "referral"
    ADMIN_ADJUST = "admin_adjust"; REFUND = "refund"


class TxnStatus(str, enum.Enum):
    PENDING = "pending"; COMPLETED = "completed"; REJECTED = "rejected"


class OrderStatus(str, enum.Enum):
    DELIVERED = "delivered"; PENDING_MANUAL = "pending_manual"; CANCELLED = "cancelled"


class TicketStatus(str, enum.Enum):
    OPEN = "open"; CLOSED = "closed"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_code: Mapped[str] = mapped_column(String(16), unique=True)
    referral_earnings: Mapped[float] = mapped_column(Float, default=0.0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    total_spent: Mapped[float] = mapped_column(Float, default=0.0)
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    orders: Mapped[list["Order"]] = relationship(back_populates="user")


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    emoji: Mapped[str] = mapped_column(String(8), default="📦")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    children: Mapped[list["Category"]] = relationship()
    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sold_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    category: Mapped["Category"] = relationship(back_populates="products")
    plans: Mapped[list["Plan"]] = relationship(back_populates="product",
                                               cascade="all, delete-orphan")


class Plan(Base):
    """Duration/variant of a product, e.g. Netflix 1M / 6M / Lifetime."""
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(64))
    price: Mapped[float] = mapped_column(Float)
    original_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    auto_delivery: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    flash_sale_ends: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    product: Mapped["Product"] = relationship(back_populates="plans")
    stock_items: Mapped[list["StockItem"]] = relationship(back_populates="plan",
                                                          cascade="all, delete-orphan")


class StockItem(Base):
    """One license key / account combo in the auto-delivery pool."""
    __tablename__ = "stock_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    content: Mapped[str] = mapped_column(Text)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    plan: Mapped["Plan"] = relationship(back_populates="stock_items")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus),
                                                default=OrderStatus.PENDING_MANUAL)
    coupon_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped["User"] = relationship(back_populates="orders")


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[TxnType] = mapped_column(SAEnum(TxnType))
    status: Mapped[TxnStatus] = mapped_column(SAEnum(TxnStatus), default=TxnStatus.PENDING)
    amount: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(32), default="")
    reference: Mapped[str] = mapped_column(String(64), unique=True)
    screenshot_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    admin_note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Coupon(Base):
    __tablename__ = "coupons"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    percent: Mapped[float] = mapped_column(Float, default=0)
    flat_amount: Mapped[float] = mapped_column(Float, default=0)
    max_uses: Mapped[int] = mapped_column(Integer, default=100)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SupportTicket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subject: Mapped[str] = mapped_column(String(128))
    status: Mapped[TicketStatus] = mapped_column(SAEnum(TicketStatus),
                                                 default=TicketStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BroadcastLog(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    audience: Mapped[str] = mapped_column(String(32))
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Engine / Session ──
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_async_engine(DATABASE_URL, echo=False, connect_args=connect_args)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        if DATABASE_URL.startswith("sqlite"):
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    res = await session.execute(select(User).where(User.telegram_id == tg_user.id))
    user = res.scalar_one_or_none()
    if user:
        user.username = tg_user.username
        user.full_name = tg_user.full_name or ""
        return user
    user = User(telegram_id=tg_user.id, username=tg_user.username,
                full_name=tg_user.full_name or "", referral_code=secrets.token_hex(4))
    session.add(user)
    await session.flush()
    return user


async def count_stock(session: AsyncSession, plan_id: int) -> int:
    res = await session.execute(
        select(func.count(StockItem.id)).where(
            StockItem.plan_id == plan_id, StockItem.is_sold.is_(False)))
    return res.scalar_one()


# ══════════════════════════ 3. UTILS / HELPERS ══════════════════════════

_last_call: dict[int, float] = {}


def rate_limited(user_id: int) -> bool:
    """In-process token bucket. Swap for Redis in multi-instance production."""
    now = time.monotonic()
    if now - _last_call.get(user_id, 0) < RATE_LIMIT_SECONDS:
        return True
    _last_call[user_id] = now
    return False


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def money(v: float) -> str:
    return f"{CURRENCY}{v:,.2f}".rstrip("0").rstrip(".")


def header(title: str) -> str:
    return f"<b>{title}</b>\n{DIVIDER}\n"


def tier_badge(total_spent: float) -> str:
    if total_spent >= 50000: return "💎 Diamond"
    if total_spent >= 20000: return "🥇 Gold"
    if total_spent >= 5000:  return "🥈 Silver"
    return "🥉 Bronze"


async def safe_edit(query, text: str, **kwargs):
    try:
        await query.edit_message_text(text, parse_mode="HTML", **kwargs)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def admin_only(func_):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer("🚫 Admins only.", show_alert=True)
            return
        return await func_(update, context)
    wrapper.__name__ = getattr(func_, "__name__", "wrapped")
    return wrapper


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
    elif update.message:
        await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ── FSM states ──
(
    ADM_PROD_TITLE, ADM_PROD_CATEGORY, ADM_PROD_DESC, ADM_PROD_MEDIA,
    ADM_PLAN_NAME, ADM_PLAN_PRICE, ADM_PLAN_ORIG, ADM_PLAN_AUTO,
    ADM_IMPORT_FILE, ADM_COUPON, ADM_BALANCE, ADM_BROADCAST, ADM_FLASH,
    ADM_USER_SEARCH,
    USR_SEARCH, USR_TICKET_SUBJ, USR_TICKET_BODY, USR_COUPON,
    USR_TOPUP_AMOUNT, USR_SCREENSHOT,
) = range(20)


# ══════════════════════════ 4. PAYMENT GATEWAY ══════════════════════════

def make_reference() -> str:
    return "TXN" + secrets.token_hex(5).upper()


class GatewayError(Exception):
    pass


def build_upi_uri(amount: float, reference: str) -> str:
    params = {"pa": UPI_ID, "pn": UPI_PAYEE_NAME, "am": f"{amount:.2f}",
              "cu": "INR", "tn": reference}
    return "upi://pay?" + urllib.parse.urlencode(params)


def upi_qr_png(amount: float, reference: str) -> bytes:
    img = qrcode.make(build_upi_uri(amount, reference), box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def create_cryptobot_invoice(amount_usdt: float, payload: str) -> str:
    if not CRYPTOBOT_TOKEN:
        raise GatewayError("CRYPTOBOT_TOKEN not configured")
    body = {"asset": "USDT", "amount": f"{amount_usdt:.2f}", "payload": payload,
            "allow_comments": False, "allow_anonymous": False}
    async with aiohttp.ClientSession() as http:
        async with http.post("https://pay.crypt.bot/api/createInvoice", json=body,
                             headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}) as r:
            data = await r.json()
    if not data.get("ok"):
        raise GatewayError(str(data))
    return data["result"]["pay_url"]


async def create_stripe_checkout(amount: float, reference: str,
                                 success_url: str, cancel_url: str) -> str:
    if not STRIPE_SECRET_KEY:
        raise GatewayError("STRIPE_SECRET_KEY not configured")
    form = {
        "mode": "payment", "success_url": success_url, "cancel_url": cancel_url,
        "client_reference_id": reference,
        "line_items[0][price_data][currency]": "inr",
        "line_items[0][price_data][unit_amount]": str(int(amount * 100)),
        "line_items[0][price_data][product_data][name]": f"Wallet Top-up {reference}",
        "line_items[0][quantity]": "1",
    }
    async with aiohttp.ClientSession() as http:
        async with http.post("https://api.stripe.com/v1/checkout/sessions", data=form,
                             auth=aiohttp.BasicAuth(STRIPE_SECRET_KEY, "")) as r:
            data = await r.json()
    if "url" not in data:
        raise GatewayError(str(data))
    return data["url"]


def verify_stripe_signature(payload: bytes, sig_header: str) -> dict | None:
    if not STRIPE_WEBHOOK_SECRET:
        return None
    try:
        parts = dict(kv.split("=", 1) for kv in sig_header.split(","))
        ts, v1 = parts["t"], parts["v1"]
        signed = f"{ts}.{payload.decode()}"
        expect = hmac.new(STRIPE_WEBHOOK_SECRET.encode(),
                          signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, v1) or abs(time.time() - int(ts)) > 300:
            return None
        return json.loads(payload)
    except Exception:
        return None


# ══════════════════════════ 5. KEYBOARDS ══════════════════════════

Btn = InlineKeyboardButton


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def nav_row(back: str | None = None) -> list[InlineKeyboardButton]:
    row = []
    if back:
        row.append(Btn("🔙 Back", callback_data=back))
    row.append(Btn("🏠 Main Menu", callback_data="menu:home"))
    return row


def main_menu() -> InlineKeyboardMarkup:
    return kb([
        [Btn("🛍️ Browse Catalog", callback_data="cat:list"),
         Btn("🔍 Search", callback_data="search:start")],
        [Btn("💳 Wallet & Top-up", callback_data="wallet:menu"),
         Btn("📦 My Orders", callback_data="orders:list:0")],
        [Btn("👥 Refer & Earn", callback_data="ref:menu"),
         Btn("🎟️ Redeem Coupon", callback_data="coupon:redeem")],
        [Btn("💬 24/7 Support", callback_data="support:menu"),
         Btn("📊 My Stats", callback_data="stats:me")],
    ])


def categories_menu(categories) -> InlineKeyboardMarkup:
    rows, pair = [], []
    for c in categories:
        pair.append(Btn(f"{c.emoji} {c.name}", callback_data=f"cat:view:{c.id}"))
        if len(pair) == 2:
            rows.append(pair); pair = []
    if pair:
        rows.append(pair)
    rows.append(nav_row())
    return kb(rows)


def products_menu(products, category_id: int, page: int, has_next: bool):
    rows = [[Btn(f"🛒 {p.title}", callback_data=f"prod:view:{p.id}")] for p in products]
    pager = []
    if page > 0:
        pager.append(Btn("⬅️ Prev", callback_data=f"cat:view:{category_id}:{page-1}"))
    pager.append(Btn("🔄 Refresh", callback_data=f"cat:view:{category_id}:{page}"))
    if has_next:
        pager.append(Btn("➡️ Next", callback_data=f"cat:view:{category_id}:{page+1}"))
    rows.append(pager)
    rows.append(nav_row(back="cat:list"))
    return kb(rows)


def product_card(product_id: int, plans, back_to: str):
    rows = [[Btn(f"💠 {pl.name} — {money(pl.price)}", callback_data=f"plan:buy:{pl.id}")]
            for pl in plans]
    rows.append(nav_row(back=back_to))
    return kb(rows)


def confirm_order(plan_id: int, qty: int) -> InlineKeyboardMarkup:
    return kb([
        [Btn("➖", callback_data=f"plan:qty:{plan_id}:{qty-1}"),
         Btn(f"Qty: {qty}", callback_data="noop"),
         Btn("➕", callback_data=f"plan:qty:{plan_id}:{qty+1}")],
        [Btn("✅ Confirm — Pay from Wallet", callback_data=f"plan:confirm:{plan_id}:{qty}")],
        [Btn("❌ Cancel", callback_data="menu:home")],
    ])


def wallet_menu() -> InlineKeyboardMarkup:
    return kb([
        [Btn("📲 UPI (Instant QR)", callback_data="pay:upi"),
         Btn("🪙 Crypto (USDT)", callback_data="pay:crypto")],
        [Btn("💳 Stripe / Card", callback_data="pay:stripe")],
        [Btn("📜 Transaction History", callback_data="wallet:history:0")],
        nav_row(),
    ])


def upi_payment(txn_id: int) -> InlineKeyboardMarkup:
    return kb([
        [Btn("📸 Upload Payment Screenshot", callback_data=f"pay:shot:{txn_id}")],
        [Btn("🔄 Check Status", callback_data=f"pay:status:{txn_id}")],
        nav_row(back="wallet:menu"),
    ])


def order_actions(order_id: int) -> InlineKeyboardMarkup:
    return kb([
        [Btn("🧾 Receipt", callback_data=f"orders:receipt:{order_id}")],
        nav_row(back="orders:list:0"),
    ])


def refer_menu(bot_username: str, ref_code: str) -> InlineKeyboardMarkup:
    return kb([
        [Btn("🔗 Copy Link", url=f"https://t.me/{bot_username}?start=ref_{ref_code}")],
        nav_row(),
    ])


def support_menu(has_open: bool) -> InlineKeyboardMarkup:
    rows = [[Btn("📝 Open New Ticket", callback_data="support:new")]]
    if has_open:
        rows.append([Btn("📂 My Open Tickets", callback_data="support:list")])
    rows.append(nav_row())
    return kb(rows)


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [Btn("📦 Products", callback_data="adm:products"),
         Btn("🗂️ Categories", callback_data="adm:categories")],
        [Btn("📥 Import Stock", callback_data="adm:import"),
         Btn("🎟️ Coupons", callback_data="adm:coupons")],
        [Btn("💰 Payments Queue", callback_data="adm:payments:0"),
         Btn("👤 Users", callback_data="adm:users")],
        [Btn("📢 Broadcast", callback_data="adm:broadcast"),
         Btn("⚡ Flash Sale", callback_data="adm:flash")],
        [Btn("📊 Analytics", callback_data="adm:analytics"),
         Btn("🎫 Tickets", callback_data="adm:tickets")],
        [Btn("🏠 Exit to Store", callback_data="menu:home")],
    ])


def payment_review(txn_id: int) -> InlineKeyboardMarkup:
    return kb([[
        Btn("✅ Approve", callback_data=f"adm:payok:{txn_id}"),
        Btn("❌ Reject", callback_data=f"adm:payno:{txn_id}"),
    ]])


def user_manage(user_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    return kb([
        [Btn("➕ Add Balance", callback_data=f"adm:baladd:{user_id}"),
         Btn("➖ Deduct", callback_data=f"adm:balsub:{user_id}")],
        [Btn("🚫 Ban" if not is_banned else "✅ Unban",
             callback_data=f"adm:ban:{user_id}:{0 if is_banned else 1}")],
        nav_row(back="adm:menu"),
    ])


def broadcast_targets() -> InlineKeyboardMarkup:
    return kb([
        [Btn("👥 All Users", callback_data="adm:bc:all")],
        [Btn("💰 Balance > 0", callback_data="adm:bc:balance"),
         Btn("🛒 Buyers Only", callback_data="adm:bc:buyers")],
        nav_row(back="adm:menu"),
    ])


# ══════════════════════ 6. USER (CUSTOMER) HANDLERS ══════════════════════

PAGE_SIZE = 6


def _welcome_text(user: User) -> str:
    return (
        f"👋 <b>Welcome, {user.full_name or 'friend'}!</b>\n{DIVIDER}\n"
        f"🆔 ID: <code>{user.telegram_id}</code>\n"
        f"💰 Balance: <b>{money(user.balance)}</b>\n"
        f"🏅 Tier: {tier_badge(user.total_spent)}\n"
        f"📦 Orders: {user.orders_count}\n{DIVIDER}\n"
        f"<i>Premium digital products, delivered instantly. ⚡</i>"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    async with SessionLocal() as s:
        user = await get_or_create_user(s, tg)
        args = context.args
        if args and args[0].startswith("ref_") and not user.referrer_id:
            ref = (await s.execute(select(User).where(
                User.referral_code == args[0][4:]))).scalar_one_or_none()
            if ref and ref.telegram_id != tg.id:
                user.referrer_id = ref.telegram_id
                try:
                    await context.bot.send_message(
                        ref.telegram_id,
                        f"🎉 <b>New referral!</b> {tg.full_name} joined via your link.\n"
                        f"You earn {REFERRAL_PERCENT}% of every purchase they make.",
                        parse_mode="HTML")
                except Exception:
                    pass
        if user.is_banned:
            await update.message.reply_text("🚫 Your account is suspended.")
            return
        await s.commit()
        text = _welcome_text(user)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu())


async def menu_home_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
    await safe_edit(q, _welcome_text(user), reply_markup=main_menu())


# ────────── Catalog ──────────

async def cat_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        cats = (await s.execute(
            select(Category).where(Category.is_active.is_(True),
                                   Category.parent_id.is_(None))
            .order_by(Category.sort_order))).scalars().all()
    if not cats:
        await safe_edit(q, header("🛍️ Catalog") + "No categories yet.",
                        reply_markup=kb([nav_row()]))
        return
    await safe_edit(q, header("🛍️ Browse Catalog") + "Choose a category:",
                    reply_markup=categories_menu(cats))


async def cat_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    cat_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    async with SessionLocal() as s:
        prods = (await s.execute(
            select(Product).where(Product.category_id == cat_id,
                                  Product.is_active.is_(True))
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1))).scalars().all()
        cat = await s.get(Category, cat_id)
    has_next = len(prods) > PAGE_SIZE
    prods = prods[:PAGE_SIZE]
    if not prods:
        await safe_edit(q, header(f"{cat.emoji} {cat.name}") + "🚫 No products here yet.",
                        reply_markup=kb([nav_row(back="cat:list")]))
        return
    await safe_edit(q, header(f"{cat.emoji} {cat.name}") +
                    "Select a product to view plans & live stock:",
                    reply_markup=products_menu(prods, cat_id, page, has_next))


async def prod_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prod_id = int(q.data.split(":")[2])
    async with SessionLocal() as s:
        p = await s.get(Product, prod_id)
        plans = (await s.execute(
            select(Plan).where(Plan.product_id == prod_id, Plan.is_active.is_(True)))
        ).scalars().all()
        stock_map = {pl.id: await count_stock(s, pl.id) for pl in plans}
    lines = [header(f"🛒 {p.title}")]
    if p.description:
        lines.append(p.description + f"\n{DIVIDER}")
    for pl in plans:
        st = stock_map[pl.id]
        badge = f"🟢 {st} in stock" if (st > 0 or not pl.auto_delivery) else "🔴 Out of Stock"
        price = money(pl.price)
        if pl.original_price and pl.original_price > pl.price:
            price = f"<s>{money(pl.original_price)}</s> <b>{price}</b> 🔥"
        if pl.flash_sale_ends and pl.flash_sale_ends > datetime.utcnow():
            left = pl.flash_sale_ends - datetime.utcnow()
            price += f"  ⏳ {left.seconds // 3600}h left"
        lines.append(f"💠 <b>{pl.name}</b> — {price}\n    {badge}")
    markup = product_card(prod_id, plans, back_to=f"cat:view:{p.category_id}")
    if p.media_file_id and p.media_type == "photo":
        await q.message.reply_photo(p.media_file_id, caption="\n".join(lines),
                                    parse_mode="HTML", reply_markup=markup)
        await q.message.delete()
    else:
        await safe_edit(q, "\n".join(lines), reply_markup=markup)


# ────────── Purchase flow ──────────

async def plan_buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _show_confirm(q, int(q.data.split(":")[2]), qty=1)


async def plan_qty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, _, plan_id, qty = q.data.split(":")
    await q.answer()
    await _show_confirm(q, int(plan_id), max(1, min(50, int(qty))))


async def _show_confirm(q, plan_id: int, qty: int):
    async with SessionLocal() as s:
        pl = await s.get(Plan, plan_id)
        p = await s.get(Product, pl.product_id)
        stock = await count_stock(s, plan_id)
        user = await get_or_create_user(s, q.from_user)
    if pl.auto_delivery and stock < qty:
        await q.answer(f"Only {stock} left in stock!", show_alert=True)
        return
    total = pl.price * qty
    bulk_note = ""
    if qty >= 5:
        total *= 0.9
        bulk_note = "\n🎁 <b>Bulk discount −10% applied!</b>"
    text = (header("🧾 Order Confirmation") +
            f"📦 Product: <b>{p.title}</b>\n"
            f"💠 Plan: {pl.name}\n"
            f"🔢 Quantity: {qty}\n"
            f"💰 Total: <b>{money(total)}</b>{bulk_note}\n{DIVIDER}\n"
            f"👛 Wallet balance: {money(user.balance)}\n"
            + ("✅ Sufficient balance — confirm to buy instantly."
               if user.balance >= total else
               f"⚠️ Insufficient balance. Top up {money(total - user.balance)} first."))
    await safe_edit(q, text, reply_markup=confirm_order(plan_id, qty))


async def plan_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Atomic checkout: stock reservation + wallet debit + referral in ONE txn."""
    q = update.callback_query
    await q.answer()
    if rate_limited(q.from_user.id):
        await q.answer("⏳ Slow down!", show_alert=True)
        return
    _, _, plan_id, qty = q.data.split(":")
    plan_id, qty = int(plan_id), int(qty)
    coupon = context.user_data.get("coupon")

    async with SessionLocal() as s:
        pl = await s.get(Plan, plan_id)
        p = await s.get(Product, pl.product_id)
        user = await get_or_create_user(s, q.from_user)
        if user.is_banned:
            await q.answer("🚫 Account suspended.", show_alert=True)
            return
        total = pl.price * qty * (0.9 if qty >= 5 else 1)
        if coupon:
            c = (await s.execute(select(Coupon).where(
                Coupon.code == coupon, Coupon.is_active.is_(True)))).scalar_one_or_none()
            if c and c.used_count < c.max_uses and (
                    not c.expires_at or c.expires_at > datetime.utcnow()):
                total -= total * c.percent / 100 + c.flat_amount
                c.used_count += 1
        total = max(total, 0)
        if user.balance < total:
            await safe_edit(q, header("💳 Top-up Required") +
                            f"You need {money(total - user.balance)} more.",
                            reply_markup=wallet_menu())
            return
        items = []
        if pl.auto_delivery:
            res = await s.execute(
                select(StockItem).where(StockItem.plan_id == plan_id,
                                        StockItem.is_sold.is_(False))
                .limit(qty).with_for_update())
            items = res.scalars().all()
            if len(items) < qty:
                await q.answer("🔴 Stock ran out!", show_alert=True)
                return
        user.balance -= total
        user.total_spent += total
        user.orders_count += 1
        p.sold_count += qty
        order = Order(user_id=user.id, plan_id=plan_id, quantity=qty, amount=total,
                      coupon_code=coupon,
                      status=OrderStatus.DELIVERED if pl.auto_delivery
                      else OrderStatus.PENDING_MANUAL)
        s.add(order)
        await s.flush()
        for it in items:
            it.is_sold = True
            it.order_id = order.id
        s.add(Transaction(user_id=user.id, type=TxnType.PURCHASE,
                          status=TxnStatus.COMPLETED, amount=-total,
                          method="wallet", reference=f"ORD{order.id}"))
        ref_msg = None
        if user.referrer_id:
            commission = round(total * REFERRAL_PERCENT / 100, 2)
            ref = (await s.execute(select(User).where(
                User.telegram_id == user.referrer_id))).scalar_one_or_none()
            if ref:
                ref.balance += commission
                ref.referral_earnings += commission
                s.add(Transaction(user_id=ref.id, type=TxnType.REFERRAL,
                                  status=TxnStatus.COMPLETED, amount=commission,
                                  method="referral", reference=f"ORD{order.id}"))
                ref_msg = (ref.telegram_id, commission)
        await s.commit()
        context.user_data.pop("coupon", None)

    lines = [header("✅ Order Complete") +
             f"🧾 Order ID: <code>#{order.id}</code>\n"
             f"📦 {p.title} — {pl.name} × {qty}\n"
             f"💰 Paid: {money(total)}\n{DIVIDER}\n"]
    if pl.auto_delivery:
        lines.append("🔑 <b>Your credentials / keys:</b>\n" +
                     "\n".join(f"<code>{it.content}</code>" for it in items))
    else:
        lines.append("⏳ <b>Manual delivery</b> — an admin will deliver shortly in this chat.")
    await safe_edit(q, "\n".join(lines), reply_markup=order_actions(order.id))
    if ref_msg:
        try:
            await context.bot.send_message(
                ref_msg[0],
                f"💸 Referral commission credited: <b>{money(ref_msg[1])}</b>",
                parse_mode="HTML")
        except Exception:
            pass


# ────────── Orders / receipts ──────────

async def orders_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[2])
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
        orders = (await s.execute(
            select(Order).where(Order.user_id == user.id)
            .order_by(Order.id.desc()).offset(page * 8).limit(8))).scalars().all()
        rows = []
        for o in orders:
            pl = await s.get(Plan, o.plan_id)
            p = await s.get(Product, pl.product_id)
            rows.append((o, p, pl))
    if not rows:
        await safe_edit(q, header("📦 My Orders") +
                        "No orders yet — go grab something! 🛍️",
                        reply_markup=kb([[Btn("🛍️ Browse Store", callback_data="cat:list")],
                                         nav_row()]))
        return
    icons = {OrderStatus.DELIVERED: "✅", OrderStatus.PENDING_MANUAL: "⏳",
             OrderStatus.CANCELLED: "❌"}
    text = header("📦 My Orders") + "\n".join(
        f"{icons[o.status]} <b>#{o.id}</b> {p.title} · {pl.name} × {o.quantity}"
        f" — {money(o.amount)}\n    <i>{o.created_at:%d %b %Y %H:%M}</i>"
        for o, p, pl in rows)
    await safe_edit(q, text, reply_markup=kb([
        [Btn("🔄 Refresh", callback_data=f"orders:list:{page}")], nav_row()]))


async def orders_receipt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    order_id = int(q.data.split(":")[2])
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
        o = await s.get(Order, order_id)
        if not o or o.user_id != user.id:
            await q.answer("Not found.", show_alert=True)
            return
        pl = await s.get(Plan, o.plan_id)
        p = await s.get(Product, pl.product_id)
    receipt = (f"DIGITAL STORE — RECEIPT\n{'='*32}\nOrder:   #{o.id}\n"
               f"Date:    {o.created_at:%Y-%m-%d %H:%M UTC}\nProduct: {p.title}\n"
               f"Plan:    {pl.name} x {o.quantity}\nAmount:  {o.amount:.2f}\n"
               f"Status:  {o.status.value}\n{'='*32}\nThank you for shopping!")
    await q.message.reply_document(io.BytesIO(receipt.encode()),
                                   filename=f"receipt_{o.id}.txt",
                                   caption="🧾 Your receipt")


# ────────── Wallet & top-up ──────────

async def wallet_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
    await safe_edit(q, header("💳 Wallet") +
                    f"Current balance: <b>{money(user.balance)}</b>\n{DIVIDER}\n"
                    "Choose a top-up method:", reply_markup=wallet_menu())


async def wallet_history_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
        txns = (await s.execute(
            select(Transaction).where(Transaction.user_id == user.id)
            .order_by(Transaction.id.desc()).limit(10))).scalars().all()
    icons = {"completed": "✅", "pending": "⏳", "rejected": "❌"}
    text = header("📜 Transactions") + ("\n".join(
        f"{icons[t.status.value]} {money(t.amount)} · {t.type.value} · <i>{t.method}</i>\n"
        f"    <i>{t.created_at:%d %b %H:%M}</i>" for t in txns) or "No transactions yet.")
    await safe_edit(q, text, reply_markup=kb([nav_row(back="wallet:menu")]))


async def pay_upi_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await safe_edit(q, header("📲 UPI Top-up") +
                    "Enter the amount you want to add (e.g. <code>500</code>):")
    return USR_TOPUP_AMOUNT


async def upi_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        assert 10 <= amount <= 100000
    except (ValueError, AssertionError):
        await update.message.reply_text("⚠️ Enter a valid amount between 10 and 100,000.")
        return USR_TOPUP_AMOUNT
    ref = make_reference()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, update.effective_user)
        txn = Transaction(user_id=user.id, type=TxnType.TOPUP, amount=amount,
                          method="upi", reference=ref)
        s.add(txn)
        await s.commit()
        txn_id = txn.id
    await update.message.reply_photo(
        io.BytesIO(upi_qr_png(amount, ref)),
        caption=(header("📲 Scan & Pay via UPI") +
                 f"💰 Amount: <b>{money(amount)}</b>\n"
                 f"🔖 Reference: <code>{ref}</code>\n{DIVIDER}\n"
                 "1️⃣ Scan QR with any UPI app\n"
                 f"2️⃣ Pay <b>exactly {money(amount)}</b>\n"
                 "3️⃣ Upload the payment screenshot below 📸\n\n"
                 "<i>⏱️ Verified within minutes, 24/7.</i>"),
        parse_mode="HTML", reply_markup=upi_payment(txn_id))
    return ConversationHandler.END


async def pay_shot_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["shot_txn"] = int(q.data.split(":")[2])
    await q.message.reply_text("📸 Send the payment <b>screenshot</b> now:",
                               parse_mode="HTML")
    return USR_SCREENSHOT


async def screenshot_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txn_id = context.user_data.get("shot_txn")
    if not txn_id or not update.message.photo:
        await update.message.reply_text("⚠️ Please send a photo.")
        return USR_SCREENSHOT
    file_id = update.message.photo[-1].file_id
    async with SessionLocal() as s:
        txn = await s.get(Transaction, txn_id)
        txn.screenshot_file_id = file_id
        await s.commit()
        amount, reference = txn.amount, txn.reference
    await update.message.reply_text(
        "✅ Screenshot received! Verification usually takes <10 min.")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                aid, file_id,
                caption=(f"💰 <b>Top-up request #{txn_id}</b>\n"
                         f"User: <code>{update.effective_user.id}</code> "
                         f"(@{update.effective_user.username})\n"
                         f"Amount: <b>{money(amount)}</b>\nRef: <code>{reference}</code>"),
                parse_mode="HTML", reply_markup=payment_review(txn_id))
        except Exception:
            pass
    return ConversationHandler.END


async def pay_status_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    async with SessionLocal() as s:
        txn = await s.get(Transaction, int(q.data.split(":")[2]))
    icons = {TxnStatus.PENDING: "⏳ Pending verification",
             TxnStatus.COMPLETED: "✅ Approved & credited!",
             TxnStatus.REJECTED: "❌ Rejected — contact support"}
    await q.answer(icons[txn.status], show_alert=True)


async def pay_crypto_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        url = await create_cryptobot_invoice(10.0, make_reference())
        await safe_edit(q, header("🪙 Crypto Top-up (USDT)") +
                        "Pay via CryptoBot — balance credits automatically:",
                        reply_markup=kb([[Btn("💠 Pay 10 USDT", url=url)],
                                         nav_row(back="wallet:menu")]))
    except GatewayError as e:
        await safe_edit(q, header("🪙 Crypto") + f"⚠️ Gateway unavailable: {e}",
                        reply_markup=kb([nav_row(back="wallet:menu")]))


async def pay_stripe_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Stripe checkout runs via the /webhooks/stripe endpoint — "
                   "configure PUBLIC_URL + keys first.", show_alert=True)


# ────────── Referrals / coupons / support / search / stats ──────────

async def ref_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    me = await context.bot.get_me()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
        refs = (await s.execute(select(User).where(
            User.referrer_id == user.telegram_id))).scalars().all()
    text = (header("👥 Refer & Earn") +
            f"🔗 Your link:\n<code>https://t.me/{me.username}?start=ref_{user.referral_code}</code>\n"
            f"{DIVIDER}\n👤 Referrals: <b>{len(refs)}</b>\n"
            f"💸 Total earned: <b>{money(user.referral_earnings)}</b>\n{DIVIDER}\n"
            f"<i>Earn {REFERRAL_PERCENT}% commission on every purchase your "
            "referrals make — forever.</i>")
    await safe_edit(q, text, reply_markup=refer_menu(me.username, user.referral_code))


async def coupon_redeem_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("🎟️ Send your coupon code:")
    return USR_COUPON


async def coupon_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip().upper()
    async with SessionLocal() as s:
        c = (await s.execute(select(Coupon).where(
            Coupon.code == code, Coupon.is_active.is_(True)))).scalar_one_or_none()
        valid = c and c.used_count < c.max_uses and (
            not c.expires_at or c.expires_at > datetime.utcnow())
        desc = (f"{c.percent:g}% off" if c and c.percent else
                f"{money(c.flat_amount)} off") if valid else ""
    if valid:
        context.user_data["coupon"] = code
        await update.message.reply_text(
            f"✅ Coupon applied: <b>{desc}</b> on your next order!", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Invalid or expired coupon.")
    return ConversationHandler.END


async def support_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
        open_t = (await s.execute(select(SupportTicket).where(
            SupportTicket.user_id == user.id,
            SupportTicket.status == TicketStatus.OPEN))).scalars().all()
    await safe_edit(q, header("💬 24/7 Support") +
                    "Average response: <b>under 15 min</b> ⚡\n"
                    f"Or DM us directly: {SUPPORT_USERNAME}",
                    reply_markup=support_menu(bool(open_t)))


async def support_new_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("📝 What's the subject of your issue?")
    return USR_TICKET_SUBJ


async def ticket_subj_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["ticket_subj"] = update.message.text.strip()[:120]
    await update.message.reply_text("📄 Describe the issue in detail:")
    return USR_TICKET_BODY


async def ticket_body_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subj = context.user_data.pop("ticket_subj", "No subject")
    async with SessionLocal() as s:
        user = await get_or_create_user(s, update.effective_user)
        t = SupportTicket(user_id=user.id, subject=subj)
        s.add(t)
        await s.commit()
        tid = t.id
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                aid, f"🎫 <b>Ticket #{tid}</b> from <code>{user.telegram_id}</code>\n"
                     f"<b>{subj}</b>\n{update.message.text}", parse_mode="HTML")
        except Exception:
            pass
    await update.message.reply_text(
        f"✅ Ticket <b>#{tid}</b> opened. We'll reply here shortly!", parse_mode="HTML")
    return ConversationHandler.END


async def search_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "🔍 Type a keyword (e.g. <i>Netflix</i>, <i>Gemini</i>, <i>VPN</i>):",
        parse_mode="HTML")
    return USR_SEARCH


async def search_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    term = f"%{update.message.text.strip()}%"
    async with SessionLocal() as s:
        prods = (await s.execute(
            select(Product).where(Product.is_active.is_(True),
                                  or_(Product.title.ilike(term),
                                      Product.description.ilike(term)))
            .limit(10))).scalars().all()
    if not prods:
        await update.message.reply_text("🔍 No matches. Try another keyword.",
                                        reply_markup=kb([nav_row()]))
    else:
        rows = [[Btn(f"🛒 {p.title}", callback_data=f"prod:view:{p.id}")] for p in prods]
        rows.append(nav_row())
        await update.message.reply_text(header(f"🔍 Results: {len(prods)}"),
                                        reply_markup=kb(rows))
    return ConversationHandler.END


async def stats_me_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        user = await get_or_create_user(s, q.from_user)
    await safe_edit(q, header("📊 My Stats") +
                    f"🏅 Tier: {tier_badge(user.total_spent)}\n"
                    f"💰 Balance: {money(user.balance)}\n"
                    f"🛒 Orders: {user.orders_count}\n"
                    f"💸 Total spent: {money(user.total_spent)}\n"
                    f"👥 Referral earnings: {money(user.referral_earnings)}\n"
                    f"📅 Member since: {user.created_at:%d %b %Y}",
                    reply_markup=kb([nav_row()]))


async def noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ══════════════════════ 7. ADMIN HANDLERS (/admin) ══════════════════════

@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with SessionLocal() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar_one()
        rev = (await s.execute(select(func.coalesce(func.sum(Order.amount), 0)))).scalar_one()
    text = (header("🛠️ Admin Control Panel") +
            f"👥 Users: <b>{users}</b>\n💰 Lifetime revenue: <b>{money(rev)}</b>\n{DIVIDER}")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=admin_menu())


@admin_only
async def adm_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, header("🛠️ Admin Control Panel"), reply_markup=admin_menu())


# ────────── Products CRUD ──────────

@admin_only
async def adm_products_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        prods = (await s.execute(select(Product).order_by(Product.id.desc())
                                 .limit(15))).scalars().all()
    rows = []
    for p in prods:
        rows.append([Btn(f"{'✅' if p.is_active else '🙈'} {p.title}",
                         callback_data=f"adm:prod:{p.id}")])
    rows.append([Btn("➕ Add New Product", callback_data="adm:addprod")])
    rows.append(nav_row(back="adm:menu"))
    await safe_edit(q, header("📦 Product Manager") +
                    (f"Showing {len(prods)} latest." if prods else "No products yet."),
                    reply_markup=kb(rows))


@admin_only
async def adm_prod_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    pid = int(parts[2])
    action = parts[3] if len(parts) > 3 else None
    async with SessionLocal() as s:
        p = await s.get(Product, pid)
        if action == "del":
            await s.delete(p)
            await s.commit()
            await q.answer("🗑️ Deleted", show_alert=True)
            await adm_products_cb(update, context)
            return
        if action == "toggle":
            p.is_active = not p.is_active
            await s.commit()
            await q.answer("👁️ Visible" if p.is_active else "🙈 Hidden")
        plans = (await s.execute(select(Plan).where(Plan.product_id == pid))).scalars().all()
    text = header(f"✏️ {p.title}") + ("\n".join(
        f"💠 #{pl.id} {pl.name} — {money(pl.price)} "
        f"({'⚡auto' if pl.auto_delivery else '👤manual'})"
        f"{' 🚫' if not pl.is_active else ''}" for pl in plans) or "No plans yet.")
    await safe_edit(q, text, reply_markup=kb([
        [Btn("➕ Add Plan", callback_data=f"adm:prod:{pid}:addplan")],
        [Btn("👁️ Toggle", callback_data=f"adm:prod:{pid}:toggle"),
         Btn("🗑️ Delete", callback_data=f"adm:prod:{pid}:del")],
        nav_row(back="adm:products")]))


# ────────── Add Product wizard (FSM) ──────────

@admin_only
async def adm_addprod_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("📝 <b>Step 1/4:</b> Product title?", parse_mode="HTML")
    return ADM_PROD_TITLE


async def prod_title_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["np_title"] = update.message.text.strip()
    async with SessionLocal() as s:
        cats = (await s.execute(select(Category).where(
            Category.is_active.is_(True)))).scalars().all()
    rows = [[Btn(f"{c.emoji} {c.name}", callback_data=f"np:cat:{c.id}")] for c in cats]
    await update.message.reply_text("🗂️ <b>Step 2/4:</b> Pick a category:",
                                    parse_mode="HTML", reply_markup=kb(rows))
    return ADM_PROD_CATEGORY


async def prod_cat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["np_cat"] = int(q.data.split(":")[2])
    await q.message.reply_text(
        "📄 <b>Step 3/4:</b> Description (features, warranty, terms):", parse_mode="HTML")
    return ADM_PROD_DESC


async def prod_desc_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["np_desc"] = update.message.text.strip()
    await update.message.reply_text(
        "🖼️ <b>Step 4/4:</b> Send a banner photo/video, or /skip:", parse_mode="HTML")
    return ADM_PROD_MEDIA


async def prod_media_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    m = update.message
    if m.photo:
        context.user_data["np_media"] = (m.photo[-1].file_id, "photo")
    elif m.video:
        context.user_data["np_media"] = (m.video.file_id, "video")
    elif m.animation:
        context.user_data["np_media"] = (m.animation.file_id, "animation")
    else:
        context.user_data["np_media"] = (None, None)
    return await _finish_product(update, context)


async def prod_media_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["np_media"] = (None, None)
    return await _finish_product(update, context)


async def _finish_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    media_id, media_type = ud.pop("np_media")
    async with SessionLocal() as s:
        p = Product(title=ud.pop("np_title"), category_id=ud.pop("np_cat"),
                    description=ud.pop("np_desc"),
                    media_file_id=media_id, media_type=media_type)
        s.add(p)
        await s.commit()
        pid = p.id
    await update.message.reply_text(
        f"✅ Product <b>#{pid}</b> created!\nNow add a pricing plan:",
        parse_mode="HTML",
        reply_markup=kb([[Btn("➕ Add Plan", callback_data=f"adm:prod:{pid}:addplan")],
                         [Btn("🛠️ Admin Menu", callback_data="adm:menu")]]))
    return ConversationHandler.END


# ────────── Add Plan wizard ──────────

@admin_only
async def adm_addplan_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["pl_prod"] = int(q.data.split(":")[2])
    await q.message.reply_text("💠 Plan name? (e.g. <i>1 Month</i>, <i>Lifetime</i>)",
                               parse_mode="HTML")
    return ADM_PLAN_NAME


async def plan_name_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["pl_name"] = update.message.text.strip()
    await update.message.reply_text("💰 Selling price? (numbers only)")
    return ADM_PLAN_PRICE


async def plan_price_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["pl_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Numbers only, try again:")
        return ADM_PLAN_PRICE
    await update.message.reply_text("🏷️ Original price for strike-through? (or /skip)")
    return ADM_PLAN_ORIG


async def plan_orig_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["pl_orig"] = float(update.message.text.strip())
    except ValueError:
        context.user_data["pl_orig"] = None
    return await _plan_delivery_q(update, context)


async def plan_orig_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["pl_orig"] = None
    return await _plan_delivery_q(update, context)


async def _plan_delivery_q(update, context) -> int:
    await update.message.reply_text("🚚 Delivery mode?", reply_markup=kb([[
        Btn("⚡ Auto (key pool)", callback_data="np:auto:1"),
        Btn("👤 Manual admin", callback_data="np:auto:0")]]))
    return ADM_PLAN_AUTO


async def plan_auto_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ud = context.user_data
    auto = q.data.endswith(":1")
    async with SessionLocal() as s:
        pl = Plan(product_id=ud.pop("pl_prod"), name=ud.pop("pl_name"),
                  price=ud.pop("pl_price"), original_price=ud.pop("pl_orig"),
                  auto_delivery=auto)
        s.add(pl)
        await s.commit()
        pl_id = pl.id
    await q.message.reply_text(
        f"✅ Plan <b>#{pl_id}</b> created!" + ("\n📥 Import stock keys next:" if auto else ""),
        parse_mode="HTML",
        reply_markup=kb(([Btn("📥 Import Stock", callback_data="adm:import")] if auto else [])
                        + [[Btn("🛠️ Admin Menu", callback_data="adm:menu")]]))
    return ConversationHandler.END


# ────────── Categories ──────────

@admin_only
async def adm_categories_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        cats = (await s.execute(select(Category).order_by(Category.sort_order))).scalars().all()
    rows = [[Btn(f"{c.emoji} {c.name}", callback_data=f"adm:cat:{c.id}")] for c in cats]
    rows.append([Btn("➕ Add Category", callback_data="adm:addcat")])
    rows.append(nav_row(back="adm:menu"))
    await safe_edit(q, header("🗂️ Categories"), reply_markup=kb(rows))


@admin_only
async def adm_addcat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Send category as: <code>Emoji Name</code> "
                               "(e.g. <code>🎬 OTT</code>)", parse_mode="HTML")
    return ADM_COUPON  # reuse a plain-text state


async def addcat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.strip().split(maxsplit=1)
    emoji = parts[0] if len(parts) > 1 else "📦"
    name = parts[-1]
    async with SessionLocal() as s:
        s.add(Category(name=name, emoji=emoji))
        await s.commit()
    await update.message.reply_text(f"✅ Category <b>{emoji} {name}</b> added.",
                                    parse_mode="HTML",
                                    reply_markup=kb([[Btn("🛠️ Admin Menu",
                                                          callback_data="adm:menu")]]))
    return ConversationHandler.END


# ────────── Bulk stock import (.txt / .csv) ──────────

@admin_only
async def adm_import_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        plans = (await s.execute(
            select(Plan, Product).join(Product, Plan.product_id == Product.id)
            .where(Plan.auto_delivery.is_(True)))).all()
    rows = [[Btn(f"{p.title} · {pl.name}", callback_data=f"imp:plan:{pl.id}")]
            for pl, p in plans]
    rows.append(nav_row(back="adm:menu"))
    await safe_edit(q, header("📥 Bulk Stock Import") +
                    "Select the plan to stock, then send a <b>.txt/.csv</b> file "
                    "(one key/account per line):", reply_markup=kb(rows))
    return ADM_IMPORT_FILE


async def import_plan_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["imp_plan"] = int(q.data.split(":")[2])
    await q.message.reply_text("📄 Now send the .txt/.csv file:")
    return ADM_IMPORT_FILE


async def import_file_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plan_id = context.user_data.get("imp_plan")
    doc = update.message.document
    if not plan_id or not doc:
        await update.message.reply_text("⚠️ Send a file after choosing a plan.")
        return ADM_IMPORT_FILE
    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    lines = [l.strip() for l in raw.decode("utf-8", "ignore").splitlines() if l.strip()]
    async with SessionLocal() as s:
        s.add_all([StockItem(plan_id=plan_id, content=l) for l in lines])
        await s.commit()
    await update.message.reply_text(
        f"✅ Imported <b>{len(lines)}</b> stock items into plan #{plan_id}.",
        parse_mode="HTML",
        reply_markup=kb([[Btn("🛠️ Admin Menu", callback_data="adm:menu")]]))
    return ConversationHandler.END


# ────────── Payments approval queue ──────────

@admin_only
async def adm_payments_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        txns = (await s.execute(
            select(Transaction).where(Transaction.status == TxnStatus.PENDING)
            .order_by(Transaction.id.desc()).limit(10))).scalars().all()
    if not txns:
        await safe_edit(q, header("💰 Payments Queue") + "🎉 Queue is empty!",
                        reply_markup=kb([nav_row(back="adm:menu")]))
        return
    text = header("💰 Pending Top-ups")
    rows = []
    for t in txns:
        text += f"• #{t.id} — {money(t.amount)} via {t.method} · <code>{t.reference}</code>\n"
        rows.append([Btn(f"✅ #{t.id}", callback_data=f"adm:payok:{t.id}"),
                     Btn(f"❌ #{t.id}", callback_data=f"adm:payno:{t.id}"),
                     Btn("📸", callback_data=f"adm:shot:{t.id}")])
    rows.append(nav_row(back="adm:menu"))
    await safe_edit(q, text, reply_markup=kb(rows))


@admin_only
async def adm_pay_decide_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, action, txn_id = q.data.split(":")
    async with SessionLocal() as s:
        t = await s.get(Transaction, int(txn_id))
        if t.status != TxnStatus.PENDING:
            await q.answer("Already processed.", show_alert=True)
            return
        user = await s.get(User, t.user_id)
        if action == "payok":
            t.status = TxnStatus.COMPLETED
            user.balance += t.amount
            msg_user = (f"✅ Your top-up of <b>{money(t.amount)}</b> was approved! "
                        f"New balance: {money(user.balance)}")
        else:
            t.status = TxnStatus.REJECTED
            msg_user = (f"❌ Your top-up of <b>{money(t.amount)}</b> was rejected. "
                        "Contact support.")
        await s.commit()
        tg_id = user.telegram_id
    await q.answer("Done")
    try:
        await q.edit_message_caption(
            caption=(q.message.caption or "") +
            f"\n\n{'✅ APPROVED' if action == 'payok' else '❌ REJECTED'}",
            parse_mode="HTML")
    except Exception:
        pass
    try:
        await context.bot.send_message(tg_id, msg_user, parse_mode="HTML")
    except Exception:
        pass


@admin_only
async def adm_shot_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    async with SessionLocal() as s:
        t = await s.get(Transaction, int(q.data.split(":")[2]))
    if t and t.screenshot_file_id:
        await q.message.reply_photo(t.screenshot_file_id,
                                    caption=f"📸 Screenshot for txn #{t.id}",
                                    reply_markup=payment_review(t.id))
    await q.answer()


# ────────── Users management ──────────

@admin_only
async def adm_users_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("👤 Send a <b>User ID</b> or <b>@username</b> to manage:",
                               parse_mode="HTML")
    return ADM_USER_SEARCH


async def user_search_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.message.text.strip().lstrip("@")
    async with SessionLocal() as s:
        if q.isdigit():
            res = await s.execute(select(User).where(User.telegram_id == int(q)))
        else:
            res = await s.execute(select(User).where(User.username == q))
        user = res.scalar_one_or_none()
        if not user:
            await update.message.reply_text("🚫 User not found.")
            return ConversationHandler.END
        text = (header("👤 User Profile") +
                f"🆔 <code>{user.telegram_id}</code> · @{user.username or '—'}\n"
                f"💰 Balance: {money(user.balance)}\n"
                f"🛒 Orders: {user.orders_count} · Spent: {money(user.total_spent)}\n"
                f"👥 Referrals earned: {money(user.referral_earnings)}\n"
                f"Status: {'🚫 BANNED' if user.is_banned else '✅ Active'}")
        markup = user_manage(user.id, user.is_banned)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return ConversationHandler.END


@admin_only
async def adm_balance_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    _, action, uid = q.data.split(":")
    context.user_data["bal_action"] = (action, int(uid))
    await q.message.reply_text(
        f"💰 Amount to {'add' if action == 'baladd' else 'deduct'}?")
    return ADM_BALANCE


async def balance_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action, uid = context.user_data.pop("bal_action")
    try:
        amount = abs(float(update.message.text.strip()))
    except ValueError:
        await update.message.reply_text("⚠️ Numbers only.")
        return ADM_BALANCE
    delta = amount if action == "baladd" else -amount
    async with SessionLocal() as s:
        user = await s.get(User, uid)
        user.balance = max(0.0, user.balance + delta)
        s.add(Transaction(user_id=user.id, type=TxnType.ADMIN_ADJUST,
                          status=TxnStatus.COMPLETED, amount=delta,
                          method="admin", reference=f"ADM{update.effective_user.id}"
                                                   f"{secrets.token_hex(2)}"))
        await s.commit()
        new_bal, tg_id = user.balance, user.telegram_id
    await update.message.reply_text(f"✅ Done. New balance: {money(new_bal)}")
    try:
        await context.bot.send_message(
            tg_id, f"💰 Balance {'credited' if delta > 0 else 'adjusted'}: "
                   f"<b>{money(abs(delta))}</b>\nNew balance: {money(new_bal)}",
            parse_mode="HTML")
    except Exception:
        pass
    return ConversationHandler.END


@admin_only
async def adm_ban_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, _, uid, flag = q.data.split(":")
    async with SessionLocal() as s:
        user = await s.get(User, int(uid))
        user.is_banned = flag == "1"
        await s.commit()
    await q.answer("Updated ✅", show_alert=True)


# ────────── Broadcast engine ──────────

@admin_only
async def adm_broadcast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, header("📢 Broadcast Center") + "Choose the target audience:",
                    reply_markup=broadcast_targets())


@admin_only
async def adm_bc_target_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["bc_audience"] = q.data.split(":")[2]
    await q.message.reply_text(
        "📨 Send the broadcast now — text, photo, video or GIF with caption.\n"
        "Inline URL buttons: add lines like <code>[Text](https://url)</code> "
        "in the caption.", parse_mode="HTML")
    return ADM_BROADCAST


async def broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    audience = context.user_data.pop("bc_audience", "all")
    m = update.message
    async with SessionLocal() as s:
        q_ = select(User.telegram_id).where(User.is_banned.is_(False))
        if audience == "balance":
            q_ = q_.where(User.balance > 0)
        elif audience == "buyers":
            q_ = q_.where(User.orders_count > 0)
        tg_ids = [r[0] for r in (await s.execute(q_)).all()]
        s.add(BroadcastLog(admin_id=update.effective_user.id, audience=audience))
        await s.commit()

    caption = m.caption or m.text or ""
    buttons = [[Btn(label, url=url)] for label, url in
               re.findall(r"\[(.+?)\]\((https?://.+?)\)", caption)]
    clean = re.sub(r"\[(.+?)\]\((https?://.+?)\)", "", caption).strip()
    markup = kb(buttons) if buttons else None

    sent = failed = 0
    status = await m.reply_text(f"📢 Broadcasting to {len(tg_ids)} users…")
    for i, tid in enumerate(tg_ids):
        try:
            if m.photo:
                await context.bot.send_photo(tid, m.photo[-1].file_id, caption=clean,
                                             parse_mode="HTML", reply_markup=markup)
            elif m.video:
                await context.bot.send_video(tid, m.video.file_id, caption=clean,
                                             parse_mode="HTML", reply_markup=markup)
            elif m.animation:
                await context.bot.send_animation(tid, m.animation.file_id, caption=clean,
                                                 parse_mode="HTML", reply_markup=markup)
            else:
                await context.bot.send_message(tid, clean, parse_mode="HTML",
                                               reply_markup=markup)
            sent += 1
        except Exception:
            failed += 1
        if i % 25 == 24:
            await asyncio.sleep(1)  # flood-limit safe
    async with SessionLocal() as s:
        last = (await s.execute(select(BroadcastLog)
                                .order_by(BroadcastLog.id.desc()).limit(1))).scalar_one()
        last.sent_count, last.failed_count = sent, failed
        await s.commit()
    await status.edit_text(
        f"✅ Broadcast done — sent: <b>{sent}</b>, failed: {failed}", parse_mode="HTML")
    return ConversationHandler.END


# ────────── Coupons / flash sale / analytics / tickets ──────────

@admin_only
async def adm_coupons_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "🎟️ Create coupon:\n<code>CODE PERCENT FLAT MAX_USES</code>\n"
        "Example: <code>DIWALI 10 0 200</code> = 10% off, 200 uses", parse_mode="HTML")
    return ADM_COUPON


async def coupon_create_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        code, pct, flat, mx = update.message.text.split()
        pct, flat, mx = float(pct), float(flat), int(mx)
    except ValueError:
        await update.message.reply_text("⚠️ Format: CODE PERCENT FLAT MAX_USES")
        return ADM_COUPON
    async with SessionLocal() as s:
        s.add(Coupon(code=code.upper(), percent=pct, flat_amount=flat, max_uses=mx))
        await s.commit()
    await update.message.reply_text(f"✅ Coupon <b>{code.upper()}</b> live!",
                                    parse_mode="HTML")
    return ConversationHandler.END


@admin_only
async def adm_flash_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "⚡ Schedule flash sale:\n<code>PLAN_ID NEW_PRICE HOURS</code>\n"
        "Example: <code>12 99 24</code>", parse_mode="HTML")
    return ADM_FLASH


async def flash_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        pid, price, hours = update.message.text.split()
        pid, price, hours = int(pid), float(price), int(hours)
    except ValueError:
        await update.message.reply_text("⚠️ Format: PLAN_ID NEW_PRICE HOURS")
        return ADM_FLASH
    async with SessionLocal() as s:
        pl = await s.get(Plan, pid)
        pl.original_price = pl.original_price or pl.price
        pl.price = price
        pl.flash_sale_ends = datetime.utcnow() + timedelta(hours=hours)
        await s.commit()
    await update.message.reply_text(
        f"⚡ Flash sale live on plan #{pid}: <b>{money(price)}</b> for {hours}h! "
        "Tip: broadcast it 📢", parse_mode="HTML")
    return ConversationHandler.END


@admin_only
async def adm_analytics_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    now = datetime.utcnow()
    async with SessionLocal() as s:
        total_users = (await s.execute(select(func.count(User.id)))).scalar_one()
        day_users = (await s.execute(select(func.count(User.id)).where(
            User.created_at >= now - timedelta(days=1)))).scalar_one()
        rev_total = (await s.execute(
            select(func.coalesce(func.sum(Order.amount), 0)))).scalar_one()
        rev_week = (await s.execute(select(func.coalesce(func.sum(Order.amount), 0))
                                    .where(Order.created_at >= now - timedelta(days=7)))
                    ).scalar_one()
        rev_month = (await s.execute(select(func.coalesce(func.sum(Order.amount), 0))
                                     .where(Order.created_at >= now - timedelta(days=30)))
                     ).scalar_one()
        top = (await s.execute(select(Product.title, Product.sold_count)
                               .order_by(Product.sold_count.desc()).limit(5))).all()
        open_t = (await s.execute(select(func.count(SupportTicket.id)).where(
            SupportTicket.status == TicketStatus.OPEN))).scalar_one()
    text = (header("📊 Analytics Dashboard") +
            f"👥 Users: <b>{total_users}</b> (+{day_users} today)\n"
            f"💰 Revenue — 7d: <b>{money(rev_week)}</b> · 30d: <b>{money(rev_month)}</b>"
            f" · all: <b>{money(rev_total)}</b>\n"
            f"🎫 Open tickets: <b>{open_t}</b>\n{DIVIDER}\n"
            "🏆 <b>Top sellers:</b>\n" +
            ("\n".join(f"{i+1}. {t} — {c} sold" for i, (t, c) in enumerate(top))
             or "No sales yet."))
    await safe_edit(q, text, reply_markup=kb([
        [Btn("🔄 Refresh", callback_data="adm:analytics")],
        nav_row(back="adm:menu")]))


@admin_only
async def adm_tickets_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    async with SessionLocal() as s:
        tickets = (await s.execute(
            select(SupportTicket, User).join(User, SupportTicket.user_id == User.id)
            .where(SupportTicket.status == TicketStatus.OPEN)
            .order_by(SupportTicket.id.desc()).limit(10))).all()
    if not tickets:
        await safe_edit(q, header("🎫 Tickets") + "🎉 No open tickets!",
                        reply_markup=kb([nav_row(back="adm:menu")]))
        return
    text = header("🎫 Open Tickets")
    rows = []
    for t, u in tickets:
        text += f"• #{t.id} — {t.subject} (<code>{u.telegram_id}</code>)\n"
        rows.append([Btn(f"✅ Close #{t.id}", callback_data=f"adm:tclose:{t.id}"),
                     Btn("💬 Reply", url=f"tg://user?id={u.telegram_id}")])
    rows.append(nav_row(back="adm:menu"))
    await safe_edit(q, text, reply_markup=kb(rows))


@admin_only
async def adm_tclose_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    async with SessionLocal() as s:
        t = await s.get(SupportTicket, int(q.data.split(":")[2]))
        t.status = TicketStatus.CLOSED
        await s.commit()
    await q.answer("Closed ✅")
    await adm_tickets_cb(update, context)


# ══════════════════════ 8. FASTAPI PAYMENT WEBHOOKS ══════════════════════

async def _credit_topup(reference: str, bot) -> bool:
    async with SessionLocal() as s:
        t = (await s.execute(select(Transaction).where(
            Transaction.reference == reference))).scalar_one_or_none()
        if not t or t.status != TxnStatus.PENDING:
            return False
        user = await s.get(User, t.user_id)
        t.status = TxnStatus.COMPLETED
        user.balance += t.amount
        await s.commit()
        tg_id, amount, bal = user.telegram_id, t.amount, user.balance
    try:
        await bot.send_message(
            tg_id, f"✅ Payment confirmed! <b>{money(amount)}</b> added.\n"
                   f"New balance: {money(bal)}", parse_mode="HTML")
    except Exception as e:
        log.warning("notify failed: %s", e)
    return True


def build_fastapi_app(ptb_app: Application) -> FastAPI:
    api = FastAPI(title="Store Bot Webhooks")

    @api.get("/health")
    async def health():
        return {"ok": True}

    @api.post("/webhooks/cryptobot")
    async def cryptobot_hook(request: Request):
        data = await request.json()
        if data.get("update_type") == "invoice_paid":
            payload = data["payload"].get("payload")  # our internal reference
            if payload:
                await _credit_topup(payload, ptb_app.bot)
        return {"ok": True}

    @api.post("/webhooks/stripe")
    async def stripe_hook(request: Request):
        body = await request.body()
        sig = request.headers.get("stripe-signature", "")
        event = verify_stripe_signature(body, sig)
        if event and event.get("type") == "checkout.session.completed":
            ref = event["data"]["object"].get("client_reference_id")
            if ref:
                await _credit_topup(ref, ptb_app.bot)
        return {"ok": True}

    return api


# ══════════════════════ 9. APP WIRING & ENTRYPOINT ══════════════════════

PHOTO_VIDEO = filters.PHOTO | filters.VIDEO | filters.ANIMATION


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("menu", cmd_start))

    # user callbacks
    app.add_handler(CallbackQueryHandler(menu_home_cb, pattern=r"^menu:home$"))
    app.add_handler(CallbackQueryHandler(cat_list_cb, pattern=r"^cat:list$"))
    app.add_handler(CallbackQueryHandler(cat_view_cb, pattern=r"^cat:view:\d+(:\d+)?$"))
    app.add_handler(CallbackQueryHandler(prod_view_cb, pattern=r"^prod:view:\d+$"))
    app.add_handler(CallbackQueryHandler(plan_buy_cb, pattern=r"^plan:buy:\d+$"))
    app.add_handler(CallbackQueryHandler(plan_qty_cb, pattern=r"^plan:qty:\d+:-?\d+$"))
    app.add_handler(CallbackQueryHandler(plan_confirm_cb, pattern=r"^plan:confirm:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(orders_list_cb, pattern=r"^orders:list:\d+$"))
    app.add_handler(CallbackQueryHandler(orders_receipt_cb, pattern=r"^orders:receipt:\d+$"))
    app.add_handler(CallbackQueryHandler(wallet_menu_cb, pattern=r"^wallet:menu$"))
    app.add_handler(CallbackQueryHandler(wallet_history_cb, pattern=r"^wallet:history:\d+$"))
    app.add_handler(CallbackQueryHandler(pay_crypto_cb, pattern=r"^pay:crypto$"))
    app.add_handler(CallbackQueryHandler(pay_stripe_cb, pattern=r"^pay:stripe$"))
    app.add_handler(CallbackQueryHandler(pay_status_cb, pattern=r"^pay:status:\d+$"))
    app.add_handler(CallbackQueryHandler(ref_menu_cb, pattern=r"^ref:menu$"))
    app.add_handler(CallbackQueryHandler(support_menu_cb, pattern=r"^support:menu$"))
    app.add_handler(CallbackQueryHandler(stats_me_cb, pattern=r"^stats:me$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))

    # admin callbacks
    app.add_handler(CallbackQueryHandler(adm_menu_cb, pattern=r"^adm:menu$"))
    app.add_handler(CallbackQueryHandler(adm_products_cb, pattern=r"^adm:products$"))
    app.add_handler(CallbackQueryHandler(adm_categories_cb, pattern=r"^adm:categories$"))
    app.add_handler(CallbackQueryHandler(adm_prod_cb, pattern=r"^adm:prod:\d+(:(del|toggle))?$"))
    app.add_handler(CallbackQueryHandler(adm_payments_cb, pattern=r"^adm:payments:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_pay_decide_cb, pattern=r"^adm:pay(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(adm_shot_cb, pattern=r"^adm:shot:\d+$"))
    app.add_handler(CallbackQueryHandler(adm_ban_cb, pattern=r"^adm:ban:\d+:[01]$"))
    app.add_handler(CallbackQueryHandler(adm_broadcast_cb, pattern=r"^adm:broadcast$"))
    app.add_handler(CallbackQueryHandler(adm_analytics_cb, pattern=r"^adm:analytics$"))
    app.add_handler(CallbackQueryHandler(adm_tickets_cb, pattern=r"^adm:tickets$"))
    app.add_handler(CallbackQueryHandler(adm_tclose_cb, pattern=r"^adm:tclose:\d+$"))

    fallbacks = [CommandHandler("cancel", cancel_conversation)]

    # FSM: add product wizard
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_addprod_cb, pattern=r"^adm:addprod$")],
        states={
            ADM_PROD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, prod_title_msg)],
            ADM_PROD_CATEGORY: [CallbackQueryHandler(prod_cat_cb, pattern=r"^np:cat:\d+$")],
            ADM_PROD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, prod_desc_msg)],
            ADM_PROD_MEDIA: [MessageHandler(PHOTO_VIDEO, prod_media_msg),
                             CommandHandler("skip", prod_media_skip)],
        }, fallbacks=fallbacks))

    # FSM: add plan wizard
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_addplan_cb, pattern=r"^adm:prod:\d+:addplan$")],
        states={
            ADM_PLAN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_name_msg)],
            ADM_PLAN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_price_msg)],
            ADM_PLAN_ORIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_orig_msg),
                            CommandHandler("skip", plan_orig_skip)],
            ADM_PLAN_AUTO: [CallbackQueryHandler(plan_auto_cb, pattern=r"^np:auto:[01]$")],
        }, fallbacks=fallbacks))

    # FSM: add category
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_addcat_cb, pattern=r"^adm:addcat$")],
        states={ADM_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcat_msg)]},
        fallbacks=fallbacks))

    # FSM: bulk stock import
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_import_cb, pattern=r"^adm:import$")],
        states={ADM_IMPORT_FILE: [
            CallbackQueryHandler(import_plan_cb, pattern=r"^imp:plan:\d+$"),
            MessageHandler(filters.Document.ALL, import_file_msg)]},
        fallbacks=fallbacks))

    # FSM: user management
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_users_cb, pattern=r"^adm:users$")],
        states={ADM_USER_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                                 user_search_msg)]},
        fallbacks=fallbacks))

    # FSM: balance adjust
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_balance_cb, pattern=r"^adm:bal(add|sub):\d+$")],
        states={ADM_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_msg)]},
        fallbacks=fallbacks))

    # FSM: broadcast
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_bc_target_cb,
                                           pattern=r"^adm:bc:(all|balance|buyers)$")],
        states={ADM_BROADCAST: [MessageHandler(~filters.COMMAND, broadcast_msg)]},
        fallbacks=fallbacks))

    # FSM: create coupon
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_coupons_cb, pattern=r"^adm:coupons$")],
        states={ADM_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                            coupon_create_msg)]},
        fallbacks=fallbacks))

    # FSM: flash sale
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_flash_cb, pattern=r"^adm:flash$")],
        states={ADM_FLASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, flash_msg)]},
        fallbacks=fallbacks))

    # FSM: UPI top-up amount
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(pay_upi_cb, pattern=r"^pay:upi$")],
        states={USR_TOPUP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                                  upi_amount_msg)]},
        fallbacks=fallbacks))

    # FSM: payment screenshot
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(pay_shot_cb, pattern=r"^pay:shot:\d+$")],
        states={USR_SCREENSHOT: [MessageHandler(filters.PHOTO, screenshot_msg)]},
        fallbacks=fallbacks))

    # FSM: redeem coupon
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(coupon_redeem_cb, pattern=r"^coupon:redeem$")],
        states={USR_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_msg)]},
        fallbacks=fallbacks))

    # FSM: support ticket
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(support_new_cb, pattern=r"^support:new$")],
        states={
            USR_TICKET_SUBJ: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                             ticket_subj_msg)],
            USR_TICKET_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                             ticket_body_msg)],
        }, fallbacks=fallbacks))

    # FSM: search
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(search_start_cb, pattern=r"^search:start$")],
        states={USR_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_msg)]},
        fallbacks=fallbacks))

    return app


async def serve_webhooks(ptb_app: Application) -> None:
    """Run FastAPI (payment webhooks) next to the bot in the same event loop."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        build_fastapi_app(ptb_app), host=WEBHOOK_HOST, port=WEBHOOK_PORT,
        log_level="warning"))
    await server.serve()


async def amain() -> None:
    await init_db()
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("✅ Bot polling started — send /start to your bot!")
    try:
        await serve_webhooks(app)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(amain())
