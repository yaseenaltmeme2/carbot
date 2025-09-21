# bot.py — نسخة كاملة مع كود DEBUG لاستخراج Chat ID

import os, logging, asyncio, traceback
from typing import List, Dict, Optional
from openpyxl import load_workbook
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest, Forbidden, TimedOut, NetworkError, RetryAfter

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# ===== ضبط القفل =====
def parse_group_id() -> int:
    val = os.getenv("GROUP_ID", "").strip()
    if not val:
        return 0
    try:
        return int(val)
    except:
        return 0

GROUP_ID = parse_group_id()
DENY_MSG = "❌ هذا البوت يعمل فقط داخل المجموعة الخاصة."

# ===== DEBUG لاستخراج IDs =====
ADMIN_ID = 0  # إذا عرفت User ID مالتك حطه هنا، إذا بقي صفر رح يطبع بس باللوگ

async def _capture_ids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    try:
        logging.info(f"[CAPTURE] ChatID={getattr(c,'id',None)} Type={getattr(c,'type',None)} "
                     f"UserID={getattr(u,'id',None)} Username={getattr(u,'username',None)}")
        if u and ADMIN_ID and u.id == ADMIN_ID:
            await context.bot.send_message(
                chat_id=u.id,
                text=f"Chat ID: {c.id}\nUser ID: {u.id}\nChat Type: {c.type}"
            )
    except Exception:
        pass

def in_allowed_chat(update: Update) -> bool:
    return bool(update.effective_chat and GROUP_ID != 0 and update.effective_chat.id == GROUP_ID)

# ===== ملفات الإكسل =====
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE)

EXCEL_FILES = [
    os.path.join(DATA_DIR, "اكسل ارشيف باجات السيارات 2024.xlsx"),
    os.path.join(DATA_DIR, "اكسل سيارات 2025.xlsx"),
]

PLATE_CANDIDATES = ["رقمها", "رقم اللوحة", "رقم السيارة", "رقم العجلة", "رقم", "الرقم", "لوحة"]
RESPONSE_COLUMNS = ["الاسم الثلاثي", "العنوان", "الوظيفي", "مكان العمل", "لونها", "رقمها", "تسلسل الباج", "رقم الهاتف"]
MAX_LEN = 3900

def read_token():
    p = os.path.join(BASE, "token.txt")
    if os.path.exists(p):
        return open(p, "r", encoding="utf-8").read().strip()
    return os.getenv("TELEGRAM_BOT_TOKEN")

def norm(s: str) -> str:
    return str(s).replace("\u200f","").replace("\u200e","").strip().upper()

def norm_col(s: str) -> str:
    return str(s).replace("\u200f","").replace("\u200e","").strip()

def detect_plate_col(headers: List[str]) -> Optional[str]:
    headers_n = [norm_col(h) for h in headers]
    for k in PLATE_CANDIDATES[:4]:
        if k in headers_n:
            return headers[headers_n.index(k)]
    for i, h in enumerate(headers_n):
        for k in PLATE_CANDIDATES:
            if k in h:
                return headers[i]
    return None

def read_first_sheet_headers(ws) -> List[str]:
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        return [norm_col(c if c is not None else "") for c in row]
    return []

def build_row_dict(headers: List[str], row_values: List[str]) -> Dict[str, str]:
    out = {}
    for h, v in zip(headers, row_values):
        out[h] = "" if v is None else str(v)
    return out

def format_response(row: Dict[str,str], source_name:str) -> str:
    parts = []
    if "رقمها" in row:
        parts.append(f"🔎 نتيجة البحث للرقم: {row.get('رقمها','')}")
        parts.append("—" * 10)
    for col in RESPONSE_COLUMNS:
        if col in row:
            parts.append(f"{col}: {row.get(col,'')}")
    parts.append(f"المصدر: {source_name}")
    return "\n".join(parts)

def search_plate_once(xlsx_path: str, key: str) -> Optional[Dict[str,str]]:
    if not os.path.exists(xlsx_path):
        return None
    try:
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        headers = read_first_sheet_headers(ws)
        if not headers:
            wb.close(); return None
        plate_col_name = detect_plate_col(headers)
        if not plate_col_name:
            wb.close(); return None
        headers_map = {headers[i]: i for i in range(len(headers))}
        plate_idx = headers_map.get(plate_col_name, None)
        if plate_idx is None:
            wb.close(); return None

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or plate_idx >= len(row): continue
            val = row[plate_idx]
            if val is None: continue
            if norm(str(val)) == key or (key in norm(str(val))):
                row_dict = build_row_dict(headers, list(row))
                wb.close()
                return row_dict
        wb.close()
        return None
    except Exception as e:
        logging.exception(f"خطأ أثناء قراءة {xlsx_path}: {e}")
        return None

# ===== إرسال آمن =====
async def safe_send_text(update: Update, text: str, max_attempts=3):
    attempt = 0
    while attempt < max_attempts:
        try:
            return await update.message.reply_text(text)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1); attempt += 1
        except (BadRequest, Forbidden, TimedOut, NetworkError):
            await asyncio.sleep(1.2); attempt += 1
    try:
        return await update.message.reply_text("تعذر الإرسال حالياً.")
    except Exception:
        return None

async def send_in_chunks(update: Update, text: str):
    start, n = 0, len(text)
    while start < n:
        end = min(start + MAX_LEN, n)
        await safe_send_text(update, text[start:end])
        await asyncio.sleep(0.6)
        start = end

# ===== أوامر =====
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"User ID: {update.effective_user.id}\nChat ID: {update.effective_chat.id}\nGROUP_ID (env): {GROUP_ID}"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_chat(update):
        return
    await safe_send_text(update, "👋 أهلاً، أرسل رقم السيارة للبحث.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_chat(update):
        return
    await safe_send_text(update, "pong ✅")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_chat(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        await safe_send_text(update, "اكتب رقم السيارة حتى أبحث عنه.")
        return
    key = norm(text)
    for path in EXCEL_FILES:
        row = search_plate_once(path, key)
        if row:
            msg = format_response(row, source_name=os.path.basename(path))
            if len(msg) > MAX_LEN:
                await send_in_chunks(update, msg)
            else:
                await safe_send_text(update, msg)
            return
    await safe_send_text(update, f"ماكو معلومات للسيارة رقم: {text}")

# ===== تشغيل البوت =====
if __name__ == "__main__":
    token = read_token()
    if not token:
        print("ضع التوكن في TELEGRAM_BOT_TOKEN أو token.txt.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(token).build()

    # DEBUG: يطبع أي IDs
    app.add_handler(MessageHandler(filters.ALL, _capture_ids), group=0)

    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if GROUP_ID == 0:
        logging.warning("⚠️ لم يتم ضبط GROUP_ID. أرسل أي رسالة بالمجموعة ثم شوف الـ Logs حتى تاخذ Chat ID.")
    print("البوت يعمل. شوف الـ Logs للحصول على Chat ID إذا بعده غير مضبوط.")
    app.run_polling()
