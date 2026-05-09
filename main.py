import asyncio, sqlite3, random, os, re
from datetime import datetime, timedelta
from difflib import get_close_matches
import yfinance as yf
import pandas as pd
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# ===================== KONFIGURASI =====================
TOKEN = "8686616468:AAHP8_Qhhhr-2O-rgO5LZGE6UnxvN60-K-g"
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
ADMIN_ID = 5526290677
DB_NAME = "alphaledger.db"
SPREADSHEET_NAME = "Log AlphaLedger"
SHEET_NAME = "Sheet1"

# Setup OCR
try:
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    OCR_READY = True
except Exception as e:
    OCR_READY = False
    print(f"⚠️ OCR tidak siap: {e}")

# Setup Google Sheets
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open(SPREADSHEET_NAME).worksheet(SHEET_NAME)
    GS_READY = True
except Exception as e:
    GS_READY = False
    print(f"⚠️ Google Sheets tidak siap: {e}")

# =================== DATABASE ===================
def setup_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT,
        is_premium INTEGER DEFAULT 0, expiry_date TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        symbol TEXT, trade_type TEXT, entry REAL,
        sl_pips REAL, tp_pips REAL, profit_pips REAL, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS promo (
        id INTEGER PRIMARY KEY CHECK (id=1), counter INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        alert_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        symbol TEXT, target_price REAL, created_at TEXT)''')
    c.execute("INSERT OR IGNORE INTO promo (id, counter) VALUES (1,0)")
    conn.commit(); conn.close()

def is_premium(uid):
    if uid == ADMIN_ID: return True
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT is_premium, expiry_date FROM users WHERE user_id=?", (uid,))
    row = c.fetchone(); conn.close()
    if not row or row[0] == 0: return False
    if row[1]:
        try:
            if datetime.strptime(row[1], "%Y-%m-%d") < datetime.now():
                conn = sqlite3.connect(DB_NAME); c = conn.cursor()
                c.execute("UPDATE users SET is_premium=0 WHERE user_id=?", (uid,))
                conn.commit(); conn.close()
                return False
        except: pass
    return True

def promo_count():
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT counter FROM promo WHERE id=1")
    row = c.fetchone(); conn.close()
    return row[0] if row else 0

def promo_inc():
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("UPDATE promo SET counter = counter + 1 WHERE id=1")
    conn.commit(); conn.close()

def set_premium(uid, uname, days):
    exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, username, is_premium, expiry_date, created_at) VALUES (?,?,1,?,?)",
              (uid, uname, exp, now))
    conn.commit(); conn.close()

def log_to_sheets(username, uid, paket, harga):
    if not GS_READY: return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([username, str(uid), paket, f"Rp{harga:,}", now])
    except Exception as e:
        print(f"Gagal tulis ke Sheets: {e}")

# =========== FUNGSI RSI & MACD MANUAL ===========
def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def compute_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

# =========== DAFTAR KODE POPULER ===========
ALL_CODES = {
    "Forex ": ["EURUSD ", "GBPUSD ", "USDJPY ", "AUDUSD ", "NZDUSD ", "USDCAD ", "USDCHF "],
    "Crypto ": ["BTC ", "ETH ", "XRP ", "SOL ", "ADA ", "DOGE ", "DOT ", "LINK ", "UNI ", "MATIC ", "SHIB ", "AVAX ", "LTC ", "BCH "],
    "Indeks ": ["SP500 ", "DOW ", "NASDAQ ", "FTSE ", "DAX ", "NIKKEI ", "HSI "],
    "Komoditas ": ["XAUUSD ", "XAGUSD ", "OIL ", "BRENT ", "NATGAS "],
    "Saham ID ": ["TLKM ", "BBCA ", "BBRI ", "ASII ", "UNVR ", "GOTO ", "ADRO ", "ANTM ", "ICBP ", "INDF ", "PGAS ", "PTBA ", "SMGR ", "KLBF ", "BMRI ", "BNGA ", "CPIN ", "EXCL ", "MNCN ", "PWON ", "SRIL ", "TOWR ", "WIKA ", "JSMR ", "PTPP ", "ADHI ", "AKRA ", "INKP ", "TKIM ", "MYOR ", "GGRM ", "HMSP ", "UNTR ", "ITMG ", "MEDC ", "ELSA ", "RAJA ", "ENRG ", "CTRA ", "LPKR ", "BSDE ", "PANI ", "DMAS ", "MDLN ", "MTDL ", "TAPG ", "ISAT ", "TBIG ", "TOBA ", "HRUM "]
}

# =========== FUNGSI RESOLUSI SIMBOL ===========
def resolve_symbol(user_input: str) -> tuple:
    s = user_input.upper().strip()
    if s in ("XAUUSD ", "XAUU ", "GOLD ", "EMAS "):
        return "GC=F ", "GLD ", 10.0
    if s in ("XAGUSD ", "SILVER ", "PERAK "):
        return "SI=F ", "SLV ", 1.0
    
    ALIASES = {
        "OIL ": "CL=F ", "WTI ": "CL=F ", "BRENT ": "BZ=F ", "NATGAS ": "NG=F ",
        "BTC ": "BTC-USD ", "ETH ": "ETH-USD ", "XRP ": "XRP-USD ", "SOL ": "SOL-USD ",
        "ADA ": "ADA-USD ", "DOGE ": "DOGE-USD ", "DOT ": "DOT-USD ", "LINK ": "LINK-USD ",
        "UNI ": "UNI-USD ", "MATIC ": "MATIC-USD ", "SHIB ": "SHIB-USD ",
        "AVAX ": "AVAX-USD ", "LTC ": "LTC-USD ", "BCH ": "BCH-USD ",
        "SPX ": "^GSPC ", "SP500 ": "^GSPC ", "DOW ": "^DJI ", "DJI ": "^DJI ",
        "NASDAQ ": "^IXIC ", "IXIC ": "^IXIC ", "FTSE ": "^FTSE ", "DAX ": "^GDAXI ",
        "NIKKEI ": "^N225 ", "HSI ": "^HSI ", "KOSPI ": "^KS11 ",
        "TLKM ": "TLKM.JK ", "BBCA ": "BBCA.JK ", "BBRI ": "BBRI.JK ", "ASII ": "ASII.JK ",
        "UNVR ": "UNVR.JK ", "GOTO ": "GOTO.JK ", "BUKA ": "BUKA.JK ", "ADRO ": "ADRO.JK ",
        "ANTM ": "ANTM.JK ", "ICBP ": "ICBP.JK ", "INDF ": "INDF.JK ", "PGAS ": "PGAS.JK ",
        "PTBA ": "PTBA.JK ", "SMGR ": "SMGR.JK ", "KLBF ": "KLBF.JK ", "BMRI ": "BMRI.JK ",
        "BNGA ": "BNGA.JK ", "CPIN ": "CPIN.JK ", "EXCL ": "EXCL.JK ", "MNCN ": "MNCN.JK ",
        "PWON ": "PWON.JK ", "SRIL ": "SRIL.JK ", "TOWR ": "TOWR.JK ", "WIKA ": "WIKA.JK ",
        "JSMR ": "JSMR.JK ", "PTPP ": "PTPP.JK ", "ADHI ": "ADHI.JK ", "AKRA ": "AKRA.JK ",
        "INKP ": "INKP.JK ", "TKIM ": "TKIM.JK ", "MYOR ": "MYOR.JK ", "GGRM ": "GGRM.JK ",
        "HMSP ": "HMSP.JK ", "UNTR ": "UNTR.JK ", "ITMG ": "ITMG.JK ", "MEDC ": "MEDC.JK ",
        "ELSA ": "ELSA.JK ", "RAJA ": "RAJA.JK ", "ENRG ": "ENRG.JK ", "CTRA ": "CTRA.JK ",
        "LPKR ": "LPKR.JK ", "BSDE ": "BSDE.JK ", "PANI ": "PANI.JK ", "DMAS ": "DMAS.JK ",
        "MDLN ": "MDLN.JK ", "MTDL ": "MTDL.JK ", "TAPG ": "TAPG.JK ", "ISAT ": "ISAT.JK ",
        "TBIG ": "TBIG.JK ", "TOBA ": "TOBA.JK ", "HRUM ": "HRUM.JK ", "GLD ": "GLD ",
    }
    
    if s in ALIASES:
        return ALIASES[s], None, 1.0
    if '-' in s or '.' in s or '=' in s:
        return s, None, 1.0
    forex_list = ALL_CODES["Forex "]
    if s in forex_list:
        return s + "=X ", None, 1.0
    if len(s) <= 4 and s.isalpha():
        return s + ".JK ", None, 1.0
    return s, None, 1.0

def get_closest_code(user_input: str):
    all_codes = []
    for k, v in ALL_CODES.items():
        all_codes.extend(v)
    all_codes.extend(["XAUUSD","OIL","BRENT","NATGAS","SP500","GLD"])
    matches = get_close_matches(user_input.upper(), [c.upper() for c in all_codes], n=3, cutoff=0.5)
    return matches

# =========== DOWNLOADER ===========
def get_price(primary: str, fallback: str = None, multiplier: float = 1.0):
    data = yf.download(primary, period="1d", progress=False)
    if not data.empty:
        return float(data['Close'].values.flat[-1])
    try:
        tick = yf.Ticker(primary)
        hist = tick.history(period="1d")
        if not hist.empty: return float(hist['Close'].iloc[-1])
    except: pass
    if fallback:
        data = yf.download(fallback, period="1d", progress=False)
        if not data.empty: return float(data['Close'].values.flat[-1]) * multiplier
    return None

def get_history(primary: str, fallback: str = None, period="60d"):
    df = yf.download(primary, period=period, progress=False)
    if not df.empty: return df
    try:
        tick = yf.Ticker(primary)
        hist = tick.history(period=period)
        if not hist.empty: return hist
    except: pass
    if fallback:
        df = yf.download(fallback, period=period, progress=False)
        if not df.empty: return df
    return pd.DataFrame()

# =================== HANDLER DASAR ===================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f" Selamat datang di AlphaLedger!\n\n"
        f"📊 Jurnal trading cerdas + sinyal teknikal + mindset trader.\n"
        f"Catat setiap trade, lihat performamu, dan dapatkan ide trading.\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📖 PANDUAN LENGKAP\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📈 /price [kode] — Cek harga (contoh: /price EURUSD, /price XAUUSD)\n"
        f" /signal [kode] — Sinyal teknikal (contoh: /signal GBPUSD)\n"
        f"📐 /trade — Catat trading (contoh: /trade EURUSD buy 1.0850 20 40 TP)\n"
        f"📊 /stats — Statistik performa\n"
        f"🧠 /mindset — Motivasi gratis\n"
        f"🔓 /upgrade — Upgrade premium\n\n"
        f" FITUR BARU:\n"
        f"📱 /menu — Menu interaktif\n"
        f"📋 /kode — Daftar kode yang didukung\n"
        f" /status — Cek status premium\n"
        f"🔔 /alert — Pasang pengingat harga (contoh: /alert XAUUSD 4800)\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"️ Hampir semua fitur hanya untuk premium.\n\n"
        f"🔥 Promo Launching (10 pembeli pertama):\n"
        f"Bulanan Rp20.000 | Selamanya Rp35.000"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start /help /price /signal /trade /stats /mindset /upgrade /menu /kode /status /alert")

async def mindset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = ["💡 \"Pasar bukan tempat untuk berharap.\"","💡 \"Trader sukses belajar dari kerugian.\"","💡 \"Konsistensi adalah kunci profit.\""]
    await update.message.reply_text(random.choice(q))

async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_premium(update.effective_user.id):
        await update.message.reply_text("✅ Kamu sudah premium!")
        return
    left = 10 - promo_count()
    kb = [[InlineKeyboardButton("🔥 Bulanan (Rp20.000)" if left > 0 else " Bulanan (Rp35.000)", callback_data="pilih_bulanan")],
          [InlineKeyboardButton("💎 Selamanya (Rp35.000)" if left > 0 else "👑 Selamanya (Rp50.000)", callback_data="pilih_selamanya")]]
    markup = InlineKeyboardMarkup(kb)
    if left > 0:
        txt = (f"🔐 Upgrade Premium\n\n"
               f" Promo ({left} slot tersisa!)\n"
               f"Klik tombol untuk pilih:\n\n"
               f"💼 Harga normal: Bulanan Rp35.000 | Selamanya Rp50.000")
    else:
        txt = "💼 Harga Normal\nKlik tombol untuk pilih:"
    await update.message.reply_text(txt, reply_markup=markup, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if is_premium(query.from_user.id):
        await query.message.reply_text("✅ Kamu sudah premium!")
        return
    
    data = query.data
    left = 10 - promo_count()
    
    if data == "pilih_bulanan":
        harga = 20000 if left > 0 else 35000
        pkt = f"Bulanan (Rp{harga:,})"
        dur = "30 hari"
    else:
        harga = 35000 if left > 0 else 50000
        pkt = f"Selamanya (Rp{harga:,})"
        dur = "selamanya"
        
    if not os.path.exists("QR PAYMENT.jpg"):
        await query.message.reply_text("❌ QR pembayaran belum disiapkan.")
        return

    context.user_data["harga"] = harga
    context.user_data["durasi"] = dur
    
    with open("QR PAYMENT.jpg", "rb") as f:
        await query.message.reply_photo(f, caption=(
            f"🔐 {pkt}\nMasa aktif: {dur}\n\n"
            f"💳 Transfer Rp{harga:,} ke QR.\n"
            f"📸 Upload bukti transfer di sini."
        ), parse_mode="Markdown")

# ==========================================
# 🧠 LOGIKA VERIFIKASI PEMBAYARAN SMART OCR
# ==========================================

def verifikasi_pembayaran(teks_ocr: str, harga_harapan: int) -> int:
    """
    Mengekstrak nominal pembayaran dengan toleransi error OCR.
    Return: Nominal yang terdeteksi (integer), atau 0 jika gagal.
    """
    # 1. Ekstrak semua angka dari teks
    # Regex ini menangkap angka seperti 20.000, 20000, 20,000
    raw_numbers = re.findall(r'[\d]+[.,]?[\d]+', teks_ocr)
    
    kandidat_nominal = []
    for raw in raw_numbers:
        try:
            # Bersihkan koma/titik untuk jadi integer
            clean = raw.replace(',', '').replace('.', '')
            nilai = int(clean)
            # Filter angka masuk akal (antara 5.000 sampai 100.000)
            # Ini mengabaikan jam (misal 14:30 -> 1430) atau tanggal
            if 5000 <= nilai <= 100000:
                kandidat_nominal.append(nilai)
        except:
            continue

    if not kandidat_nominal:
        return 0

    # 2. Cari angka yang PALING DEKAT dengan harga_harapan
    # Contoh: Harga 20.000. OCR baca 19.800 dan 500.000 (saldo).
    # 19.800 selisihnya 200. 500.000 selisihnya 480.000. Bot pilih 19.800.
    nominal_terdekat = min(kandidat_nominal, key=lambda x: abs(x - harga_harapan))
    
    return nominal_terdekat

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OCR_READY:
        await update.message.reply_text("❌ OCR belum dikonfigurasi admin.")
        return
    
    uid = update.effective_user.id
    uname = update.effective_user.username or "TanpaUsername"
    harga_harapan = context.user_data.get("harga")
    
    if not harga_harapan:
        await update.message.reply_text("️ Klik /upgrade dulu sebelum upload bukti.")
        return
    
    file = await update.message.photo[-1].get_file()
    path = f"bukti_{uid}.jpg"
    await file.download_to_drive(path)
    
    try:
        img = Image.open(path)
        teks = pytesseract.image_to_string(img, lang="ind+eng")
        
        # Gunakan Smart OCR
        nominal_terdeteksi = verifikasi_pembayaran(teks, harga_harapan)
        
        if nominal_terdeteksi == 0:
            await update.message.reply_text(
                "❌ Nominal tidak terbaca jelas. Pastikan screenshot mencakup angka nominal pembayaran."
            )
            return

        # Cek toleransi (selisih < 3000 dianggap valid)
        # Misal bayar 20.000, terbaca 19.500 -> selisih 500 -> VALID
        selisih = abs(nominal_terdeteksi - harga_harapan)
        
        if selisih <= 3000:
            # ✅ BERHASIL VERIFIKASI
            dur = context.user_data.get("durasi", "30 hari")
            days = 30 if dur == "30 hari" else 36500
            set_premium(uid, uname, days)
            
            if promo_count() < 10:
                promo_inc()
            
            log_to_sheets(uname, uid, "Bulanan" if dur == "30 hari" else "Selamanya", harga_harapan)
            
            await context.bot.send_message(
                ADMIN_ID,
                f"🔔 Pembeli Baru!\n"
                f"Username: @{uname}\nID: {uid}\n"
                f"Paket: {dur}\n"
                f"Terbaca: Rp{nominal_terdeteksi:,} | Harapan: Rp{harga_harapan:,}",
                parse_mode="Markdown"
            )
            
            await update.message.reply_text(
                f"✅ Pembayaran Rp{nominal_terdeteksi:,} TERVERIFIKASI!\n"
                f" Akses premium aktif ({dur}). Selamat trading!"
            )
            context.user_data.pop("harga", None) # Reset state
            
        else:
            # ❌ SELISIH TERLALU JAUH
            await update.message.reply_text(
                f"❌ Nominal tidak sesuai.\n"
                f"Terbaca: Rp{nominal_terdeteksi:,}\n"
                f"Seharusnya: Rp{harga_harapan:,}\n\n"
                f"Silakan upload ulang screenshot yang benar."
            )
            
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal verifikasi: {e}")
    finally:
        if os.path.exists(path): os.remove(path)

# =============== FITUR TRADING ===============
async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_premium(update.effective_user.id):
        await update.message.reply_text("❌ Kamu belum premium. Ketik /upgrade.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Gunakan: /price [kode]\nContoh: /price EURUSD")
        return
    raw = context.args[0]
    primary, fallback, mult = resolve_symbol(raw)
    last_price = get_price(primary, fallback, mult)
    if last_price is None:
        suggestions = get_closest_code(raw)
        extra = f"\n Mungkin maksud Anda: {', '.join(suggestions)}?" if suggestions else ""
        await update.message.reply_text(f"❌ Kode '{raw}' tidak ditemukan.{extra}")
        return
    info = ""
    if raw.upper() in ("XAUUSD ", "XAUU ", "GOLD ", "EMAS "):
        info = "\nℹ️ Harga mengacu pada Gold Futures (GC=F), mungkin sedikit berbeda dari harga spot."
    await update.message.reply_text(f"📈 Harga {raw.upper()} saat ini: {last_price:,.2f} USD{info}")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_premium(update.effective_user.id):
        await update.message.reply_text("❌ Kamu belum premium. Ketik /upgrade.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Gunakan: /signal [kode]\nContoh: /signal GBPUSD")
        return
    raw = context.args[0]
    primary, fallback, mult = resolve_symbol(raw)
    df = get_history(primary, fallback, "60d")
    if df.empty:
        suggestions = get_closest_code(raw)
        extra = f"\n💡 Mungkin maksud Anda: {', '.join(suggestions)}?" if suggestions else ""
        await update.message.reply_text(f"❌ Kode '{raw}' tidak ditemukan.{extra}")
        return
    close = df['Close']
    if isinstance(close, pd.DataFrame): close = close.iloc[:, 0]
    close = pd.Series(close.values.ravel(), index=close.index)
    rsi_series = compute_rsi(close, 14); rsi_now = rsi_series.iloc[-1]
    macd_line, signal_line = compute_macd(close, 12, 26, 9)
    macd_now = macd_line.iloc[-1]; signal_now = signal_line.iloc[-1]
    resp = f"🚦 Analisis Sinyal untuk {raw.upper()}\nRSI (14): {rsi_now:.2f}\n"
    if rsi_now < 30: resp += "Status: Oversold (potensi BUY)\n"
    elif rsi_now > 70: resp += "Status: Overbought (potensi SELL)\n"
    else: resp += "Status: Netral\n"
    if macd_now > signal_now: resp += "MACD: Bullish (Golden Cross)\n"
    elif macd_now < signal_now: resp += "MACD: Bearish (Dead Cross)\n"
    else: resp += "MACD: No cross\n"
    if rsi_now < 30 and macd_now > signal_now: resp += "💡 Kesimpulan: STRONG BUY"
    elif rsi_now > 70 and macd_now < signal_now: resp += "💡 Kesimpulan: STRONG SELL"
    elif rsi_now < 30: resp += "💡 Kesimpulan: BUY (tunggu konfirmasi MACD)"
    elif rsi_now > 70: resp += "💡 Kesimpulan: SELL (tunggu konfirmasi MACD)"
    elif macd_now > signal_now: resp += "💡 Kesimpulan: Potensi BUY (konfirmasi RSI)"
    elif macd_now < signal_now: resp += "💡 Kesimpulan: Potensi SELL (konfirmasi RSI)"
    else: resp += "💡 Kesimpulan: HOLD / tunggu sinyal"
    await update.message.reply_text(resp)

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_premium(update.effective_user.id):
        await update.message.reply_text("❌ Kamu belum premium. Ketik /upgrade.")
        return
    if len(context.args) != 6:
        await update.message.reply_text("️ Format: /trade [kode] [buy/sell] [entry] [SL_pips] [TP_pips] [TP/SL]\nContoh: /trade EURUSD buy 1.0850 20 40 TP")
        return
    symbol, tipe, entry_str, sl_str, tp_str, hasil = context.args
    try:
        entry = float(entry_str); sl_pips = float(sl_str); tp_pips = float(tp_str)
    except ValueError:
        await update.message.reply_text("❌ Entry, SL, TP harus angka."); return
    tipe = tipe.lower(); hasil = hasil.upper()
    if tipe not in ["buy", "sell"]:
        await update.message.reply_text("❌ Tipe harus buy atau sell."); return
    if hasil not in ["TP", "SL"]:
        await update.message.reply_text("❌ Hasil harus TP atau SL."); return
    profit_pips = tp_pips if (tipe == "buy" and hasil == "TP") or (tipe == "sell" and hasil == "TP") else -sl_pips
    uid = update.effective_user.id; date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT INTO trades (user_id, symbol, trade_type, entry, sl_pips, tp_pips, profit_pips, date) VALUES (?,?,?,?,?,?,?,?)",
              (uid, symbol.upper(), tipe, entry, sl_pips, tp_pips, profit_pips, date_now))
    conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Trade tercatat!\nSymbol: {symbol.upper()}\nEntry: {entry}\nSL: {sl_pips} pips | TP: {tp_pips} pips\nHasil: {'✅ Profit' if hasil == 'TP' else '❌ Loss'}\nPips: {profit_pips:+} pips")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_premium(update.effective_user.id):
        await update.message.reply_text("❌ Kamu belum premium. Ketik /upgrade.")
        return
    uid = update.effective_user.id
    period = "all" if (context.args and context.args[0].lower() == "all") else "month"
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    if period == "all": c.execute("SELECT profit_pips FROM trades WHERE user_id=?", (uid,))
    else:
        month_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        c.execute("SELECT profit_pips FROM trades WHERE user_id=? AND date >= ?", (uid, month_start))
    trades = c.fetchall(); conn.close()
    if not trades:
        await update.message.reply_text(" Belum ada trade."); return
    profits = [t[0] for t in trades]; total = sum(profits)
    win = len([p for p in profits if p > 0]); loss = len([p for p in profits if p < 0])
    total_trades = len(profits); winrate = win / total_trades * 100 if total_trades else 0
    best = max(profits); worst = min(profits); avg = total / total_trades
    await update.message.reply_text(
        f"📊 Statistik Trading ({'Seluruh Waktu' if period == 'all' else 'Bulan Ini'}):\n"
        f"Total trade: {total_trades}\nWin rate: {winrate:.2f}% ({win}W / {loss}L)\n"
        f"Total P/L: {total:+.2f} pips\nRata-rata: {avg:+.2f} pips\n"
        f"Best trade: {best:+.2f} pips\nWorst trade: {worst:+.2f} pips"
    )

# =============== FITUR BARU ===============
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        await update.message.reply_text("✅ Status: Premium Selamanya (Admin)")
        return
    if not is_premium(uid):
        await update.message.reply_text("❌ Kamu belum premium. Ketik /upgrade.")
        return
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT expiry_date FROM users WHERE user_id=?", (uid,))
    row = c.fetchone(); conn.close()
    if row and row[0]:
        try:
            exp = datetime.strptime(row[0], "%Y-%m-%d")
            sisa = (exp - datetime.now()).days
            if sisa > 365*10: txt = "✅ Status: Premium Selamanya (Lifetime)"
            elif sisa > 0: txt = f"✅ Status: Premium\n⏳ Sisa: {sisa} hari (sampai {row[0]})"
            else: txt = "❌ Masa premium telah habis."
        except:
            txt = "✅ Status: Premium"
    else:
        txt = " Kamu belum premium."
    await update.message.reply_text(txt)

async def kode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "📋 Daftar Kode yang Didukung\n\n"
    for cat, codes in ALL_CODES.items():
        txt += f"• {cat}: {', '.join(codes[:7])}{'...' if len(codes)>7 else ''}\n"
    txt += "\nContoh: /price XAUUSD, /signal BTC, /trade EURUSD"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_premium(update.effective_user.id):
        await update.message.reply_text("❌ Kamu belum premium. Ketik /upgrade.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Format: /alert [kode] [harga]\nContoh: /alert XAUUSD 4800")
        return
    raw, price_str = context.args[0], context.args[1]
    try:
        target = float(price_str)
    except ValueError:
        await update.message.reply_text("❌ Harga harus angka.")
        return
    symbol = raw.upper()
    uid = update.effective_user.id
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT INTO alerts (user_id, symbol, target_price, created_at) VALUES (?,?,?,?)",
              (uid, symbol, target, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit(); conn.close()
    await update.message.reply_text(f" Alert dipasang: {symbol} menyentuh {target:,.2f}.")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT alert_id, user_id, symbol, target_price FROM alerts")
    alerts = c.fetchall()
    for alert in alerts:
        alert_id, user_id, sym, target = alert
        try:
            current = get_price(sym, multiplier=1.0)
            if current is None: continue
            if (current >= target and target > 0) or (current <= target and target < 0):
                await context.bot.send_message(user_id, f"🚨 Alert Tercapai!\n{sym} sekarang di {current:,.2f} (target {target:,.2f})", parse_mode="Markdown")
                c.execute("DELETE FROM alerts WHERE alert_id=?", (alert_id,))
                conn.commit()
        except: continue
    conn.close()

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📈 Cek Harga", callback_data="menu_price")],
        [InlineKeyboardButton("🚦 Sinyal Teknikal", callback_data="menu_signal")],
        [InlineKeyboardButton("📐 Catat Trading", callback_data="menu_trade")],
        [InlineKeyboardButton("📊 Statistik", callback_data="menu_stats")],
        [InlineKeyboardButton("🔔 Pasang Alert", callback_data="menu_alert")],
        [InlineKeyboardButton("📋 Kode Populer", callback_data="menu_kode")],
        [InlineKeyboardButton("⏳ Status Premium", callback_data="menu_status")],
        [InlineKeyboardButton("🔓 Upgrade", callback_data="menu_upgrade")]
    ]
    await update.message.reply_text(" Menu AlphaLedger\nPilih fitur yang ingin digunakan:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "menu_price":
        await query.message.reply_text("Gunakan /price [kode], contoh: /price EURUSD")
    elif data == "menu_signal":
        await query.message.reply_text("Gunakan /signal [kode], contoh: /signal XAUUSD")
    elif data == "menu_trade":
        await query.message.reply_text("Gunakan /trade, contoh: /trade EURUSD buy 1.0850 20 40 TP")
    elif data == "menu_stats":
        await query.message.reply_text("Gunakan /stats atau /stats all")
    elif data == "menu_alert":
        await query.message.reply_text("Gunakan /alert [kode] [harga], contoh: /alert XAUUSD 4800")
    elif data == "menu_kode":
        await kode_cmd(query, context)
    elif data == "menu_status":
        await status_cmd(query, context)
    elif data == "menu_upgrade":
        await upgrade_cmd(query, context)

# =============== ADMIN ===============
async def listbuyer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not GS_READY: await update.message.reply_text("❌ Google Sheets belum terhubung."); return
    try:
        records = sheet.get_all_values()[1:]
        if not records: await update.message.reply_text("📭 Belum ada pembeli."); return
        txt = "📋 Daftar Pembeli Terbaru:\n"
        for r in records[-5:]: txt += f"• @{r[0]} - {r[2]} ({r[3]})\n"
        await update.message.reply_text(txt, parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f" Gagal: {e}")

async def kuota_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    left = 10 - promo_count()
    await update.message.reply_text(f"🔥 Sisa kuota promo: {left}")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        qty = int(context.args[0])
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        c.execute("UPDATE promo SET counter = ? WHERE id = 1", (10 - qty,))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Kuota promo di-reset ke {qty}.")
    except: await update.message.reply_text("️ Format: /resetkuota [jumlah]")

# =================== MAIN ===================
def main():
    setup_db()
    app = Application.builder().token(TOKEN).build()
    # Dasar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mindset", mindset_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade_cmd))
    # Trading
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    # Baru
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("kode", kode_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    # Admin
    app.add_handler(CommandHandler("listbuyer", listbuyer_cmd))
    app.add_handler(CommandHandler("kuota", kuota_cmd))
    app.add_handler(CommandHandler("resetkuota", reset_cmd))
    # Callback
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(menu_handler, pattern="^menu_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Alert checker
    async def alert_job(callback_context): await check_alerts(callback_context)
    jq = app.job_queue
    if jq: jq.run_repeating(alert_job, interval=120, first=10)
    print("✅ AlphaLedger berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
