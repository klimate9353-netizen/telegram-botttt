import os
import re
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# yt-dlp (YouTube/TikTok/Instagram download)
try:
    from yt_dlp import YoutubeDL
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "yt-dlp import bo‘lmadi. requirements.txt ga 'yt-dlp' qo‘shing va qayta deploy qiling.\n"
        f"Import error: {e}"
    )

# -------------------- LOGGING --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("downloader-bot")

# -------------------- ENV (Railway/Generic) --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. Railway Variables ichiga BOT_TOKEN qo‘ying.")

# Railway Web Service bo‘lsa PORT bo‘ladi (ko‘pincha 8080)
PORT = int(os.getenv("PORT", "8080"))

# Webhook ixtiyoriy. Agar WEBHOOK_URL bermasangiz bot polling’da ishlaydi.
# WEBHOOK_URL misol: https://your-railway-domain.up.railway.app
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret").strip()
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "").strip().lower() in ("1", "true", "yes", "y")

# Telegramga yuboriladigan maksimal fayl (MB)
MAX_MB = int(os.getenv("MAX_MB", "50"))
MAX_BYTES = MAX_MB * 1024 * 1024

# -------------------- COOKIES (YouTube auth) --------------------
# Railway’da eng oson yo‘l:
# 1) cookies.txt faylini repoga qo‘ymang (maxfiy). 
# 2) Railway Volume ishlatsangiz, masalan /data ga mount qiling va COOKIES_PATH=/data/cookies.txt qiling.
# 3) Yoki test uchun cookies ishlatmasangiz ham bo‘ladi.
COOKIES_PATH = os.getenv("COOKIES_PATH", "").strip()  # masalan: /data/cookies.txt
USE_COOKIES = bool(COOKIES_PATH) and os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0

# Brauzerga o‘xshash User-Agent ba’zi holatlarda bloklarni kamaytiradi
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
).strip()

# Proxy (ixtiyoriy) — agar IP bo‘yicha cheklov bo‘lsa
# format: http://user:pass@host:port  yoki  socks5://host:port
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# -------------------- MEMORY (pending choices) --------------------
@dataclass
class PendingChoice:
    url: str
    extractor: str  # "youtube"
    formats: Dict[str, dict]  # token -> format info

PENDING: Dict[Tuple[int, int], PendingChoice] = {}  # (chat_id, user_id) -> PendingChoice

# -------------------- URL / PLATFORM --------------------
URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    return "unknown"

# -------------------- yt-dlp helpers --------------------
def _ydl_common_opts(outtmpl: str) -> dict:
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "http_headers": {
            "User-Agent": USER_AGENT,
        },
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    if USE_COOKIES:
        opts["cookiefile"] = COOKIES_PATH
    return opts

