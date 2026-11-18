"""APON Vault style file-store bot + Telegram Mini App.  Phase 1.

Run:  uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, MenuButtonWebApp, Message, PreCheckoutQuery,
                           WebAppInfo)
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)

# ───────────────────────── config ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "vault.db")          # on Railway: point this at a Volume, e.g. /data/vault.db
BRAND = os.getenv("BRAND", "APON Vault")
BANNER = os.getenv("BANNER", "")                    # image URL or Telegram file_id for the /start banner
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
UPLOADER = os.getenv("UPLOADER_NAME", "Admin")

REF_BONUS, DAILY, VIP_DAILY, VIP_DISCOUNT = 5, 3, 5, 20
MILESTONES = {5: 10, 10: 25, 25: 75, 50: 150, 100: 400, 250: 1000}   # referrals -> bonus credits
STREAK_BONUS = {3: 5, 7: 10, 14: 20, 30: 50}                          # streak days -> bonus credits
PACKS = {"p100": (100, 0, 154), "p300": (300, 30, 462), "p700": (700, 100, 1077), "p1500": (1500, 300, 2308)}
VIP = {"v30": (30, 150), "v90": (90, 400)}                             # days, Stars

# ───────────────────────── database ─────────────────────────
_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT, credits INTEGER DEFAULT 0,
  earned INTEGER DEFAULT 0, referrals INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0, stars_spent INTEGER DEFAULT 0,
  streak INTEGER DEFAULT 0, last_daily TEXT, vip_until INTEGER DEFAULT 0, referred_by INTEGER, banned INTEGER DEFAULT 0, joined TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT DEFAULT '', category TEXT,
  price INTEGER, star_price INTEGER, tg_file_id TEXT, file_name TEXT, size INTEGER, downloads INTEGER DEFAULT 0,
  uploader TEXT, links TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created INTEGER);
CREATE TABLE IF NOT EXISTS purchases(user_id INTEGER, file_id INTEGER, ts INTEGER, PRIMARY KEY(user_id, file_id));
CREATE TABLE IF NOT EXISTS saves(user_id INTEGER, file_id INTEGER, PRIMARY KEY(user_id, file_id));
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, file_id INTEGER, stars INTEGER, PRIMARY KEY(user_id, file_id));
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, file_id INTEGER, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS tx(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, credits INTEGER, max_uses INTEGER, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, kind TEXT, ts TEXT);
""")


def now_s() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime())


def today_s(offset: int = 0) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - offset))


def q(sql, args=(), one=False):
    with _lock:
        rows = db.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        cur = db.execute(sql, args)
        return cur.lastrowid


def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def is_vip(u) -> bool:
    return bool(u["vip_until"] and u["vip_until"] > time.time())


def add_credits(uid: int, amt: int, reason: str) -> bool:
    """Atomic balance change. Returns False if the user is missing or the balance would go negative."""
    with _lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
            if not row or row[0] + amt < 0:
                db.execute("ROLLBACK")
                return False
            db.execute("UPDATE users SET credits=credits+?, earned=earned+? WHERE id=?", (amt, max(amt, 0), uid))
            db.execute("INSERT INTO tx(user_id,amount,reason,ts) VALUES(?,?,?,?)", (uid, amt, reason, now_s()))
            db.execute("COMMIT")
            return True
        except Exception:
            db.execute("ROLLBACK")
            raise


def register(u, ref=None):
    """Create/refresh a user. Returns (is_new, referrer_id_or_None)."""
    if get_user(u.id):
        x("UPDATE users SET name=?, username=? WHERE id=?", (u.full_name, u.username, u.id))
        return False, None
    ref_ok = bool(ref) and ref != u.id and get_user(ref) is not None
    x("INSERT INTO users(id,username,name,joined,referred_by) VALUES(?,?,?,?,?)",
      (u.id, u.username, u.full_name, now_s(), ref if ref_ok else None))
    if ref_ok:
        x("UPDATE users SET referrals=referrals+1 WHERE id=?", (ref,))
        add_credits(ref, REF_BONUS, f"Referral from {u.id}")
        n = get_user(ref)["referrals"]
        if n in MILESTONES:
            add_credits(ref, MILESTONES[n], "Referral milestone bonus")
        return True, ref
    return True, None


