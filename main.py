import os
import re
import json
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from yt_dlp import YoutubeDL

# -------------------- CONFIG --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("downloader-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env topilmadi. Railway Variables ga BOT_TOKEN qo‘ying.")

MAX_BYTES = 50 * 1024 * 1024  # 50MB
MAX_MB = 50

# Callback data limit (Telegram) ~64 bytes; shuning uchun qisqa token ishlatamiz.
# memory-only mapping (restart bo‘lsa yo‘qoladi). Istasangiz keyin DB qo‘shamiz.
@dataclass
class PendingChoice:
    url: str
    extractor: str  # "youtube" | "tiktok" | "instagram"
    formats: Dict[str, dict]  # token -> format dict (yt-dlp format info)

PENDING: Dict[Tuple[int, int], PendingChoice] = {}  # (chat_id, user_id) -> PendingChoice


# -------------------- URL / PLATFORM DETECTION --------------------
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
    return {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Railway/Server uchun: foydasiz metadata yozishni kamaytirish
        "restrictfilenames": True,
        "consoletitle": False,
        # Ba’zan tarmoq sekin bo‘lsa:
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
    }

def extract_info(url: str) -> dict:
    # Extract-only (download emas)
    with YoutubeDL(_ydl_common_opts(outtmpl="%(id)s.%(ext)s") | {"skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)

def _format_filesize(fmt: dict) -> Optional[int]:
    # filesize yoki filesize_approx qaytaradi
    fs = fmt.get("filesize")
    if fs is None:
        fs = fmt.get("filesize_approx")
    return fs

def build_youtube_choice_list(info: dict) -> List[dict]:
    """
    YouTube uchun Telegramga yuborishga qulay variantlar:
    - mp4
    - video+audio (progressive): vcodec != none va acodec != none
    - filesize <= 50MB (ma’lum bo‘lsa)
    """
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            # Bu alohida video/audio bo‘lishi mumkin; ffmpeg merge talab qilishi mumkin.
            # 50MB cheklov + soddalik uchun progressive formatlarni afzal ko‘ramiz.
            continue

        fs = _format_filesize(f)
        # Ba’zan fs None bo‘ladi; bunday holatda ehtiyotkorlik bilan qabul qilamiz,
        # lekin keyin yuklash paytida real fayl kattaligini tekshiramiz.
        if fs is not None and fs > MAX_BYTES:
            continue

        height = f.get("height") or 0
        # faqat rezolyutsiya bo‘yicha chiroyli tartib
        candidates.append((height, fs or 10**18, f))

    # height bo‘yicha, so‘ng kichikroq fayl
    candidates.sort(key=lambda x: (x[0], x[1]))
    # bir xil height uchun eng yaxshilaridan bittadan qoldiramiz
    by_height = {}
    for height, fs, f in candidates:
        if height not in by_height:
            by_height[height] = f
        else:
            # agar shu heightda fayl kichikroq bo‘lsa, almashtiramiz
            old = by_height[height]
            old_fs = _format_filesize(old) or 10**18
            new_fs = _format_filesize(f) or 10**18
            if new_fs < old_fs:
                by_height[height] = f

    # 144..1080 tartibida chiqarish
    result = [by_height[h] for h in sorted(by_height.keys()) if h > 0]
    return result

def choose_best_under_50mb_non_reencode(info: dict) -> Optional[dict]:
    """
    TikTok/Instagram (va boshqa) uchun avtomatik tanlov:
    - mp4
    - progressive (audio+video)
    - filesize <= 50MB (ma’lum bo‘lsa)
    Eng kattaroq rezolyutsiyali, lekin 50MB ichida bo‘lganini tanlaydi.
    """
    formats = info.get("formats") or []
    best = None
    best_score = (-1, -1)  # (height, -filesize)  -> height katta bo‘lsin, filesize kichik bo‘lsin
    for f in formats:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue

        fs = _format_filesize(f)
        if fs is not None and fs > MAX_BYTES:
            continue

        height = f.get("height") or 0
        # filesize None bo‘lsa, uni katta deb hisoblaymiz (xavfli), pastroq prioritet
        fs_score = fs if fs is not None else (10**18)
        score = (height, -fs_score)
        if score > best_score:
            best = f
            best_score = score
    return best

def download_format(url: str, format_id: str, workdir: str) -> str:
    """
    Berilgan format_id bilan yuklab oladi, qayta kodlamaydi.
    Natijada chiqadigan fayl yo‘lini qaytaradi.
    """
    outtmpl = os.path.join(workdir, "%(id)s.%(ext)s")
    opts = _ydl_common_opts(outtmpl=outtmpl) | {
        "format": format_id,
        # postprocess yo‘q: re-encode yo‘q
        "postprocessors": [],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # yt-dlp download() qaytargan info’dan fayl path topish:
    # requested_downloads bo‘lishi mumkin
    if "requested_downloads" in info and info["requested_downloads"]:
        fp = info["requested_downloads"][0].get("filepath")
        if fp and os.path.exists(fp):
            return fp

    # fallback: prepare_filename
    with YoutubeDL(_ydl_common_opts(outtmpl=outtmpl)) as ydl:
        fp = ydl.prepare_filename(info)
    if os.path.exists(fp):
        return fp

    # oxirgi fallback: workdir ichidan mp4 qidiramiz
    for name in os.listdir(workdir):
        if name.lower().endswith(".mp4"):
            return os.path.join(workdir, name)

    raise RuntimeError("Fayl topilmadi (download tugadi, lekin filepath aniqlanmadi).")


# -------------------- Telegram handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! YouTube/TikTok/Instagram havolasini yuboring.\n\n"
        f"Cheklov: max {MAX_MB}MB.\n"
        "YouTube: format tanlaysiz.\n"
        "TikTok/Instagram: avtomatik mos format tanlanadi."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Foydalanish:\n"
        "1) Havolani yuboring.\n"
        "2) YouTube bo‘lsa — format tugmalaridan tanlang.\n"
        f"3) TikTok/Instagram bo‘lsa — bot {MAX_MB}MB ichida bo‘lgan eng yaxshi variantni yuboradi.\n\n"
        "Eslatma: Bot videoni qayta kodlamaydi (o‘zgartirmaydi)."
    )

def _pretty_btn_label(fmt: dict) -> str:
    height = fmt.get("height") or 0
    fs = _format_filesize(fmt)
    mb = (fs / (1024 * 1024)) if fs else None
    if mb is None:
        return f"{height}p (size?)"
    return f"{mb:.1f}MB, {height}p"

def _make_token(i: int) -> str:
    # juda qisqa token
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
        await msg.reply_text("Bu linkni tanimadim. YouTube/TikTok/Instagram link yuboring.")
        return

    await msg.chat.send_action(ChatAction.TYPING)

    # Extract info (blocking) -> thread
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
                "YouTube formatlaridan 50MB ichida yuboriladigan variant topilmadi.\n"
                "Video juda katta bo‘lishi mumkin."
            )
            return

        # token mapping
        fmt_map = {}
        buttons = []
        for idx, f in enumerate(fmts[:12]):  # juda ko‘p bo‘lib ketmasin
            token = _make_token(idx)
            fmt_map[token] = f
            buttons.append(
                InlineKeyboardButton(
                    text=_pretty_btn_label(f),
                    callback_data=f"YT|{token}"
                )
            )

        # 2 ustun qilib chiqaramiz (rasmga o‘xshash)
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
            formats=fmt_map
        )

        title = info.get("title") or "YouTube video"
        await msg.reply_text(
            f"🎬 {title}\nFormatni tanlang (max {MAX_MB}MB):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # TikTok/Instagram: avtomatik
    if platform in ("tiktok", "instagram"):
        best = choose_best_under_50mb_non_reencode(info)
        if not best:
            await msg.reply_text(
                f"{platform.title()} uchun {MAX_MB}MB ichida mos mp4 topilmadi.\n"
                "Ehtimol video katta, yoki link private/login talab qiladi."
            )
            return

        format_id = str(best.get("format_id"))
        await send_downloaded_video(update, context, url, format_id, platform_title=platform.title())
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
        if not pending or pending.extractor != "youtube":
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
        await send_downloaded_video(update, context, url, format_id, platform_title="YouTube")
        return

    await q.edit_message_text("Noma’lum amal.")


async def send_downloaded_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, format_id: str, platform_title: str):
    chat_id = update.effective_chat.id
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        with tempfile.TemporaryDirectory() as td:
            # download in thread
            file_path = await asyncio.to_thread(download_format, url, format_id, td)

            # size check (real)
            size = os.path.getsize(file_path)
            if size > MAX_BYTES:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Video {size/1024/1024:.1f}MB chiqdi — {MAX_MB}MB limitdan katta. Boshqa format tanlang."
                )
                return

            caption = f"{platform_title} yuklab olindi: {size/1024/1024:.1f}MB"
            # Video sifatida yuboramiz (Preview bo‘lsin)
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


# -------------------- main --------------------
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    return app

if __name__ == "__main__":
    app = build_app()
    app.run_polling(
        close_loop=False,
        allowed_updates=Update.ALL_TYPES,
    )