def extract_info(url: str) -> dict:
    with YoutubeDL(_ydl_common_opts(outtmpl="%(id)s.%(ext)s") | {"skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)

def _format_filesize(fmt: dict) -> Optional[int]:
    fs = fmt.get("filesize")
    if fs is None:
        fs = fmt.get("filesize_approx")
    return fs

def build_youtube_choice_list(info: dict) -> List[dict]:
    """
    YouTube uchun:
    - mp4
    - progressive (audio+video)
    - MAX_MB ichida (filesize ma’lum bo‘lsa)
    """
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue

        fs = _format_filesize(f)
        if fs is not None and fs > MAX_BYTES:
            continue

        height = f.get("height") or 0
        candidates.append((height, fs or 10**18, f))

    candidates.sort(key=lambda x: (x[0], x[1]))

    # har bir height uchun eng kichik hajmli formatni qoldiramiz
    by_height: Dict[int, dict] = {}
    for height, fs, f in candidates:
        if height <= 0:
            continue
        if height not in by_height:
            by_height[height] = f
        else:
            old = by_height[height]
            old_fs = _format_filesize(old) or 10**18
            new_fs = _format_filesize(f) or 10**18
            if new_fs < old_fs:
                by_height[height] = f

    return [by_height[h] for h in sorted(by_height.keys())]

def choose_best_under_limit_non_reencode(info: dict) -> Optional[dict]:
    """
    TikTok/Instagram uchun avtomatik:
    - mp4
    - progressive (audio+video)
    - limit ichida (filesize ma’lum bo‘lsa)
    Eng yuqori height, lekin limitga mosini tanlaydi.
    """
    formats = info.get("formats") or []
    best = None
    best_score = (-1, -1)  # (height, -filesize)
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue

        fs = _format_filesize(f)
        if fs is not None and fs > MAX_BYTES:
            continue

        height = f.get("height") or 0
        fs_score = fs if fs is not None else 10**18
        score = (height, -fs_score)
        if score > best_score:
            best = f
            best_score = score
    return best

def download_format(url: str, format_id: str, workdir: str) -> str:
    """
    format_id bilan yuklab oladi. Re-encode qilmaydi.
    """
    outtmpl = os.path.join(workdir, "%(id)s.%(ext)s")
    opts = _ydl_common_opts(outtmpl=outtmpl) | {
        "format": format_id,
        "postprocessors": [],  # re-encode yo‘q
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if isinstance(info, dict) and info.get("requested_downloads"):
        fp = info["requested_downloads"][0].get("filepath")
        if fp and os.path.exists(fp):
            return fp

    # fallback: workdir’dan mp4 topamiz
    for name in os.listdir(workdir):
        if name.lower().endswith(".mp4"):
            return os.path.join(workdir, name)

    raise RuntimeError("Download bo‘ldi, lekin mp4 fayl topilmadi.")

# -------------------- Telegram handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! YouTube/TikTok/Instagram havolasini yuboring.\n\n"
        f"Limit: max {MAX_MB}MB.\n"
        "- YouTube: format tanlaysiz.\n"
        "- TikTok/Instagram: avtomatik mos format tanlanadi.\n"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Foydalanish:\n"
        "1) Havola yuboring.\n"
        "2) YouTube bo‘lsa format tugmalaridan tanlang.\n"
        f"3) TikTok/Instagram bo‘lsa bot {MAX_MB}MB ichida eng mos mp4 ni yuboradi.\n"
        "\nEslatma: Bot videoni qayta kodlamaydi."
    )

def _pretty_btn_label(fmt: dict) -> str:
    height = fmt.get("height") or 0
    fs = _format_filesize(fmt)
    if fs is None:
        return f"{height}p (size?)"
    return f"{fs/1024/1024:.1f}MB, {height}p"

def _make_token(i: int) -> str:
    return f"f{i}"

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or ""
    m = URL_RE.search(text)
    if not m:
        return
    url = m.group(1).strip()

    platform = detect_platform(url)
    if platform == "unknown":
        await msg.reply_text("Bu link tanilmadi. YouTube/TikTok/Instagram link yuboring.")
        return

    await msg.chat.send_action(ChatAction.TYPING)

    try:
        info = await asyncio.to_thread(extract_info, url)
    except Exception as e:
        log.exception("extract_info error")
        await msg.reply_text(f"Linkni o‘qishda xatolik: {e}")
        return

    # YouTube: format tanlash
    if platform == "youtube":
        fmts = build_youtube_choice_list(info)
        if not fmts:
            await msg.reply_text(
                f"YouTube’dan {MAX_MB}MB ichida yuboriladigan mp4 topilmadi.\n"
                "Video juda katta bo‘lishi mumkin yoki login/cookie talab qilishi mumkin."
            )
            return

        fmt_map: Dict[str, dict] = {}
        buttons = []
        for idx, f in enumerate(fmts[:12]):
            token = _make_token(idx)
            fmt_map[token] = f
            buttons.append(
                InlineKeyboardButton(
                    text=_pretty_btn_label(f),
                    callback_data=f"YT|{token}",
                )
            )

        # 2 ustunli klaviatura
        keyboard = []
        row = []
        for b in buttons:
            row.append(b)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="CANCEL")])

        PENDING[(msg.chat_id, msg.from_user.id)] = PendingChoice(
            url=url,
            extractor="youtube",
            formats=fmt_map,
        )

        title = info.get("title") or "YouTube video"
        await msg.reply_text(
            f"🎬 {title}\nFormatni tanlang (max {MAX_MB}MB):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # TikTok / Instagram: avtomatik
    if platform in ("tiktok", "instagram"):
        best = choose_best_under_limit_non_reencode(info)
        if not best:
            await msg.reply_text(
                f"{platform.title()} uchun {MAX_MB}MB ichida mos mp4 topilmadi.\n"
                "Video katta bo‘lishi, private bo‘lishi yoki login/cookie talab qilishi mumkin."
            )
            return
        format_id = str(best.get("format_id"))
        await send_downloaded_video(context, update.effective_chat.id, url, format_id, platform.title())
        return

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "CANCEL":
        PENDING.pop((q.message.chat_id, q.from_user.id), None)
        await q.edit_message_text("Bekor qilindi.")
        return

    if data.startswith("YT|"):
        token = data.split("|", 1)[1]
        pending = PENDING.get((q.message.chat_id, q.from_user.id))
        if not pending:
            await q.edit_message_text("Sessiya topilmadi. Qaytadan YouTube link yuboring.")
            return
        fmt = pending.formats.get(token)
        if not fmt:
            await q.edit_message_text("Format topilmadi. Qaytadan link yuboring.")
            return

        url = pending.url
        format_id = str(fmt.get("format_id"))
        PENDING.pop((q.message.chat_id, q.from_user.id), None)

        await q.edit_message_text("Yuklab olinmoqda…")
        await send_downloaded_video(context, q.message.chat_id, url, format_id, "YouTube")
        return

    await q.edit_message_text("Noma’lum amal.")

async def send_downloaded_video(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    format_id: str,
    platform_title: str,
):
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        with tempfile.TemporaryDirectory() as td:
            file_path = await asyncio.to_thread(download_format, url, format_id, td)
            size = os.path.getsize(file_path)

            if size > MAX_BYTES:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Video {size/1024/1024:.1f}MB chiqdi — limit {MAX_MB}MB. Pastroq format tanlang.",
                )
                return

            caption = f"{platform_title} yuklab olindi: {size/1024/1024:.1f}MB"
            with open(file_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                )

    except Exception as e:
        log.exception("download/send error")
        await context.bot.send_message(chat_id=chat_id, text=f"Xatolik: {e}")