def claim_daily(uid: int):
    u = get_user(uid)
    today, yday = today_s(), today_s(86400)
    streak = u["streak"] + 1 if u["last_daily"] == yday else 1
    with _lock:
        cur = db.execute("UPDATE users SET last_daily=?, streak=? WHERE id=? AND COALESCE(last_daily,'')!=?",
                         (today, streak, uid, today))
        if cur.rowcount == 0:
            return None
    amt = DAILY + (VIP_DAILY if is_vip(u) else 0)
    add_credits(uid, amt, "Daily reward")
    bonus = STREAK_BONUS.get(streak, 0)
    if bonus:
        add_credits(uid, bonus, f"{streak}-day streak bonus")
    return {"amount": amt, "bonus": bonus, "streak": streak}


def redeem(uid: int, code: str):
    code = code.strip().upper()
    with _lock:
        p = db.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
        if not p:
            return None, "Invalid code"
        if p["max_uses"] and p["used"] >= p["max_uses"]:
            return None, "This code has reached its limit"
        if db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            return None, "You already used this code"
        db.execute("INSERT INTO promo_uses VALUES(?,?)", (code, uid))
        db.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
    add_credits(uid, p["credits"], f"Promo code {code}")
    return p["credits"], None


def price_for(u, f) -> int:
    p = f["price"]
    return max(0, round(p * (100 - VIP_DISCOUNT) / 100)) if is_vip(u) else p


def esc(s) -> str:
    return html.escape(str(s or ""))


# ───────────────────────── bot ─────────────────────────
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
user_r, admin_r = Router(), Router()
admin_r.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_r.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
BOT_USERNAME = ""


def ref_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={uid}"


def home_text(u) -> str:
    return (f"✦ <b><i>{esc(BRAND)}</i></b> ✦\n\n👋 Welcome back, <b>{esc(u['name'])}</b>!\n\n"
            f"<blockquote>🪙 Credits › <b>{u['credits']}</b>\n👥 Referrals › <b>{u['referrals']}</b>\n"
            f"📥 Downloads › <b>{u['downloads']}</b></blockquote>\n"
            "⚡ <b>What you can do</b>\n"
            "<blockquote>• 🗂 Download source codes, courses, apps &amp; more\n"
            "• 🎯 Finish tasks and earn free credits\n"
            "• 🛒 Buy and sell files on the marketplace\n"
            "• 🎁 Invite friends — earn credits for every invite</blockquote>\n"
            "👇 <i>Tap below to open the app</i>")


def home_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✨ Open App", web_app=WebAppInfo(url=WEBAPP_URL))]]
    share = f"https://t.me/share/url?url={quote(ref_link(uid))}&text={quote('Download files for free on ' + BRAND)}"
    row = [InlineKeyboardButton(text="🎁 Invite & Earn", url=share)]
    if SUPPORT:
        row.append(InlineKeyboardButton(text="💬 Support", url=f"https://t.me/{SUPPORT}"))
    rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@user_r.message(CommandStart())
async def start(m: Message):
    arg = (m.text or "").split(maxsplit=1)
    ref = int(arg[1]) if len(arg) > 1 and arg[1].isdigit() else None
    is_new, referrer = register(m.from_user, ref)
    u = get_user(m.from_user.id)
    if u["banned"]:
        return await m.answer("🚫 You are banned from using this bot.")
    if referrer:
        try:
            await bot.send_message(referrer, f"🎉 <b>{esc(m.from_user.full_name)}</b> joined with your link. +{REF_BONUS} credits!")
        except Exception:
            pass
    if BANNER:
        try:
            return await m.answer_photo(BANNER, caption=home_text(u), reply_markup=home_kb(u["id"]))
        except Exception:
            logging.exception("banner failed")
    await m.answer(home_text(u), reply_markup=home_kb(u["id"]))


@user_r.pre_checkout_query()
async def pre_checkout(p: PreCheckoutQuery):
    await p.answer(ok=True)


async def deliver(uid: int, f):
    cap = f"<b>{esc(f['title'])}</b>"
    if f["description"]:
        cap += f"\n{esc(f['description'])}"
    await bot.send_document(uid, f["tg_file_id"], caption=cap[:1000])


