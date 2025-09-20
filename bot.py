# bot.py — نسخة مستقرة: يعرض فقط الأعمدة المطلوبة + مكافحة Flood + اكتشاف عمود اللوحة تلقائياً
import os, logging, traceback, asyncio
from io import BytesIO
import pandas as pd
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest, Forbidden, TimedOut, NetworkError, RetryAfter

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# ==== إعداد المسارات والملفات ====
BASE = os.path.dirname(os.path.abspath(__file__))
FILES = [
    os.path.join(BASE, "اكسل ارشيف باجات السيارات 2024.xlsx"),
    os.path.join(BASE, "اكسل سيارات 2025.xlsx"),
]

# اسم عمود اللوحة الافتراضي (قد يُستبدل تلقائياً عبر الاكتشاف)
PLATE_COLUMN = "رقمها"

# الأعمدة المطلوبة فقط (بالترتيب) — سيتم عرضها فقط
EXPECTED_COLUMNS_ORDER = [
    "الاسم الثلاثي", "العنوان", "الوظيفي", "مكان العمل",
    "لونها", "رقمها", "تسلسل الباج", "رقم الهاتف"
]
SHOW_ONLY_EXPECTED = True  # لا تعرض أي أعمدة إضافية

DATA_DF = None
MAX_LEN = 3900      # أقل من حد 4096 لتجنب "Message is too long"
MAX_RESULTS = 1     # نرسل نتيجة واحدة لتقليل الرسائل وتفادي الفلود

# ==== أدوات عامة ====
def read_token():
    token_path = os.path.join(BASE, "token.txt")
    if os.path.exists(token_path):
        return open(token_path, "r", encoding="utf-8").read().strip()
    return os.getenv("TELEGRAM_BOT_TOKEN")

def _normalize_text(s: str) -> str:
    # إزالة أحرف اتجاه خفية + فراغات + تحويل لـ Upper
    return str(s).replace("\u200f", "").replace("\u200e", "").strip().upper()

def _normalize_colname(s: str) -> str:
    return str(s).replace("\u200f", "").replace("\u200e", "").strip()

def detect_plate_column(cols):
    """يحاول اكتشاف عمود اللوحة تلقائياً إذا الاسم مختلف."""
    exact = ["رقمها", "رقم اللوحة", "رقم السيارة", "رقم العجلة"]
    for k in exact:
        if k in cols:
            return k
    contains = ["رقم", "الرقم", "لوحة", "العجلة", "السيارة"]
    for c in cols:
        for k in contains:
            if k in c:
                return c
    return None

def load_data():
    """تحميل ودمج ملفات الإكسل + تطبيع الأعمدة والقيم + اكتشاف عمود اللوحة + تمييز المصدر."""
    dfs = []
    for f in FILES:
        if not os.path.exists(f):
            logging.warning(f"⚠ الملف غير موجود: {f}")
            continue
        try:
            df = pd.read_excel(f, engine="openpyxl")
            df.columns = [_normalize_colname(c) for c in df.columns]
            # نضيف عمود "المصدر" لكن لن نعرضه لأننا نظهر فقط الأعمدة المطلوبة
            df["المصدر"] = os.path.basename(f)
            dfs.append(df)
        except Exception as e:
            logging.exception(f"❌ خطأ بقراءة {f}: {e}")
    if not dfs:
        return pd.DataFrame()

    out = pd.concat(dfs, ignore_index=True).fillna("")
    # اكتشاف عمود اللوحة لو الافتراضي غير موجود
    global PLATE_COLUMN
    if PLATE_COLUMN not in out.columns:
        guess = detect_plate_column(list(out.columns))
        if guess:
            PLATE_COLUMN = guess
            logging.info(f"🔎 اكتشفنا عمود اللوحة: {PLATE_COLUMN}")
        else:
            logging.warning(f"⚠ لم نعثر على عمود لوحة مناسب ضمن الأعمدة: {list(out.columns)}")

    if PLATE_COLUMN in out.columns:
        out[PLATE_COLUMN] = out[PLATE_COLUMN].apply(_normalize_text)
    return out

def format_row(row, preferred_order):
    parts = []
    # سطر تعريف بسيط
    if PLATE_COLUMN in row.index:
        parts.append(f"🔎 نتيجة البحث للرقم: {row[PLATE_COLUMN]}")
        parts.append("—" * 10)
    # نعرض فقط الأعمدة المطلوبة وبنفس الترتيب
    for col in preferred_order:
        if col in row.index:
            val = "" if pd.isna(row[col]) else str(row[col])
            parts.append(f"{col}: {val}")
    # لا نعرض أعمدة إضافية إذا SHOW_ONLY_EXPECTED = True
    if not SHOW_ONLY_EXPECTED:
        preferred_set = set(preferred_order)
        for col in row.index:
            if col not in preferred_set:
                val = "" if pd.isna(row[col]) else str(row[col])
                parts.append(f"{col}: {val}")
    return "\n".join(parts)