# -------------------- Build app --------------------
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    return app

# -------------------- Webhook helpers --------------------
async def _post_init_set_webhook(app: Application):
    if not WEBHOOK_URL:
        return
    webhook_full = f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_SECRET}"
    try:
        await app.bot.set_webhook(url=webhook_full, drop_pending_updates=True)
        log.info("Webhook set: %s", webhook_full)
    except Exception:
        log.exception("Webhook set error")

async def _post_init_delete_webhook(app: Application):
    # Polling rejimida webhook o‘rnatilgan bo‘lsa, o‘chirib qo‘yamiz (ba’zan shart)
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (polling mode).")
    except Exception:
        log.exception("delete_webhook error (ignored)")

def main():
    log.info(
        "Cookies: path=%s use=%s (exists=%s size=%s)",
        COOKIES_PATH or "(not set)",
        USE_COOKIES,
        os.path.exists(COOKIES_PATH) if COOKIES_PATH else False,
        (os.path.getsize(COOKIES_PATH) if COOKIES_PATH and os.path.exists(COOKIES_PATH) else 0),
    )
    log.info("Proxy: %s", PROXY_URL or "(none)")

    app = build_app()

    # Webhook: faqat WEBHOOK_URL bo‘lsa yoki USE_WEBHOOK=1 bo‘lsa
    if WEBHOOK_URL or USE_WEBHOOK:
        if not WEBHOOK_URL:
            raise RuntimeError("Webhook rejimi uchun WEBHOOK_URL kerak. Railway domain URL’ingizni WEBHOOK_URL ga qo‘ying.")

        app.post_init = _post_init_set_webhook
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_SECRET,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_SECRET}",
            close_loop=False,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # Railway’da odatda polling (Worker) eng oson va stabil
        app.post_init = _post_init_delete_webhook
        app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