def record_purchase(uid: int, fid: int):
    x("INSERT OR IGNORE INTO purchases VALUES(?,?,?)", (uid, fid, int(time.time())))
    x("UPDATE files SET downloads=downloads+1 WHERE id=?", (fid,))
    x("UPDATE users SET downloads=downloads+1 WHERE id=?", (uid,))


@user_r.message(F.successful_payment)
async def paid(m: Message):
    p, uid = m.successful_payment, m.from_user.id
    try:
        uid_s, kind, key = p.invoice_payload.split("|", 2)
        if int(uid_s) != uid:
            return
        x("INSERT INTO payments VALUES(?,?,?,?,?)", (p.telegram_payment_charge_id, uid, p.total_amount, kind, now_s()))
    except sqlite3.IntegrityError:
        return                                   # already processed
    except Exception:
        logging.exception("bad payment payload")
        return
    x("UPDATE users SET stars_spent=stars_spent+? WHERE id=?", (p.total_amount, uid))
    if kind == "pack":
        credits, bonus, _ = PACKS[key]
        add_credits(uid, credits + bonus, "Top up")
        await m.answer(f"✅ <b>{credits + bonus}</b> credits added to your wallet.")
    elif kind == "vip":
        days, _ = VIP[key]
        u = get_user(uid)
        base = max(int(time.time()), u["vip_until"] or 0)
        x("UPDATE users SET vip_until=? WHERE id=?", (base + days * 86400, uid))
        add_credits(uid, 0, f"Premium VIP ({days} days)")
        await m.answer(f"👑 Premium VIP is active for {days} days.")
    elif kind == "file":
        f = q("SELECT * FROM files WHERE id=?", (int(key),), one=True)
        if f:
            add_credits(uid, 0, f"File: #{f['id']} (Stars)")
            record_purchase(uid, f["id"])
            await deliver(uid, f)


# ───────────────────────── admin panel ─────────────────────────
class Up(StatesGroup):
    file = State(); title = State(); desc = State(); links = State(); price = State()
class EdPrice(StatesGroup):
    price = State()
class Cr(StatesGroup):
    uid = State(); amount = State()
class Pr(StatesGroup):
    code = State(); credits = State(); uses = State()
class Us(StatesGroup):
    uid = State()
class Bc(StatesGroup):
    msg = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in r] for r in rows])


ADMIN_MENU = kb([[("📤 Upload file", "a:up"), ("📁 Manage files", "a:files")],
                 [("💰 Add / remove credits", "a:cr"), ("🎟 Promo codes", "a:promo")],
                 [("👥 Users", "a:users"), ("📢 Broadcast", "a:bc")],
                 [("📊 Stats", "a:stats")]])
BACK = kb([[("⬅️ Admin menu", "a:home")]])


@admin_r.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=ADMIN_MENU)


@admin_r.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Cancelled.", reply_markup=ADMIN_MENU)


@admin_r.callback_query(F.data == "a:home")
async def a_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🛠 <b>Admin panel</b>", reply_markup=ADMIN_MENU)


@admin_r.callback_query(F.data == "a:stats")
async def a_stats(c: CallbackQuery):
    n = lambda s: q(s, one=True)[0] or 0
    await c.message.edit_text(
        f"📊 <b>Stats</b>\n\nUsers: <b>{n('SELECT COUNT(*) FROM users')}</b>\n"
        f"VIP users: <b>{n('SELECT COUNT(*) FROM users WHERE vip_until>' + str(int(time.time())))}</b>\n"
        f"Files: <b>{n('SELECT COUNT(*) FROM files')}</b>\nDownloads: <b>{n('SELECT SUM(downloads) FROM files')}</b>\n"
        f"Credits in wallets: <b>{n('SELECT SUM(credits) FROM users')}</b>\n"
        f"Stars received: <b>{n('SELECT SUM(stars) FROM payments')}</b>\n"
        f"Open reports: <b>{n('SELECT COUNT(*) FROM reports')}</b>", reply_markup=BACK)


# --- upload ---
@admin_r.callback_query(F.data == "a:up")
async def a_up(c: CallbackQuery, state: FSMContext):
    await state.set_state(Up.file)
    await c.message.edit_text("📤 Send the file as a <b>document</b>. (/cancel to stop)")


@admin_r.message(Up.file, F.document)
async def up_file(m: Message, state: FSMContext):
    d = m.document
    await state.update_data(tg_file_id=d.file_id, file_name=d.file_name or "file", size=d.file_size or 0)
    await state.set_state(Up.title)
    await m.answer("Title of the file?")