# ==== إرسال آمن مع مكافحة Flood ====
async def safe_send_text(update, text, max_attempts=3):
    attempt = 0
    while attempt < max_attempts:
        try:
            return await update.message.reply_text(text)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            attempt += 1
        except (BadRequest, Forbidden, TimedOut, NetworkError):
            await asyncio.sleep(1.2)
            attempt += 1
    try:
        return await update.message.reply_text("تعذر الإرسال حالياً بسبب قيود تيليغرام. جرّب مرة ثانية.")
    except Exception:
        return None

async def send_in_chunks(update, text, chunk_size=MAX_LEN):
    start, n = 0, len(text)
    while start < n:
        end = min(start + chunk_size, n)
        await safe_send_text(update, text[start:end])
        await asyncio.sleep(0.6)  # تأخير بسيط يقلّل احتمال الفلود
        start = end

# ==== أوامر ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DATA_DF
    if DATA_DF is None or DATA_DF.empty:
        DATA_DF = load_data()
    rows = 0 if DATA_DF is None else len(DATA_DF)
    await safe_send_text(
        update,
        f"هلا! أرسل رقم السيارة (من عمود '{PLATE_COLUMN}').\n"
        f"عدد السجلات المحمّلة: {rows}\n"
        f"أوامر: /ping /reload /debug"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_text(update, "pong ✅")

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DATA_DF
    DATA_DF = load_data()
    await safe_send_text(update, f"تم إعادة تحميل البيانات. السجلات: {len(DATA_DF)}")

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DATA_DF, PLATE_COLUMN
    if DATA_DF is None or DATA_DF.empty:
        DATA_DF = load_data()
    rows = 0 if DATA_DF is None else len(DATA_DF)
    cols = [] if DATA_DF is None else list(DATA_DF.columns)
    has_plate = PLATE_COLUMN in cols
    txt = [
        f"Rows: {rows}",
        f"Plate column used: {PLATE_COLUMN}",
        f"Has plate column: {has_plate}",
        "Columns:",
        *(str(c) for c in cols[:100]),
    ]
    out = "\n".join(txt)
    if len(out) <= MAX_LEN:
        await safe_send_text(update, out)
    else:
        bio = BytesIO(out.encode("utf-8"))
        bio.name = "debug.txt"
        try:
            await update.message.reply_document(bio)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            await update.message.reply_document(bio)

# ==== البحث ====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DATA_DF, PLATE_COLUMN
    try:
        text = (update.message.text or "").strip()
        logging.info(f"⬅ Received: {text}")

        if not text:
            await safe_send_text(update, "اكتب رقم السيارة حتى أبحث عنه.")
            return

        if DATA_DF is None or DATA_DF.empty:
            DATA_DF = load_data()
            if DATA_DF is None or DATA_DF.empty:
                await safe_send_text(update, "خطأ بتحميل الملفات. تأكد من وجودها بنفس المجلد.")
                return

        if PLATE_COLUMN not in DATA_DF.columns:
            await safe_send_text(update, f"خطأ: عمود '{PLATE_COLUMN}' غير موجود. اكتب /debug لمراجعة الأعمدة.")
            return

        key = _normalize_text(text)

        # مطابق أولاً ثم جزئي
        matches = DATA_DF[DATA_DF[PLATE_COLUMN] == key].copy()
        if matches.empty:
            matches = DATA_DF[DATA_DF[PLATE_COLUMN].str.contains(key, na=False)].copy()

        if matches.empty:
            await safe_send_text(update, f"ماكو معلومات للسيارة رقم: {text}")
            return

        matches = matches.drop_duplicates(subset=[PLATE_COLUMN], keep="first").fillna("")

        # نرسل نتيجة واحدة فقط لتفادي الفلود
        row = matches.iloc[0]
        msg = format_row(row, EXPECTED_COLUMNS_ORDER)
        if len(msg) > MAX_LEN:
            await send_in_chunks(update, msg)
        else:
            await safe_send_text(update, msg)

    except (BadRequest, Forbidden, TimedOut, NetworkError, RetryAfter) as e:
        logging.error(f"Telegram error: {type(e).__name__}: {e}")
        detail = f"{type(e).__name__}: {str(e)}"
        try:
            await safe_send_text(update, "خطأ إرسال:\n" + detail[:500])
        except Exception:
            pass
    except Exception as e:
        tb = traceback.format_exc()
        logging.error("Unhandled error:\n" + tb)
        try:
            await safe_send_text(update, "خطأ داخلي:\n" + str(e)[:500])
        except Exception:
            pass

# ==== التشغيل ====
if __name__ == "__main__":
    token = read_token()
    if not token:
        print("ضع التوكن في token.txt أو TELEGRAM_BOT_TOKEN.")
        raise SystemExit(1)

    DATA_DF = load_data()

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("البوت يعمل... افتح تيليغرام وارسل /start")
    app.run_polling()
