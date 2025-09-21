# bot.py — مقفول على مجموعة واحدة باستخدام GROUP_ID من Environment
import os, logging, asyncio, traceback
from typing import List, Dict, Optional
from openpyxl import load_workbook
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest, Forbidden, TimedOut, NetworkError, RetryAfter

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# ===== إعداد القفل =====
GROUP_ID = int(os.getenv("GROUP_ID", "0"))   # لازم يكون -1003070817023 عندك
DENY_MSG = "❌ هذا البوت يعمل فقط داخل المجموعة الخاصة."

def in_allowed_chat(update: Update) -> bool:
    return bool(update.effective_chat and GROUP_ID != 0 and update.effective_chat.id == GROUP_ID)

# ===== الحذف التلقائي (اختياري) =====
AUTO_DELETE_SECONDS = 300        # 0 = معطّل. خليه 300 حتى يحذف بعد 5 دقائق
DELETE_BOT_MESSAGES = True     # إذا True يحذف حتى ردود البوت بعد المدة

async def _delete_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, msg_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass

def schedule_autodelete(context: ContextTypes.DEFAULT_TYPE, msg: Optional[Message]):
    if AUTO_DELETE_SECONDS <= 0 or not msg:
        return
    if msg.chat_id != GROUP_ID:
        return
    try:
        context.job_queue.run_once(_delete_job, AUTO_DELETE_SECONDS, data=(msg.chat_id, msg.message_id))
    except Exception:
        pass

# ===== ملفات البيانات =====
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
    return {h: ("" if v is None else str(v)) for h, v in zip(headers, row_values)}

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
        from openpyxl import load_workbook
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        headers = read_first_sheet_headers(ws)
        if not headers:
            wb.close(); return None
        plate_col_name = detect_plate_col(headers)
        if not plate_col_name:
            wb.close(); return None
        idx = {headers[i]: i for i in range(len(headers))}.get(plate_col_name, None)
        if idx is None:
            wb.close(); return None
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or idx >= len(row): continue
            val = row[idx]
            if val is None: continue
            if norm(str(val)) == key or (key in norm(str(val))):
                row_dict = build_row_dict(headers, list(row))
                wb.close()
                return row_dict
        wb.close(); return None
    except Exception as e:
        logging.exception(f"خطأ أثناء قراءة {xlsx_path}: {e}")
        return None

# ===== إرسال آمن =====
async def safe_send_text(update: Update, text: str, context: ContextTypes.DEFAULT_TYPE, max_attempts=3):
    attempt = 0
    msg = None
    while attempt < max_attempts:
        try:
            msg = await update.message.reply_text(text)
            break
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1); attempt += 1
        except (BadRequest, Forbidden, TimedOut, NetworkError):
            await asyncio.sleep(1.2); attempt += 1
    if msg and DELETE_BOT_MESSAGES and AUTO_DELETE_SECONDS > 0:
        schedule_autodelete(context, msg)
    return msg

async def send_in_chunks(update: Update, text: str, context: ContextTypes.DEFAULT_TYPE):
    start, n = 0, len(text)
    while start < n:
        end = min(start + MAX_LEN, n)
        await safe_send_text(update, text[start:end], context)
        await asyncio.sleep(0.6)
        start = end

# ===== أوامر =====
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # يبقى مفيد لو احتجت التأكد
    await update.message.reply_text(
        f"User ID: {update.effective_user.id}\nChat ID: {update.effective_chat.id}\nGROUP_ID (env): {GROUP_ID}"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_chat(update): return
    await safe_send_text(update, "👋 أهلاً بأعضاء المجموعة.\nأرسل رقم السيارة للبحث.\nأوامر: /ping /id", context)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_chat(update): return
    await safe_send_text(update, "pong ✅", context)

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_chat(update): return
    existing = [os.path.basename(f) for f in EXCEL_FILES if os.path.exists(f)]
    txt = "\n".join(["Files on server:"] + existing) if existing else "ماكو ملفات إكسل على السيرفر."
    await safe_send_text(update, txt, context)

# ===== البحث =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not in_allowed_chat(update):
            return  # صمت خارج المجموعة

        text = (update.message.text or "").strip()
        if not text:
            await safe_send_text(update, "اكتب رقم السيارة حتى أبحث عنه.", context)
            return
        key = norm(text)

        for path in EXCEL_FILES:
            row = search_plate_once(path, key)
            if row:
                msg = format_response(row, source_name=os.path.basename(path))
                if len(msg) > MAX_LEN:
                    await send_in_chunks(update, msg, context)
                else:
                    await safe_send_text(update, msg, context)
                return

        await safe_send_text(update, f"ماكو معلومات للسيارة رقم: {text}", context)

    except (BadRequest, Forbidden, TimedOut, NetworkError, RetryAfter) as e:
        logging.error(f"Telegram error: {type(e).__name__}: {e}")
        if in_allowed_chat(update):
            await safe_send_text(update, "خطأ إرسال: جرّب بعد شوي.", context)
    except Exception:
        logging.error("Unhandled error:\n" + traceback.format_exc())
        if in_allowed_chat(update):
            await safe_send_text(update, "صار خطأ غير متوقع داخل البوت.", context)

# ===== التشغيل =====
if __name__ == "__main__":
    token = read_token()
    if not token:
        print("ضع التوكن في TELEGRAM_BOT_TOKEN أو token.txt.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(token).build()

    # نسجّل /id بدون فلتر لو احتجته بأي مكان
    app.add_handler(CommandHandler("id", id_cmd))

    # بقية الأوامر والرسائل محصورة على المجموعة
    only_group = filters.Chat(GROUP_ID) if GROUP_ID != 0 else filters.ChatType.GROUPS
    app.add_handler(CommandHandler("start", start, filters=only_group))
    app.add_handler(CommandHandler("ping", ping, filters=only_group))
    app.add_handler(CommandHandler("debug", debug_cmd, filters=only_group))
    app.add_handler(MessageHandler(only_group & (filters.TEXT & ~filters.COMMAND), handle_message))

    if GROUP_ID == 0:
        logging.warning("⚠️ GROUP_ID غير مضبوط. ضعه في Environment ثم أعد النشر.")

    print("البوت يعمل… مقفول على المجموعة المحددة بـ GROUP_ID.")
    app.run_polling()