@admin_r.message(Up.title, F.text)
async def up_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:80])
    await state.set_state(Up.desc)
    await m.answer("Short description? Send <code>-</code> to skip.")


@admin_r.message(Up.desc, F.text)
async def up_desc(m: Message, state: FSMContext):
    await state.update_data(desc="" if m.text.strip() == "-" else m.text.strip()[:200])
    await state.set_state(Up.links)
    await m.answer("Optional buttons, one per line:\n<code>demo=https://...</code>\n<code>tutorial=https://...</code>\n"
                   "<code>bot=https://t.me/...</code>\n\nSend <code>-</code> to skip.")


@admin_r.message(Up.links, F.text)
async def up_links(m: Message, state: FSMContext):
    links = {}
    if m.text.strip() != "-":
        for line in m.text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in ("demo", "tutorial", "bot") and v.strip().startswith("http"):
                    links[k.strip()] = v.strip()
    await state.update_data(links=json.dumps(links))
    await state.set_state(Up.price)
    await m.answer("Price? Send <code>credits stars</code>, for example <code>50 30</code>.\n"
                   "If you send only credits, the Stars price is set to 60% of it.")


@admin_r.message(Up.price, F.text)
async def up_price(m: Message, state: FSMContext):
    parts = m.text.split()
    if not parts or not all(p.isdigit() for p in parts[:2]):
        return await m.answer("Send numbers only, like <code>50 30</code>.")
    credits = int(parts[0])
    stars = int(parts[1]) if len(parts) > 1 else max(1, math.ceil(credits * 0.6))
    d = await state.get_data()
    ext = os.path.splitext(d["file_name"])[1].lstrip(".").upper() or "FILE"
    fid = x("INSERT INTO files(title,description,category,price,star_price,tg_file_id,file_name,size,uploader,links,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d["title"], d["desc"], ext, credits, stars, d["tg_file_id"], d["file_name"], d["size"], UPLOADER, d["links"], int(time.time())))
    await state.clear()
    await m.answer(f"✅ Uploaded <b>#{fid}</b> — {esc(d['title'])}\n{credits} cr · ⭐ {stars}", reply_markup=ADMIN_MENU)


# --- manage files ---
@admin_r.callback_query(F.data == "a:files")
async def a_files(c: CallbackQuery):
    rows = q("SELECT id,title,price,active FROM files ORDER BY id DESC LIMIT 30")
    if not rows:
        return await c.message.edit_text("No files yet.", reply_markup=BACK)
    btns = [[(f"{'' if r['active'] else '🙈 '}#{r['id']} {r['title'][:28]} · {r['price']}cr", f"a:f:{r['id']}")] for r in rows]
    await c.message.edit_text("📁 <b>Files</b> (latest 30)", reply_markup=kb(btns + [[("⬅️ Admin menu", "a:home")]]))


@admin_r.callback_query(F.data.startswith("a:f:"))
async def a_file(c: CallbackQuery):
    await show_file(c, int(c.data.split(":")[2]))


async def show_file(c: CallbackQuery, fid: int):
    f = q("SELECT * FROM files WHERE id=?", (fid,), one=True)
    if not f:
        return await c.answer("Not found")
    r = q("SELECT AVG(stars) a, COUNT(*) n FROM ratings WHERE file_id=?", (f["id"],), one=True)
    await c.message.edit_text(
        f"<b>#{f['id']} {esc(f['title'])}</b>\n{esc(f['description'])}\n\nType: {f['category']} · {f['size'] // 1024} KB\n"
        f"Price: {f['price']} cr · ⭐ {f['star_price']}\nDownloads: {f['downloads']}\n"
        f"Rating: {round(r['a'], 1) if r['a'] else '—'} ({r['n']})\nStatus: {'visible' if f['active'] else 'hidden'}",
        reply_markup=kb([[("💲 Edit price", f"a:fp:{f['id']}"), ("👁 Hide / show", f"a:ft:{f['id']}")],
                         [("🗑 Delete", f"a:fd:{f['id']}"), ("⬅️ Files", "a:files")]]))


@admin_r.callback_query(F.data.startswith("a:ft:"))
async def a_toggle(c: CallbackQuery):
    fid = int(c.data.split(":")[2])
    x("UPDATE files SET active=1-active WHERE id=?", (fid,))
    await show_file(c, fid)


@admin_r.callback_query(F.data.startswith("a:fd:"))
async def a_delete(c: CallbackQuery):
    fid = int(c.data.split(":")[2])
    x("DELETE FROM files WHERE id=?", (fid,))
    await c.answer("Deleted")
    await a_files(c)


@admin_r.callback_query(F.data.startswith("a:fp:"))
async def a_price(c: CallbackQuery, state: FSMContext):
    await state.set_state(EdPrice.price)
    await state.update_data(fid=int(c.data.split(":")[2]))
    await c.message.edit_text("New price? Send <code>credits stars</code>, for example <code>50 30</code>.")


@admin_r.message(EdPrice.price, F.text)
async def a_price_set(m: Message, state: FSMContext):
    parts = m.text.split()
    if not parts or not all(p.isdigit() for p in parts[:2]):
        return await m.answer("Send numbers only, like <code>50 30</code>.")
    credits = int(parts[0])
    stars = int(parts[1]) if len(parts) > 1 else max(1, math.ceil(credits * 0.6))
    fid = (await state.get_data())["fid"]
    x("UPDATE files SET price=?, star_price=? WHERE id=?", (credits, stars, fid))
    await state.clear()
    await m.answer(f"✅ #{fid} is now {credits} cr · ⭐ {stars}", reply_markup=ADMIN_MENU)


# --- credits ---
@admin_r.callback_query(F.data == "a:cr")
async def a_cr(c: CallbackQuery, state: FSMContext):
    await state.set_state(Cr.uid)
    await c.message.edit_text("User ID? (/cancel to stop)")


@admin_r.message(Cr.uid, F.text)
async def cr_uid(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not get_user(int(m.text)):
        return await m.answer("No user with that ID. Try again.")
    await state.update_data(uid=int(m.text))
    await state.set_state(Cr.amount)
    await m.answer("Amount? Use a minus sign to remove, like <code>-20</code>.")


@admin_r.message(Cr.amount, F.text)
async def cr_amount(m: Message, state: FSMContext):
    try:
        amt = int(m.text)
    except ValueError:
        return await m.answer("Send a whole number.")
    uid = (await state.get_data())["uid"]
    ok = add_credits(uid, amt, "Added by admin" if amt >= 0 else "Removed by admin")
    await state.clear()
    if not ok:
        return await m.answer("❌ Balance can't go below zero.", reply_markup=ADMIN_MENU)
    await m.answer(f"✅ Done. New balance: <b>{get_user(uid)['credits']}</b>", reply_markup=ADMIN_MENU)
    try:
        await bot.send_message(uid, f"💰 {'+' if amt >= 0 else ''}{amt} credits — balance: {get_user(uid)['credits']}")
    except Exception:
        pass


# --- promo ---
@admin_r.callback_query(F.data == "a:promo")
async def a_promo(c: CallbackQuery):
    rows = q("SELECT * FROM promos ORDER BY rowid DESC LIMIT 20")
    txt = "🎟 <b>Promo codes</b>\n\n" + ("\n".join(
        f"<code>{r['code']}</code> · +{r['credits']} · {r['used']}/{r['max_uses'] or '∞'}" for r in rows) or "None yet.")
    btns = [[(f"🗑 {r['code']}", f"a:pd:{r['code']}")] for r in rows]
    await c.message.edit_text(txt, reply_markup=kb([[("➕ New code", "a:pn")]] + btns + [[("⬅️ Admin menu", "a:home")]]))


@admin_r.callback_query(F.data.startswith("a:pd:"))
async def a_promo_del(c: CallbackQuery):
    x("DELETE FROM promos WHERE code=?", (c.data.split(":", 2)[2],))
    await c.answer("Deleted")
    await a_promo(c)


@admin_r.callback_query(F.data == "a:pn")
async def a_promo_new(c: CallbackQuery, state: FSMContext):
    await state.set_state(Pr.code)
    await c.message.edit_text("Code text? (letters and numbers, /cancel to stop)")


@admin_r.message(Pr.code, F.text)
async def pr_code(m: Message, state: FSMContext):
    await state.update_data(code=m.text.strip().upper()[:30])
    await state.set_state(Pr.credits)
    await m.answer("How many credits does it give?")


@admin_r.message(Pr.credits, F.text)
async def pr_credits(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("Send a number.")
    await state.update_data(credits=int(m.text))
    await state.set_state(Pr.uses)
    await m.answer("How many people can use it? Send <code>0</code> for unlimited.")


@admin_r.message(Pr.uses, F.text)
async def pr_uses(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("Send a number.")
    d = await state.get_data()
    x("INSERT OR REPLACE INTO promos(code,credits,max_uses,used) VALUES(?,?,?,0)", (d["code"], d["credits"], int(m.text)))
    await state.clear()
    await m.answer(f"✅ <code>{d['code']}</code> → +{d['credits']} credits", reply_markup=ADMIN_MENU)


# --- users ---
def user_card(u):
    vip = f"VIP until {time.strftime('%Y-%m-%d', time.gmtime(u['vip_until']))}" if is_vip(u) else "no VIP"
    return (f"👤 <b>{esc(u['name'])}</b> @{esc(u['username'] or '—')}\nID: <code>{u['id']}</code>\n"
            f"Credits: {u['credits']} (earned {u['earned']})\nReferrals: {u['referrals']} · Downloads: {u['downloads']}\n"
            f"Streak: {u['streak']} · {vip}\nJoined: {u['joined']}\nStatus: {'🚫 banned' if u['banned'] else 'active'}")


@admin_r.callback_query(F.data == "a:users")
async def a_users(c: CallbackQuery, state: FSMContext):
    await state.set_state(Us.uid)
    await c.message.edit_text("User ID to look up? (/cancel to stop)")


@admin_r.message(Us.uid, F.text)
async def us_lookup(m: Message, state: FSMContext):
    u = get_user(int(m.text)) if m.text.strip().isdigit() else None
    if not u:
        return await m.answer("No user with that ID. Try again.")
    await state.clear()
    await m.answer(user_card(u), reply_markup=kb([[("✅ Unban" if u["banned"] else "🚫 Ban", f"a:ub:{u['id']}")],
                                                   [("⬅️ Admin menu", "a:home")]]))


@admin_r.callback_query(F.data.startswith("a:ub:"))
async def a_ban(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    x("UPDATE users SET banned=1-banned WHERE id=?", (uid,))
    u = get_user(uid)
    await c.message.edit_text(user_card(u), reply_markup=kb([[("✅ Unban" if u["banned"] else "🚫 Ban", f"a:ub:{uid}")],
                                                              [("⬅️ Admin menu", "a:home")]]))


# --- broadcast ---
@admin_r.callback_query(F.data == "a:bc")
async def a_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Bc.msg)
    await c.message.edit_text("Send the message to broadcast (text, photo or file). /cancel to stop.")


@admin_r.message(Bc.msg)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    ids = [r["id"] for r in q("SELECT id FROM users WHERE banned=0")]
    status = await m.answer(f"Sending to {len(ids)} users…")
    ok = 0
    for uid in ids:
        try:
            await m.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"📢 Delivered to {ok} of {len(ids)} users.", reply_markup=ADMIN_MENU)


dp.include_router(admin_r)   # admin first so its FSM handlers win
dp.include_router(user_r)

# ───────────────────────── mini app API ─────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="✨ Open App", web_app=WebAppInfo(url=WEBAPP_URL)))
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)
_locks = defaultdict(asyncio.Lock)


def auth(x_init_data: str = Header("")):
    try:
        data = dict(parse_qsl(x_init_data, keep_blank_values=True))
        got = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            raise ValueError
        if time.time() - int(data["auth_date"]) > 86400:
            raise ValueError
        uid = json.loads(data["user"])["id"]
    except Exception:
        raise HTTPException(401, "Open this app from the bot")
    u = get_user(uid)
    if not u:
        raise HTTPException(401, "Send /start to the bot first")
    if u["banned"]:
        raise HTTPException(403, "You are banned")
    return u


def file_json(f, u):
    uid = u["id"]
    r = q("SELECT AVG(stars) a, COUNT(*) n FROM ratings WHERE file_id=?", (f["id"],), one=True)
    return {"id": f["id"], "title": f["title"], "description": f["description"], "category": f["category"],
            "size": f["size"], "downloads": f["downloads"], "created": f["created"], "uploader": f["uploader"],
            "price": price_for(u, f), "base_price": f["price"], "stars": f["star_price"],
            "links": json.loads(f["links"] or "{}"),
            "owned": q("SELECT 1 FROM purchases WHERE user_id=? AND file_id=?", (uid, f["id"]), one=True) is not None,
            "saved": q("SELECT 1 FROM saves WHERE user_id=? AND file_id=?", (uid, f["id"]), one=True) is not None,
            "rating": round(r["a"], 1) if r["a"] else None, "rating_count": r["n"]}


def get_active_file(fid: int):
    f = q("SELECT * FROM files WHERE id=? AND active=1", (fid,), one=True)
    if not f:
        raise HTTPException(404, "File not found")
    return f


def daily_info(u):
    claimed = u["last_daily"] == today_s()
    streak = u["streak"] if u["last_daily"] in (today_s(), today_s(86400)) else 0
    nxt = next(((d - streak, b) for d, b in sorted(STREAK_BONUS.items()) if d > streak), None)
    return {"claimed": claimed, "streak": streak, "amount": DAILY + (VIP_DAILY if is_vip(u) else 0),
            "next_in": nxt[0] if nxt else None, "next_bonus": nxt[1] if nxt else None,
            "steps": sorted(STREAK_BONUS)}


@app.get("/api/me")
def me(u=Depends(auth)):
    refe = q("SELECT COALESCE(SUM(amount),0) s FROM tx WHERE user_id=? AND reason LIKE 'Referral%'", (u["id"],), one=True)["s"]
    return {"id": u["id"], "name": u["name"], "username": u["username"], "credits": u["credits"], "earned": u["earned"],
            "referrals": u["referrals"], "referral_earned": refe, "downloads": u["downloads"], "stars_spent": u["stars_spent"],
            "vip": is_vip(u), "vip_until": u["vip_until"], "daily": daily_info(u), "ref_link": ref_link(u["id"]),
            "ref_bonus": REF_BONUS, "milestones": MILESTONES, "brand": BRAND, "support": SUPPORT,
            "packs": {k: {"credits": v[0], "bonus": v[1], "stars": v[2]} for k, v in PACKS.items()},
            "vip_plans": {k: {"days": v[0], "stars": v[1]} for k, v in VIP.items()}, "vip_discount": VIP_DISCOUNT,
            "vip_daily": VIP_DAILY}


@app.get("/api/home")
def home(u=Depends(auth)):
    cats = q("SELECT category, COUNT(*) n FROM files WHERE active=1 GROUP BY category ORDER BY n DESC")
    trend = q("SELECT * FROM files WHERE active=1 ORDER BY downloads DESC, id DESC LIMIT 5")
    return {"categories": [{"name": c["category"], "count": c["n"]} for c in cats],
            "trending": [file_json(f, u) for f in trend]}


@app.get("/api/files")
def files(q_: str = "", cat: str = "", saved: int = 0, u=Depends(auth)):
    sql, args = "SELECT * FROM files WHERE active=1", []
    if q_:
        sql += " AND (title LIKE ? OR description LIKE ?)"
        args += [f"%{q_}%", f"%{q_}%"]
    if cat:
        sql += " AND category=?"
        args.append(cat)
    if saved:
        sql += " AND id IN (SELECT file_id FROM saves WHERE user_id=?)"
        args.append(u["id"])
    rows = q(sql + " ORDER BY id DESC", args)
    cats = [r["category"] for r in q("SELECT DISTINCT category FROM files WHERE active=1 ORDER BY category")]
    return {"files": [file_json(f, u) for f in rows], "categories": cats}


@app.post("/api/files/{fid}/get")
async def get_file(fid: int, u=Depends(auth)):
    async with _locks[u["id"]]:
        u = get_user(u["id"])
        f = get_active_file(fid)
        owned = q("SELECT 1 FROM purchases WHERE user_id=? AND file_id=?", (u["id"], fid), one=True) is not None
        price = 0 if owned else price_for(u, f)
        if u["credits"] < price:
            raise HTTPException(402, "Not enough credits")
        try:
            await deliver(u["id"], f)
        except Exception:
            raise HTTPException(502, "Couldn't send the file. Open the bot, press /start and try again.")
        if not owned:
            if price and not add_credits(u["id"], -price, f"File: #{fid}"):
                raise HTTPException(402, "Not enough credits")
            if not price:
                add_credits(u["id"], 0, f"File: #{fid}")
            record_purchase(u["id"], fid)
    return {"ok": True, "credits": get_user(u["id"])["credits"]}


@app.post("/api/files/{fid}/stars")
async def file_stars(fid: int, u=Depends(auth)):
    f = get_active_file(fid)
    link = await bot.create_invoice_link(title=f["title"][:32], description=(f["description"] or f["title"])[:200],
                                         payload=f"{u['id']}|file|{fid}", provider_token="", currency="XTR",
                                         prices=[LabeledPrice(label="File", amount=f["star_price"])])
    return {"link": link}


@app.post("/api/files/{fid}/save")
def save_file(fid: int, u=Depends(auth)):
    get_active_file(fid)
    if q("SELECT 1 FROM saves WHERE user_id=? AND file_id=?", (u["id"], fid), one=True):
        x("DELETE FROM saves WHERE user_id=? AND file_id=?", (u["id"], fid))
        return {"saved": False}
    x("INSERT INTO saves VALUES(?,?)", (u["id"], fid))
    return {"saved": True}


class Rate(BaseModel):
    stars: int


@app.post("/api/files/{fid}/rate")
def rate_file(fid: int, b: Rate, u=Depends(auth)):
    get_active_file(fid)
    if not q("SELECT 1 FROM purchases WHERE user_id=? AND file_id=?", (u["id"], fid), one=True):
        raise HTTPException(403, "Get the file before rating it")
    if not 1 <= b.stars <= 5:
        raise HTTPException(400, "Pick 1 to 5 stars")
    x("INSERT OR REPLACE INTO ratings VALUES(?,?,?)", (u["id"], fid, b.stars))
    return {"ok": True}


class Report(BaseModel):
    reason: str = ""


@app.post("/api/files/{fid}/report")
async def report_file(fid: int, b: Report, u=Depends(auth)):
    f = get_active_file(fid)
    x("INSERT INTO reports(user_id,file_id,reason,ts) VALUES(?,?,?,?)", (u["id"], fid, b.reason[:200], now_s()))
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, f"🚩 Report on <b>#{fid} {esc(f['title'])}</b> from <code>{u['id']}</code>\n{esc(b.reason)}")
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/daily")
def daily(u=Depends(auth)):
    r = claim_daily(u["id"])
    if not r:
        raise HTTPException(400, "You already claimed today")
    return r


class Code(BaseModel):
    code: str


@app.post("/api/promo")
def promo(b: Code, u=Depends(auth)):
    credits, err = redeem(u["id"], b.code)
    if err:
        raise HTTPException(400, err)
    return {"credits": credits}


@app.get("/api/history")
def history(u=Depends(auth)):
    return [{"reason": r["reason"], "amount": r["amount"], "ts": r["ts"]}
            for r in q("SELECT * FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 60", (u["id"],))]


@app.get("/api/purchases")
def purchases(u=Depends(auth)):
    rows = q("SELECT f.* FROM purchases p JOIN files f ON f.id=p.file_id WHERE p.user_id=? ORDER BY p.ts DESC", (u["id"],))
    return [file_json(f, u) for f in rows]


class Buy(BaseModel):
    key: str


@app.post("/api/topup")
async def topup(b: Buy, u=Depends(auth)):
    if b.key not in PACKS:
        raise HTTPException(400, "Unknown pack")
    credits, bonus, stars = PACKS[b.key]
    link = await bot.create_invoice_link(title=f"{credits + bonus} credits", description=f"Top up {BRAND} wallet",
                                         payload=f"{u['id']}|pack|{b.key}", provider_token="", currency="XTR",
                                         prices=[LabeledPrice(label="Credits", amount=stars)])
    return {"link": link}


@app.post("/api/vip")
async def vip(b: Buy, u=Depends(auth)):
    if b.key not in VIP:
        raise HTTPException(400, "Unknown plan")
    days, stars = VIP[b.key]
    link = await bot.create_invoice_link(title=f"Premium VIP · {days} days", description="20% off every file and bonus daily credits",
                                         payload=f"{u['id']}|vip|{b.key}", provider_token="", currency="XTR",
                                         prices=[LabeledPrice(label="Premium VIP", amount=stars)])
    return {"link": link}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "webapp", "index.html"))
