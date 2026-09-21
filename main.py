#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Universal Downloader Bot (Railway-ready, python-telegram-bot v20+)

Asosiy imkoniyatlar:
- YouTube: faqat MAVJUD formatlar tugmalari chiqadi (144p/360p/720p... mavjud bo'lsa bor).
- TikTok/Instagram/Facebook va boshqalar: 📹 Video + 🎵 Audio tugmalari.
- Guruhda: bot media faylni aynan link yuborilgan xabar ostiga REPLY qilib tashlaydi.
- /broadcast va /broadcastpost (faqat admin) — start bosgan foydalanuvchilarga.

Til:
- /start da til tanlash: 🇺🇿 O‘zbekcha / 🇷🇺 Русский
- O‘zbekcha salomlashish matni o'zgarmaydi.
- Ruscha tanlanganda barcha asosiy yozuvlar ruscha chiqadi.

Railway/Cloud учун:
- Tavsiya: polling режими (RUN_MODE=polling). Webhook ҳам мумкин (RUN_MODE=webhook).
- ENV:
  BOT_TOKEN          (majburiy)
  ADMIN_IDS          (ixtiyoriy) "123,456"
  DATABASE_URL       (tavsiya) Postgres connection string (Railway Postgres ёки бошқа)
  WEBHOOK_URL        (webhook rejimi uchun) масалан: https://<your-domain>
  WEBHOOK_PATH       (ixtiyoriy) default: webhook
  PORT               (webhook режимда платформа беради: Railway ва бошқалар)
  DATA_DIR           (fallback json storage uchun; cloud серверда тавсия этилмайди)

Eslatma:
- MP3 konvertatsiya uchun ffmpeg tavsiya qilinadi. Bo'lmasa m4a/webm audio yuboriladi.
"""

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # python-dotenv optional (local test учун)
    pass

import os
import uuid
import re
import json
import asyncio
import logging
import tempfile
import shutil
import secrets
import time
import base64
import html
import subprocess
import zipfile
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlunsplit, urlparse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.error import TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from yt_dlp import YoutubeDL

try:
    import asyncpg
except Exception:
    asyncpg = None  # fallback to json storage


# ---------------------------- Config ----------------------------

TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN env topilmadi ('.env' borligini va BOT_TOKEN to'g'ri yozilganini tekshiring)")

ADMIN_IDS: set[int] = set()
_admin_raw = (os.getenv("ADMIN_IDS") or "").strip()
if _admin_raw:
    for part in _admin_raw.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))

DATA_DIR = Path((os.getenv("DATA_DIR") or ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Fallback json storage (cloud серверда тавсия этилмайди)
USERS_FILE = DATA_DIR / "users.json"
PREFS_FILE = DATA_DIR / "prefs.json"
CHATS_FILE = DATA_DIR / "chats.json"

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

BOT_USERNAME_TAG = "@universal_downloader_uzb_bot"

CALLBACK_CACHE: Dict[str, Dict[str, Any]] = {}
CALLBACK_CACHE_MAX = 3000


# Telegram стандарт Bot API'да файл юклаш чегараси (одатда ~50MB).
# Local Bot API server ишлатсангиз, бу чекловни каттароқ қила оласиз.
TG_MAX_UPLOAD_MB = int((os.getenv("TG_MAX_UPLOAD_MB") or ("1900" if (os.getenv("LOCAL_BOT_API_URL") or "").strip() else "49")).strip() or "49")

# YouTube format tanlashda maksimal ruxsat etilgan hajm (MB). Katta bo'lsa — formatni tanlashga qo'ymaymiz.
DL_MAX_MB = int((os.getenv("DL_MAX_MB") or "130").strip() or "130")

# YouTube format tanlashda maksimal ruxsat etilgan hajm (MB). Katta bo'lsa — formatni tanlashga qo'ymaymiz.
YT_MAX_MB = int((os.getenv("YT_MAX_MB") or str(DL_MAX_MB)).strip() or str(DL_MAX_MB))

# YouTube видеолар учун Telegram file_id кеш (RAM). Шу орқали такрорий сўровларда 1 секундда юборилади.
YOUTUBE_FILEID_CACHE: Dict[str, tuple[str, float]] = {}
YOUTUBE_FILEID_CACHE_MAX = 5000

# Universal file_id кеш (YouTube + boshqa тармоқлар). Key: normalized_url+kind+format.
FILEID_CACHE: Dict[str, tuple[str, float]] = {}
FILEID_CACHE_MAX = 15000

# Download concurrency (RAM/CPU ni tejash uchun): default 2 ta parallel download/merge
DL_CONCURRENCY = int((os.getenv("DL_CONCURRENCY") or "2").strip() or "2")
DOWNLOAD_SEM = asyncio.Semaphore(max(1, DL_CONCURRENCY))

# file_id cache TTL (kun). Default: 180 kun (~6 oy)
FILEID_TTL_DAYS = int((os.getenv("FILEID_TTL_DAYS") or "180").strip() or "180")
FILEID_TTL_SECONDS = max(1, FILEID_TTL_DAYS) * 24 * 60 * 60

def _now_ts() -> float:
    try:
        return time.time()
    except Exception:
        return 0.0

def _cache_get_fileid(cache: Dict[str, tuple[str, float]], key: str) -> Optional[str]:
    v = cache.get(key)
    if not v:
        return None
    fid, exp = v
    if exp and _now_ts() > exp:
        cache.pop(key, None)
        return None
    return fid

def _cache_put_fileid(cache: Dict[str, tuple[str, float]], key: str, file_id: str, max_items: int) -> None:
    if not key or not file_id:
        return
    exp = _now_ts() + FILEID_TTL_SECONDS
    cache[key] = (file_id, exp)
    if len(cache) > max_items:
        # remove a batch of oldest/expired items
        _prune_fileid_cache(cache, max_items=max_items)

def _prune_fileid_cache(cache: Dict[str, tuple[str, float]], max_items: int) -> int:
    """Remove expired items, then keep cache size under max_items. Returns removed count."""
    removed = 0
    now = _now_ts()
    # remove expired
    for k in list(cache.keys()):
        try:
            _, exp = cache.get(k) or ("", 0.0)
            if exp and now > exp:
                cache.pop(k, None)
                removed += 1
        except Exception:
            continue
    # shrink if still too big: drop earliest expiry first
    if len(cache) > max_items:
        items = sorted(cache.items(), key=lambda kv: float(kv[1][1] or 0.0))
        overflow = len(cache) - max_items
        for i in range(min(overflow, len(items))):
            cache.pop(items[i][0], None)
            removed += 1
    return removed


RUN_MODE = (os.getenv("RUN_MODE") or "").strip().lower()  # "webhook" or "polling"
def _guess_public_base_url() -> str:
    """Webhook учун public base URL ни топиш (RUN_MODE=webhook бўлса)."""
    v = (os.getenv("WEBHOOK_URL") or "").strip()
    if v:
        return v.rstrip("/")
    # Railway: best-effort (ҳамма аккаунтларда бўлмаслиги мумкин)
    dom = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_STATIC_DOMAIN") or "").strip()
    if dom:
        return f"https://{dom}".rstrip("/")
    v = (os.getenv("RAILWAY_PUBLIC_URL") or "").strip()
    if v:
        return v.rstrip("/")
    # Render (ixtiyoriy fallback, агар керак бўлса)
    v = (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    if v:
        return v.rstrip("/")
    return ""

WEBHOOK_URL_BASE = _guess_public_base_url()
WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or "webhook").strip().lstrip("/")
PORT = int(os.getenv("PORT") or "8080")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("downloader")


# ---------------------------- i18n ----------------------------

LANG_UZ = "uz"
LANG_RU = "ru"

START_TEXT_UZ = (
    "👋🏻 <b>Salom</b>\n"
    "Telegramdagi <b>YouTube</b>’dan, <b>Tiktokdan</b>, <b>Instagram</b> va <b>Focebook</b>dan video, audiolarni yuklab olish uchun eng tezkor "
    f"{BOT_USERNAME_TAG} ga xush kelibsiz.\n\n"
    "✅ <b>Botning imkoniyatlari:</b>\n"
    "✨ Youtubedan Video sifatini tanlash imkoniyati;\n"
    "📁 Video va audioni saqlab olish(cheksiz);\n"
    "💫 Yuklab olingan faylni do'stlarga ulashish;\n"
    "ℹ️ Botni guruxingizda admin qiling va guruhga yuborilgan havolalarni video ko’rinishida guruxingizga shu havola ostiga tashlab beradi.\n"
    "ℹ️ <b>Botni guruxingizda reklama tarqatmaydi</b>.\n"
    "ℹ️ Biror bir xatolikga duch kelsangiz bizni botlar kanaliga o’ting va u yerdagi adminlarga habar bering.\n"
    "Bizning foydali botlar kanali 👉 https://t.me/+skp5TgimYIJjYzIy\n\n"
    "🔗 <b>BOSHLASH UCHUN VIDEO HAVOLASINI YUBORING</b>…⤵️"
)

START_TEXT_RU = (
    "👋🏻 <b>Привет</b>\n"
    "Добро пожаловать в самый быстрый бот, чтобы скачивать видео и аудио из <b>YouTube</b>, <b>TikTok</b>, <b>Instagram</b> и <b>Facebook</b>: "
    f"{BOT_USERNAME_TAG}\n\n"
    "✅ <b>Возможности бота:</b>\n"
    "✨ Выбор качества видео YouTube;\n"
    "📁 Скачивание видео и аудио (без ограничений);\n"
    "💫 Возможность делиться скачанным файлом с друзьями;\n"
    "ℹ️ Сделайте бота администратором в группе — и он будет отправлять скачанное видео ответом под сообщением со ссылкой.\n"
    "ℹ️ <b>Бот не распространяет рекламу в вашей группе</b>.\n"
    "ℹ️ Если столкнётесь с ошибкой — перейдите в наш канал ботов и напишите администраторам.\n"
    "Наш полезный канал ботов 👉 https://t.me/+skp5TgimYIJjYzIy\n\n"
    "🔗 <b>ДЛЯ НАЧАЛА ОТПРАВЬТЕ ССЫЛКУ НА ВИДЕО</b>…⤵️"
)

TEXT = {
    "choose_lang": {
        LANG_UZ: "Tilni tanlang:",
        LANG_RU: "Выберите язык:",
    },
    "btn_uz": {LANG_UZ: "🇺🇿 O‘zbekcha", LANG_RU: "🇺🇿 O‘zbekcha"},
    "btn_ru": {LANG_UZ: "🇷🇺 Русский", LANG_RU: "🇷🇺 Русский"},
    "yt_fetching": {
        LANG_UZ: "🔎 YouTube formatlar olinmoqda...",
        LANG_RU: "🔎 Получаю форматы YouTube...",
    },
    "choose": {LANG_UZ: "Tanlang:", LANG_RU: "Выберите:"},
    "btn_video": {LANG_UZ: "📹 Video yuklab olish", LANG_RU: "📹 Скачать видео"},
    "btn_audio": {LANG_UZ: "🎵 Audio", LANG_RU: "🎵 Аудио"},
    "btn_mp3": {LANG_UZ: "🎵 MP3", LANG_RU: "🎵 MP3"},
    "tt_photo_audio_only": {
        LANG_UZ: "Bu TikTok foto-post (/photo/). Faqat audio (MP3) yuklash mumkin:",
        LANG_RU: "Это TikTok фото-пост (/photo/). Доступно только аудио (MP3):",
    },
    "btn_tt_photo": {LANG_UZ: "🖼 Foto post (ZIP)", LANG_RU: "🖼 Фото-пост (ZIP)"},
    "tt_photo_only": {
        LANG_UZ: "Bu TikTok foto-post (/photo/). Rasmlarni ZIP ko‘rinishida yuklab oling:",
        LANG_RU: "Это TikTok фото-пост (/photo/). Скачайте картинки в ZIP:",
    },
    "yt_caption": {
        LANG_UZ: "📹 <b>{title}</b>\n⏱ {dur}\n\n<b>Formatni tanlang:</b>",
        LANG_RU: "📹 <b>{title}</b>\n⏱ {dur}\n\n<b>Выберите формат:</b>",
    },
    "yt_choose_fmt": {
        LANG_UZ: "Formatni tanlang (YouTube):",
        LANG_RU: "Выберите формат (YouTube):",
    },
    "btn_expired": {
        LANG_UZ: "❌ Bu tugma eskirib qolgan. Iltimos linkni qayta yuboring.",
        LANG_RU: "❌ Эта кнопка устарела. Пожалуйста, отправьте ссылку ещё раз.",
    },
    "unsupported_url": {
        LANG_UZ: "❌ Bu havola qo‘llab-quvvatlanmaydi. Faqat YouTube, TikTok, Instagram, Facebook va OK.ru havolalarini yuboring.",
        LANG_RU: "❌ Эта ссылка не поддерживается. Отправляйте только ссылки YouTube, TikTok, Instagram, Facebook и OK.ru.",
    },
    "downloading_answer": {
        LANG_UZ: "⏳ Yuklab olinmoqda...",
        LANG_RU: "⏳ Скачиваю...",
    },
    "downloading_wait": {
        LANG_UZ: "⏳ Yuklab olinmoqda, iltimos kuting...",
        LANG_RU: "⏳ Скачиваю, пожалуйста подождите...",
    },
    "fmt_error": {
        LANG_UZ: "❌ Formatlarni olishda xatolik: {err}",
        LANG_RU: "❌ Ошибка при получении форматов: {err}",
    },
    "yt_too_big": {
        LANG_UZ: "⚠️ Bu format juda katta: {size}MB. Maksimal ruxsat etilgan: {max}MB. Iltimos, boshqa format tanlang.",
        LANG_RU: "⚠️ Этот формат слишком большой: {size}MB. Максимально разрешено: {max}MB. Пожалуйста, выберите другой формат.",
    },


    "err_filename_too_long": {
        LANG_UZ: "❌ Fayl nomi juda uzun bo‘lib кетди (server cheklovi). Boshqa variantni tanlang yoki linkni qayta yuboring.",
        LANG_RU: "❌ Слишком длинное имя файла (ограничение сервера). Выберите другой вариант или отправьте ссылку заново.",
    },
    "yt_need_cookies": {
        LANG_UZ: "❌ YouTube «men robot emasman» tekshiruvini so‘radi. YouTube ишлаши учун (cloud серверда) браузердан экспорт қилинган Netscape formatdagi cookies.txt kerak.",
        LANG_RU: "❌ YouTube требует подтверждение «я не бот». Cloud серверда YouTube учун ҳам керак:  cookies.txt, экспортированный из браузера (формат Netscape).",
    },
    
    "yt_403": {
        LANG_UZ: "❌ YouTube 403 Forbidden. Bu odatda cloud/datacenter IP blok ёки cookies eskirganidan bo‘ladi. Cookies.txt ni yangilang (login bo‘lgan brauzerdan eksport), yoki Proxy/VPS (rezident IP) ishlating.",
        LANG_RU: "❌ YouTube 403 Forbidden. Обычно это блокировка cloud/datacenter IP или устаревшие cookies. Обновите cookies.txt (экспорт из залогиненного браузера) или используйте Proxy/VPS (резидентный IP).",
    },

    "yt_botcheck_even_with_cookies": {
        LANG_UZ: "❌ YouTube «men robot emasman» tekshiruvini so‘radi. Cookies топилган бўлса ҳам cloud/IP блок сабабли baribir captcha chiqishi mumkin. Cookies.txt ni yangilang (login bo‘lgan brauzerdan), yoki VPS/Proxy (rezident IP) ishlating.",
        LANG_RU: "❌ YouTube просит подтверждение «я не бот». Ҳатто cookies билан ҳам cloud (datacenter IP) капча может появляться. Обновите cookies.txt (из залогиненного браузера) или используйте VPS/Proxy (резидентный IP).",
    },
"err_generic": {LANG_UZ: "❌ Xatolik: {err}", LANG_RU: "❌ Ошибка: {err}"},

    "err_rate_limited": {LANG_UZ: "⚠️ Juda ko‘p so‘rov yuborildi (429). Biroz kutib qayta urinib ko‘ring.", LANG_RU: "⚠️ Слишком много запросов (429). Подождите и попробуйте снова."},
    "err_youtube_botcheck": {LANG_UZ: "⚠️ YouTube «bot-check» чиқарди. Cookie (YT_COOKIES_B64) ни тўғри қўйинг ёки прокси/VPS (резидент IP) ишлатинг, кейин линкни қайта юборинг.", LANG_RU: "⚠️ YouTube требует подтверждение (bot-check). Проверьте cookies (YT_COOKIES_B64) или используйте proxy/VPS (резидентный IP), затем отправьте ссылку снова."},
    "err_format_unavailable": {LANG_UZ: "⚠️ Танланган формат мавжуд эмас ёки форматлар тўлиқ чиқмаяпти. Танлаш ойнасини қайта чиқаринг (линкни қайта юборинг) ёки cookies/proxy ни текширинг.", LANG_RU: "⚠️ Выбранный формат недоступен или список форматов неполный. Снова получите форматы (отправьте ссылку заново) или проверьте cookies/proxy."},
    "not_admin": {LANG_UZ: "❌ Siz admin emassiz.", LANG_RU: "❌ Вы не админ."},
    "usage_broadcast": {
        LANG_UZ: "Ishlatish: /broadcast xabar_matni",
        LANG_RU: "Использование: /broadcast текст_сообщения",
    },
    "bc_started": {
        LANG_UZ: "📣 Broadcast boshlandi. Users: {n}",
        LANG_RU: "📣 Рассылка началась. Пользователей: {n}",
    },
    "bc_done": {
        LANG_UZ: "✅ Yakunlandi. Yuborildi: {sent}, Xato: {failed}",
        LANG_RU: "✅ Готово. Отправлено: {sent}, Ошибок: {failed}",
    },
    "usage_broadcastpost": {
        LANG_UZ: "Ishlatish: Kerakli postga reply qiling va /broadcastpost yozing.",
        LANG_RU: "Использование: Ответьте на нужный пост и отправьте /broadcastpost.",
    },
    "bcpost_started": {
        LANG_UZ: "📣 BroadcastPost boshlandi. Users: {n}",
        LANG_RU: "📣 Пересылка поста началась. Пользователей: {n}",
    },
    "usage_broadcastgroup": {
        LANG_UZ: "Ishlatish: /broadcastgroup xabar_matni",
        LANG_RU: "Использование: /broadcastgroup текст_сообщения",
    },
    "usage_broadcastpostgroup": {
        LANG_UZ: "Ishlatish: Kerakli postga reply qiling va /broadcastpostgroup yozing.",
        LANG_RU: "Использование: Ответьте на нужный пост и отправьте /broadcastpostgroup.",
    },
    "bcgroup_started": {
        LANG_UZ: "📣 Guruhlarga broadcast boshlandi. Groups: {n}",
        LANG_RU: "📣 Рассылка в группы началась. Групп: {n}",
    },
    "bcpostgroup_started": {
        LANG_UZ: "📣 Guruhlarga post yuborish boshlandi. Groups: {n}",
        LANG_RU: "📣 Пересылка поста в группы началась. Групп: {n}",
    },
    "caption_suffix": {
        LANG_UZ: f"{BOT_USERNAME_TAG} da yuklab olindi",
        LANG_RU: f"Скачано в {BOT_USERNAME_TAG}",
    },
}


def _t(lang: str, key: str, **kwargs) -> str:
    d = TEXT.get(key) or {}
    s = d.get(lang) or d.get(LANG_UZ) or key
    if kwargs:
        try:
            return s.format(**kwargs)
        except Exception:
            return s
    return s


# ---------------------------- User storage (DB + fallback JSON) ----------------------------

def _json_load(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def _json_save(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_users_json() -> Dict[str, Any]:
    """
    Supports both formats:
      - old: [123,456]
      - new: {"users":[123,456]}
    """
    raw = _json_load(USERS_FILE, {"users": []})
    if isinstance(raw, list):
        return {"users": raw}
    if isinstance(raw, dict):
        users = raw.get("users")
        if not isinstance(users, list):
            raw["users"] = []
        return raw
    return {"users": []}

def _add_user_json(user_id: int) -> None:
    data = _load_users_json()
    users = set(int(x) for x in (data.get("users") or []) if str(x).isdigit())
    users.add(int(user_id))
    data["users"] = sorted(users)
    _json_save(USERS_FILE, data)

def _get_users_json() -> List[int]:
    data = _load_users_json()
    return [int(x) for x in (data.get("users") or []) if str(x).isdigit()]

def _load_groups_json() -> Dict[str, Any]:
    """
    Stores groups where bot has been seen (best-effort).
    Format:
      - old: [-100..., -100...]
      - new: {"groups":[-100...]}
    """
    raw = _json_load(CHATS_FILE, {"groups": []})
    if isinstance(raw, list):
        return {"groups": raw}
    if isinstance(raw, dict):
        groups = raw.get("groups")
        if not isinstance(groups, list):
            raw["groups"] = []
        return raw
    return {"groups": []}

def _add_group_json(chat_id: int) -> None:
    data = _load_groups_json()
    groups = set()
    for x in (data.get("groups") or []):
        s = str(x)
        if s.lstrip("-").isdigit():
            groups.add(int(s))
    groups.add(int(chat_id))
    data["groups"] = sorted(groups)
    _json_save(CHATS_FILE, data)

def _get_groups_json() -> List[int]:
    data = _load_groups_json()
    out: List[int] = []
    for x in (data.get("groups") or []):
        s = str(x)
        if s.lstrip("-").isdigit():
            out.append(int(s))
    return out

def _load_prefs_json() -> Dict[str, Any]:
    raw = _json_load(PREFS_FILE, {})
    return raw if isinstance(raw, dict) else {}

def _set_lang_json(user_id: int, lang: str) -> None:
    prefs = _load_prefs_json()
    prefs[str(int(user_id))] = lang
    _json_save(PREFS_FILE, prefs)

def _get_lang_json(user_id: int) -> Optional[str]:
    prefs = _load_prefs_json()
    v = prefs.get(str(int(user_id)))
    return v if v in (LANG_UZ, LANG_RU) else None


class UserStore:
    def __init__(self) -> None:
        self.pool: Optional["asyncpg.pool.Pool"] = None

    async def init(self) -> None:
        if not DATABASE_URL or asyncpg is None:
            if not DATABASE_URL:
                log.warning("DATABASE_URL topilmadi — fallback: users.json ishlatiladi (cloud серверда тавсия этилмайди).")
            else:
                log.warning("asyncpg import bo'lmadi — fallback: users.json ishlatiladi.")
            return

        ssl_opt: Optional[bool] = None
        if "sslmode=require" in DATABASE_URL.lower() or (os.getenv("PGSSLMODE") or "").lower() == "require":
            ssl_opt = True

        try:
            self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl=ssl_opt)
        except Exception as e:
            log.error("DB ulanishida xatolik (fallback: JSON): %s", e)
            self.pool = None
            return
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
              user_id    BIGINT PRIMARY KEY,
              first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              last_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              lang       TEXT NOT NULL DEFAULT 'uz',
              username   TEXT,
              first_name TEXT,
              last_name  TEXT
            );
            """
        )
        await self.pool.execute("CREATE INDEX IF NOT EXISTS bot_users_last_seen_idx ON bot_users(last_seen);")
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_chats (
              chat_id    BIGINT PRIMARY KEY,
              chat_type  TEXT NOT NULL,
              title      TEXT,
              last_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await self.pool.execute("CREATE INDEX IF NOT EXISTS bot_chats_last_seen_idx ON bot_chats(last_seen);")
        log.info("DB tayyor: bot_users jadvali tekshirildi/yaratildi.")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def touch_user(self, user: User, lang: Optional[str] = None) -> None:
        """Insert/update user. lang berilsa — yangilanadi; berilmasa — avvalgisi saqlanadi."""
        uid = int(user.id)
        if self.pool:
            await self.pool.execute(
                """
                INSERT INTO bot_users (user_id, username, first_name, last_name, last_seen, lang)
                VALUES ($1, $2, $3, $4, NOW(), COALESCE($5, 'uz'))
                ON CONFLICT (user_id) DO UPDATE SET
                  username   = EXCLUDED.username,
                  first_name = EXCLUDED.first_name,
                  last_name  = EXCLUDED.last_name,
                  last_seen  = NOW(),
                  lang       = COALESCE($5, bot_users.lang);
                """,
                uid,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                lang,
            )
        else:
            _add_user_json(uid)
            if lang in (LANG_UZ, LANG_RU):
                _set_lang_json(uid, lang)

    async def set_lang(self, user: User, lang: str) -> None:
        uid = int(user.id)
        if self.pool:
            await self.pool.execute(
                """
                INSERT INTO bot_users (user_id, username, first_name, last_name, last_seen, lang)
                VALUES ($1, $2, $3, $4, NOW(), $5)
                ON CONFLICT (user_id) DO UPDATE SET
                  username   = EXCLUDED.username,
                  first_name = EXCLUDED.first_name,
                  last_name  = EXCLUDED.last_name,
                  last_seen  = NOW(),
                  lang       = EXCLUDED.lang;
                """,
                uid,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                lang,
            )
        else:
            _add_user_json(uid)
            _set_lang_json(uid, lang)

    async def get_lang(self, user_id: int) -> str:
        uid = int(user_id)
        if self.pool:
            row = await self.pool.fetchrow("SELECT lang FROM bot_users WHERE user_id=$1", uid)
            lang = (row["lang"] if row else None)  # type: ignore[index]
            return lang if lang in (LANG_UZ, LANG_RU) else LANG_UZ
        v = _get_lang_json(uid)
        return v if v in (LANG_UZ, LANG_RU) else LANG_UZ

    async def get_users(self) -> List[int]:
        if self.pool:
            rows = await self.pool.fetch("SELECT user_id FROM bot_users")
            return [int(r["user_id"]) for r in rows]  # type: ignore[index]
        return _get_users_json()

    async def touch_chat(self, chat) -> None:
        """Insert/update group chat where the bot has been seen (best-effort)."""
        try:
            cid = int(getattr(chat, "id"))
        except Exception:
            return
        ctype = (getattr(chat, "type", "") or "").lower()
        if ctype not in ("group", "supergroup"):
            return
        title = getattr(chat, "title", None)

        if self.pool:
            try:
                await self.pool.execute(
                    """
                    INSERT INTO bot_chats (chat_id, chat_type, title, last_seen)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (chat_id) DO UPDATE SET
                      chat_type = EXCLUDED.chat_type,
                      title     = EXCLUDED.title,
                      last_seen = NOW();
                    """,
                    cid,
                    ctype,
                    title,
                )
            except Exception:
                # do not crash bot on DB errors
                pass
        else:
            _add_group_json(cid)

    async def get_groups(self) -> List[int]:
        """Return known group/supergroup chat_ids (best-effort)."""
        if self.pool:
            try:
                rows = await self.pool.fetch("SELECT chat_id FROM bot_chats WHERE chat_type IN ('group','supergroup')")
                return [int(r["chat_id"]) for r in rows]  # type: ignore[index]
            except Exception:
                return _get_groups_json()
        return _get_groups_json()



STORE = UserStore()


async def get_user_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Lang priority: context.user_data -> DB/JSON -> default uz"""
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return LANG_UZ
    cached = context.user_data.get("lang")
    if cached in (LANG_UZ, LANG_RU):
        return cached
    lang = await STORE.get_lang(uid)
    context.user_data["lang"] = lang
    return lang


# ---------------------------- Utils ----------------------------

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().rstrip(").,!?;\"'")

def is_youtube(url: str) -> bool:
    u = url.lower()
    return ("youtube.com" in u) or ("youtu.be" in u)

def human_mb(num_bytes: Optional[int]) -> Optional[str]:
    if not num_bytes or num_bytes <= 0:
        return None
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def human_mb_compact(num_bytes: Optional[int]) -> Optional[str]:
    if not num_bytes or num_bytes <= 0:
        return None
    mb = num_bytes / (1024 * 1024)
    if mb >= 10:
        return f"{mb:.0f}MB"
    return f"{mb:.1f}MB"

def human_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "-"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    if h > 0:
        return f"{h:d}:{m:02d}:{ss:02d}"
    return f"{m:d}:{ss:02d}"

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()

def is_tiktok_photo(url: str) -> bool:
    u = (url or "").lower()
    return ("tiktok.com" in u) and ("/photo/" in u)


def is_instagram(url: str) -> bool:
    u = (url or "").lower()
    return ("instagram.com" in u) or ("instagr.am" in u)

def is_facebook(url: str) -> bool:
    u = (url or "").lower()
    return ("facebook.com" in u) or ("fb.watch" in u) or ("fb.com" in u) or ("m.facebook.com" in u)

def is_okru(url: str) -> bool:
    u = (url or "").lower()
    return ("ok.ru" in u) or ("odnoklassniki.ru" in u)

def is_supported_url(url: str) -> bool:
    return is_youtube(url) or is_tiktok(url) or is_instagram(url) or is_facebook(url) or is_okru(url)

def _normalize_url_for_cache(url: str) -> str:
    """Cache uchun URL ni maksimal barqarorlashtirish (canonical key).

    Eslatma: bu FUNKSIYA download uchun эмас, faqat CACHE KEY uchun.
    Shuning uchun tracking/query larni olib tashlaymiz, lekin asosiy identifikatorlarni saqlaymiz.
    """
    try:
        u = (url or "").strip()
        if not u:
            return ""

        parts = urlsplit(u)
        scheme = (parts.scheme or "https").lower()
        netloc = (parts.netloc or "").lower()
        path = parts.path or ""
        query = parts.query or ""

        # Remove trailing slash for stability
        if path != "/" and path.endswith("/"):
            path = path[:-1]

        # -------- YouTube canonical --------
        if "youtu.be" in netloc:
            # https://youtu.be/<id>
            vid = path.strip("/").split("/")[0] if path else ""
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
        if "youtube.com" in netloc:
            # https://www.youtube.com/watch?v=<id>
            q = dict([p.split("=", 1) if "=" in p else (p, "") for p in query.split("&") if p])
            vid = q.get("v") or ""
            if not vid:
                # /shorts/<id> or /embed/<id>
                parts_path = [p for p in path.split("/") if p]
                if len(parts_path) >= 2 and parts_path[0] in ("shorts", "embed"):
                    vid = parts_path[1]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"

        # -------- TikTok canonical --------
        if "tiktok.com" in netloc:
            # keep only path (drop query), this already normalizes most variants
            return urlunsplit((scheme, netloc, path, "", ""))

        # -------- Instagram canonical --------
        if "instagram.com" in netloc or "instagr.am" in netloc:
            # keep /reel/<id>, /p/<id>, /tv/<id>
            seg = [s for s in path.split("/") if s]
            if len(seg) >= 2 and seg[0] in ("reel", "p", "tv"):
                path = f"/{seg[0]}/{seg[1]}"
            return urlunsplit((scheme, netloc, path, "", ""))

        # -------- Facebook canonical --------
        if "fb.watch" in netloc:
            # /<code>/
            code = path.strip("/").split("/")[0] if path else ""
            if code:
                return f"https://fb.watch/{code}"
            return urlunsplit((scheme, netloc, path, "", ""))
        if "facebook.com" in netloc or "fb.com" in netloc:
            # preserve only essential query keys for watch links
            if path.startswith("/watch"):
                # keep v= only
                q = {}
                for p in query.split("&"):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        if k in ("v",):
                            q[k] = v
                qstr = "&".join([f"{k}={v}" for k, v in q.items() if v])
                return urlunsplit((scheme, netloc, "/watch", qstr, ""))
            # reels: /reel/<id>
            seg = [s for s in path.split("/") if s]
            if len(seg) >= 2 and seg[0] == "reel":
                return urlunsplit((scheme, netloc, f"/reel/{seg[1]}", "", ""))
            # default: strip query/fragment
            return urlunsplit((scheme, netloc, path, "", ""))

        # -------- OK.ru canonical --------
        if "ok.ru" in netloc or "odnoklassniki.ru" in netloc:
            # keep only path, strip query
            return urlunsplit((scheme, netloc, path, "", ""))

        # Default: strip query/fragment, lower netloc
        return urlunsplit((scheme, netloc, path, "", ""))
    except Exception:
        return (url or "").strip()

def _make_fileid_cache_key(url: str, kind: str, format_id: Optional[str] = None, yt_key: Optional[str] = None) -> str:
    if yt_key:
        return f"{yt_key}:{kind}"
    return f"u:{_normalize_url_for_cache(url)}:{kind}:{format_id or ''}"


def _strip_query(url: str) -> str:
    """Remove query params/fragments for more stable matching."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return url


def _resolve_final_url(url: str, timeout: float = 6.0) -> str:
    """Follow redirects (useful for vt.tiktok.com short links)."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = getattr(resp, "geturl", lambda: url)()
            return final or url
    except Exception:
        return url

def _estimate_bytes_from_kbps(kbps: Optional[float], duration_s: Optional[float]) -> int:
    if not kbps or not duration_s or kbps <= 0 or duration_s <= 0:
        return 0
    # kbps -> bytes
    return int((kbps * 1000 / 8) * duration_s)

def _best_audio_size_bytes(info: Dict[str, Any]) -> int:
    formats = info.get("formats") or []
    dur = info.get("duration")
    auds = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if not auds:
        return 0

    def score(a: Dict[str, Any]) -> Tuple[float, int]:
        abr = float(a.get("abr") or 0.0)
        tbr = float(a.get("tbr") or 0.0)
        # prefer m4a, then higher bitrate
        ext = (a.get("ext") or "").lower()
        ext_score = 2 if ext == "m4a" else (1 if ext in ("mp4", "aac") else 0)
        return (ext_score * 1000 + max(abr, tbr), int(a.get("filesize") or a.get("filesize_approx") or 0))

    best = sorted(auds, key=score, reverse=True)[0]
    sz = int(best.get("filesize") or best.get("filesize_approx") or 0)
    if sz > 0:
        return sz
    kbps = float(best.get("tbr") or best.get("abr") or 0.0)
    return _estimate_bytes_from_kbps(kbps, dur)




def _best_audio_size_bytes_meta(info: Dict[str, Any]) -> Tuple[int, bool]:
    """Return (size_bytes, is_approx) for the best audio stream."""
    formats = info.get("formats") or []
    dur = info.get("duration")
    auds = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if not auds:
        return (0, True)

    def score(a: Dict[str, Any]) -> Tuple[float, int]:
        abr = float(a.get("abr") or 0.0)
        tbr = float(a.get("tbr") or 0.0)
        ext = (a.get("ext") or "").lower()
        ext_score = 2 if ext == "m4a" else (1 if ext in ("mp4", "aac") else 0)
        return (ext_score * 1000 + max(abr, tbr), int(a.get("filesize") or a.get("filesize_approx") or 0))

    best = sorted(auds, key=score, reverse=True)[0]
    fs = int(best.get("filesize") or 0)
    if fs > 0:
        return (fs, False)
    fsa = int(best.get("filesize_approx") or 0)
    if fsa > 0:
        return (fsa, True)
    kbps = float(best.get("tbr") or best.get("abr") or 0.0)
    est = _estimate_bytes_from_kbps(kbps, dur)
    return (est, True)


def _format_size_is_approx(info: Dict[str, Any], f: Dict[str, Any]) -> bool:
    """Heuristic: True when displayed size is not guaranteed exact.

    yt-dlp often shows FILESIZE as:
      - exact `filesize` for some direct HTTPS formats
      - `filesize_approx` or a bitrate*duration estimate for many DASH/HLS formats
    Even when `filesize` is present for HLS (m3u8), it may still be effectively approximate.
    """
    # HLS/m3u8 is almost always approximate in practice
    proto = str(f.get("protocol") or "").lower()
    if "m3u8" in proto:
        return True
    # Some formats carry manifest URLs (HLS/DASH). Treat as approximate.
    if f.get("manifest_url") or f.get("fragments"):
        return True

    # If yt-dlp explicitly tells us it's approximate
    if int(f.get("filesize_approx") or 0) > 0:
        return True

    # If we have an exact filesize, consider it exact for non-HLS
    if int(f.get("filesize") or 0) > 0:
        return False

    # Otherwise, if we can only estimate from bitrate * duration, it's approximate
    dur = info.get("duration") or f.get("duration")
    kbps = float(f.get("tbr") or f.get("vbr") or f.get("abr") or 0.0)
    if kbps > 0 and dur:
        return True

    # Unknown size source -> don't claim it's approximate (we usually won't display size anyway)
    return False



def _yt_height(fmt: Dict[str, Any]) -> int:
    """Best-effort parse of a format's height.

    Problem we are fixing:
      - Sometimes yt-dlp does NOT populate `height` and also keeps `format_note` empty.
      - In that case, we still want to detect the height from fields like `resolution`
        (e.g. "640x360", "640×360"), or from the human-readable `format` string.

    Returns 0 if unknown.
    """
    # 1) Direct numeric fields (most reliable)
    try:
        h = int(fmt.get("height") or 0)
        if h > 0:
            return h
    except Exception:
        pass

    # Some extractors provide width/height separately
    try:
        w = int(fmt.get("width") or 0)
        h2 = int(fmt.get("height") or 0)
        if w > 0 and h2 > 0:
            return h2
    except Exception:
        pass

    # 2) Parse common textual fields
    # Prefer explicit "###p" patterns first, then resolution "WxH"
    keys = ("format_note", "resolution", "format", "format_id", "display_id")
    for k in keys:
        s = str(fmt.get(k) or "")
        if not s:
            continue

        # e.g. "360p", "1080p60"
        m = re.search(r"(?i)(?<!\d)(\d{3,4})p(?:\d{1,3})?(?!\d)", s)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

        # e.g. "640x360", "640×360", "640 x 360"
        m = re.search(r"(?i)(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)", s)
        if m:
            try:
                # height is the second number in WxH
                h = int(m.group(2))
                # ignore weird tiny numbers
                if h >= 100:
                    return h
            except Exception:
                pass

        # e.g. " 360 " (rare) — only accept if it looks like a resolution token
        # We keep this conservative to avoid matching bitrate, itag, etc.
        m = re.search(r"(?i)(?:\b|_)(144|240|360|480|720|1080|1440|2160)(?:\b|_)", s)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    return 0




def _is_real_youtube_video_format(f: Dict[str, Any]) -> bool:
    """Return True only for real video formats (exclude audio-only and storyboard/preview formats).

    YouTube sometimes returns storyboard/preview formats with tiny heights (e.g. 27/45/90/180).
    Those must NOT be treated as selectable video qualities.
    """
    # Must have a video codec
    if f.get("vcodec") in (None, "none"):
        return False

    # Must have a sane video height
    h = int(_yt_height(f) or 0)
    if h < 100:
        return False

    # Exclude storyboard/preview (often mhtml images with format_id sb0/sb1/...)
    fid = str(f.get("format_id") or "").lower()
    fmt = str(f.get("format") or "").lower()
    note = str(f.get("format_note") or "").lower()
    ext = str(f.get("ext") or "").lower()

    if fid.startswith("sb") or "storyboard" in fmt or "storyboard" in note:
        return False
    if ext in ("mhtml", "jpg", "jpeg", "png", "webp") and ("storyboard" in fmt or fid.startswith("sb")):
        return False

    # Only keep normal video containers/codecs
    if ext and ext not in ("mp4", "webm", "mkv"):
        # allow empty ext (rare) but drop known non-video ext
        return False

    return True

def _yt_debug_dump_formats(info: Dict[str, Any]) -> None:
    """Verbose formats diagnostics when YTDLP_DEBUG_FORMATS=1.

    Goal: distinguish between real video formats, audio-only formats, and storyboard/preview formats
    (which often show tiny 'heights' like 27/45/90/180).
    """
    try:
        fs: List[Dict[str, Any]] = info.get("formats") or []
        total = len(fs)

        def _is_audio_only(f: Dict[str, Any]) -> bool:
            return (f.get("vcodec") in (None, "none")) and (f.get("acodec") not in (None, "none"))

        def _is_storyboard_like(f: Dict[str, Any]) -> bool:
            h = int(_yt_height(f) or 0)
            fid = str(f.get("format_id") or "").lower()
            fmt = str(f.get("format") or "").lower()
            note = str(f.get("format_note") or "").lower()
            proto = str(f.get("protocol") or "").lower()
            if fid.startswith("sb") or "storyboard" in fmt or "storyboard" in note:
                return True
            if h and h < 100:
                return True
            if "mhtml" in proto:
                return True
            return False

        real = [f for f in fs if _is_real_youtube_video_format(f)]
        audio = [f for f in fs if _is_audio_only(f)]
        sb = [f for f in fs if _is_storyboard_like(f)]

        heights_all = sorted({int(_yt_height(f) or 0) for f in fs if int(_yt_height(f) or 0) > 0})
        heights_real = sorted({int(_yt_height(f) or 0) for f in real if int(_yt_height(f) or 0) > 0})

        log.info(
            "YT formats debug: total=%s real_video=%s audio_only=%s storyboard_like=%s heights_all=%s heights_real=%s",
            total,
            len(real),
            len(audio),
            len(sb),
            heights_all[:25],
            heights_real[:25],
        )

        # Print a small sample for quick troubleshooting
        for f in fs[:12]:
            log.info(
                "YT fmt: id=%s ext=%s h=%s v=%s a=%s proto=%s note=%s fmt=%s",
                f.get("format_id"),
                f.get("ext"),
                _yt_height(f),
                f.get("vcodec"),
                f.get("acodec"),
                f.get("protocol"),
                (f.get("format_note") or ""),
                (str(f.get("format") or "")[:120]),
            )
    except Exception:
        pass

        # Exclude obvious storyboard/thumbnail formats
        fid = str(f.get("format_id") or "").lower()
        fmt = str(f.get("format") or "").lower()
        note = str(f.get("format_note") or "").lower()
        proto = str(f.get("protocol") or "").lower()

        if fid.startswith("sb") or "storyboard" in fmt or "storyboard" in note:
            return False
        if "mhtml" in proto:
            return False

        # Keep only typical video containers
        ext = (f.get("ext") or "").lower()
        if ext and ext not in ("mp4", "webm", "mkv", "mov"):
            return False

        return True
    except Exception:
        return False


def _best_video_format_under_height(info: Dict[str, Any], hmax: int) -> Optional[Dict[str, Any]]:
    formats = info.get("formats") or []
    vids: List[Dict[str, Any]] = []
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        h = _yt_height(f)
        if h <= 0 or h > hmax:
            continue
        ff = f
        ff["_h"] = h
        vids.append(ff)

    if not vids:
        return None

    def score(ff: Dict[str, Any]):
        ext = (ff.get("ext") or "").lower()
        ext_score = 2 if ext == "mp4" else (1 if ext in ("webm", "mkv") else 0)
        h = int(ff.get("_h") or _yt_height(ff) or 0)
        br = max(float(ff.get("tbr") or 0), float(ff.get("vbr") or 0), float(ff.get("abr") or 0))
        fs = float(ff.get("filesize") or 0) + float(ff.get("filesize_approx") or 0)
        has_url = 1 if ff.get("url") else 0
        return (has_url, ext_score, h, br, fs)

    return max(vids, key=score)



def _video_total_size_bytes(info: Dict[str, Any], f: Dict[str, Any]) -> int:
    dur = info.get("duration")
    sz = int(f.get("filesize") or f.get("filesize_approx") or 0)
    if sz <= 0:
        kbps = float(f.get("tbr") or 0.0)
        sz = _estimate_bytes_from_kbps(kbps, dur)
    # If this format has no audio, add best audio size for display
    if (f.get("acodec") == "none") or not f.get("acodec"):
        sz += _best_audio_size_bytes(info)
    return sz


def _video_bytes_only_est(info: Dict[str, Any], f: Dict[str, Any]) -> int:
    """Estimate ONLY the video-stream size in bytes. Returns 0 if unknown.

    This is used to avoid showing misleading identical sizes when yt-dlp doesn't
    provide per-format size/bitrate.
    """
    dur = info.get("duration") or f.get("duration")
    fs = int(f.get("filesize") or 0)
    if fs > 0:
        return fs
    fs2 = int(f.get("filesize_approx") or 0)
    if fs2 > 0:
        return fs2
    kbps = float(f.get("tbr") or f.get("vbr") or 0.0)
    if kbps <= 0 or not dur:
        return 0
    return _estimate_bytes_from_kbps(kbps, dur)

def _video_total_size_bytes_strict(info: Dict[str, Any], f: Dict[str, Any]) -> int:
    """Estimate total size (video + best audio if needed). Returns 0 if video size is unknown."""
    v = _video_bytes_only_est(info, f)
    if v <= 0:
        return 0
    total = v
    if (f.get("acodec") == "none") or not f.get("acodec"):
        a = _best_audio_size_bytes(info)
        if a > 0:
            total += a
    return total

def _pick_best_thumbnail_url(info: Dict[str, Any]) -> Optional[str]:
    # yt-dlp may provide 'thumbnail' and list 'thumbnails'
    t = info.get("thumbnail")
    if t:
        return t
    thumbs = info.get("thumbnails") or []
    if not thumbs:
        return None
    # pick biggest by width/height if present
    def score(x: Dict[str, Any]) -> Tuple[int, int]:
        return (int(x.get("width") or 0), int(x.get("height") or 0))
    best = sorted(thumbs, key=score, reverse=True)[0]
    return best.get("url")


def _cache_put(payload: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(8)[:10]
    if len(CALLBACK_CACHE) >= CALLBACK_CACHE_MAX:
        for k in list(CALLBACK_CACHE.keys())[: CALLBACK_CACHE_MAX // 2]:
            CALLBACK_CACHE.pop(k, None)
    CALLBACK_CACHE[token] = payload
    return token

def _cache_get(token: str) -> Optional[Dict[str, Any]]:
    return CALLBACK_CACHE.get(token)


def _friendly_ydl_error(e: Exception, lang: str) -> str:
    """Minimal, user-friendly error text for logs from yt-dlp / download."""
    s = str(e)
    s_low = s.lower()

    # YouTube bot-check patterns
    if "sign in to confirm you’re not a bot" in s_low or "confirm you’re not a bot" in s_low:
        # Cookies bor-yo‘qligini taxmin qilamiz
        if (os.getenv("YT_COOKIES_FILE") or os.getenv("YT_COOKIES_URL") or os.getenv("YT_COOKIES_TEXT")):
            return _t(lang, "yt_botcheck_even_with_cookies")
        return _t(lang, "yt_need_cookies")

    # 403 Forbidden (ko‘pincha YouTube cloud/IP blok)
    if "http error 403" in s_low or "403 forbidden" in s_low:
        return _t(lang, "yt_403")

    # 429 Too Many Requests (rate limit)
    if "http error 429" in s_low or "too many requests" in s_low:
        return _t(lang, "err_rate_limited")

    # Requested format not available
    if "requested format is not available" in s_low or "use --list-formats" in s_low:
        return _t(lang, "err_format_unavailable")

    if "unsupported url" in s_low:
        return s

    if "filename too long" in s_low:
        return _t(lang, "err_filename_too_long")

    # Default: qisqa qilib qaytaramiz
    if len(s) > 250:
        s = s[:247] + "..."
    return s




# ---------------------------- yt-dlp cookies helpers ----------------------------

_COOKIEFILE_PATH: Optional[str] = None
_COOKIE_LOGGED: bool = False

def _ensure_cookiefile(workdir: Optional[str] = None) -> Optional[str]:
    """Prepare a **writable** cookies.txt for yt-dlp and return its path.

    Important: do NOT reuse the same temp cookies path across concurrent requests.
    yt-dlp may update cookies on exit, and parallel runs can corrupt a shared file.
    So we create a fresh temp file per call.

    Sources (priority order):
      - YT_COOKIES_URL: direct https URL to cookies.txt (raw text)
      - YT_COOKIES_TEXT: cookies.txt content as multiline env (recommended if you don't want files)
      - YT_COOKIES_FILE: path to cookies.txt (e.g. /etc/secrets/cookies.txt or /app/cookies_youtube.txt)

    Notes:
      - yt-dlp expects the cookies file in Netscape format and UTF-8 text.
      - If the source is not UTF-8 (e.g., Windows-1251), we transparently re-encode to UTF-8.
    """
    def _dst_path() -> str:
        base_dir = workdir if workdir else tempfile.gettempdir()
        os.makedirs(base_dir, exist_ok=True)
        return os.path.join(base_dir, f"yt_cookies_{uuid.uuid4().hex}.txt")

    def _warn_if_suspicious(path: str) -> None:
        try:
            sz = os.path.getsize(path)
            if sz <= 0:
                log.warning("YT cookies file is empty: %s", path)
                return
            with open(path, "rb") as f:
                head = f.read(256)
            head_txt = head.decode("utf-8", errors="ignore").strip()
            if head_txt and ("Netscape" not in head_txt) and ("# HTTP Cookie File" not in head_txt):
                log.warning("YT cookies file may be in a non-Netscape format: %s", path)
        except Exception:
            pass

    def _write_utf8_text(dst: str, content: str) -> None:
        # Normalize newlines and ensure UTF-8 text on disk
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    def _write_bytes_as_utf8(dst: str, data: bytes) -> None:
        # Try common encodings first (Railway/Linux expects UTF-8, but Windows exports can be cp1251/utf-16)
        for enc in ("utf-8", "utf-16", "utf-16le", "utf-16be", "cp1251", "latin-1"):
            try:
                txt = data.decode(enc)
                # Skip obviously wrong decodes that produce lots of NULLs
                if txt.count("\x00") > 10:
                    continue
                _write_utf8_text(dst, txt)
                return
            except Exception:
                continue
        # Fallback: replace undecodable bytes
        _write_utf8_text(dst, data.decode("utf-8", errors="replace"))

    # 1) URL variant
    url = (os.getenv("YT_COOKIES_URL") or "").strip()
    if url:
        try:
            import urllib.request
            tmp_path = _dst_path()
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            _write_bytes_as_utf8(tmp_path, data)
            _warn_if_suspicious(tmp_path)
            log.info("YT cookies (url) tayyor: %s (exists=%s, size=%s)", tmp_path, os.path.exists(tmp_path), os.path.getsize(tmp_path))
            return tmp_path
        except Exception as e:
            log.warning("YT_COOKIES_URL yuklab olish xatosi: %s", e)

    # 2) Plain-text env variant (recommended for Railway Variables)
    txt = os.getenv("YT_COOKIES_TEXT")
    if txt:
        try:
            tmp_path = _dst_path()
            # If UI stored literal "\n" characters, convert them to real newlines
            if "\\n" in txt and "\n" not in txt:
                txt = txt.replace("\\r\\n", "\n").replace("\\n", "\n")
            _write_utf8_text(tmp_path, txt)
            _warn_if_suspicious(tmp_path)
            log.info("YT cookies (text) tayyor: %s (exists=%s, size=%s)", tmp_path, os.path.exists(tmp_path), os.path.getsize(tmp_path))
            return tmp_path
        except Exception as e:
            log.warning("YT_COOKIES_TEXT write xatosi: %s", e)

    # 3) File path variant (optional)
    src = (os.getenv("YT_COOKIES_FILE") or "").strip()
    candidates: list[str] = []
    if src:
        candidates.append(src)
        candidates.append(os.path.join("/etc/secrets", os.path.basename(src)))
        candidates.append(os.path.basename(src))
    candidates += [
        "/etc/secrets/cookies.txt",
        "/etc/secrets/Cookies.txt",
        "/etc/secrets/cookies_youtube.txt",
        "cookies.txt",
        "Cookies.txt",
        "cookies_youtube.txt",
    ]

    src_path = None
    for p in candidates:
        try:
            if p and os.path.exists(p) and os.path.getsize(p) > 0:
                src_path = p
                break
        except Exception:
            continue

    if not src_path:
        if src:
            log.warning("YT_COOKIES_FILE topildi, lekin fayl yo'q: %s", src)
        return None

    try:
        tmp_path = _dst_path()
        with open(src_path, "rb") as f:
            data = f.read()
        _write_bytes_as_utf8(tmp_path, data)
        _warn_if_suspicious(tmp_path)
        log.info("YT cookies (file) tayyor: %s (exists=%s, size=%s, src=%s)", tmp_path, os.path.exists(tmp_path), os.path.getsize(tmp_path), src_path)
        return tmp_path
    except Exception as e:
        log.warning("YT cookies read/copy xatosi: %s", e)
        return None



def _normalize_proxy(raw: str) -> Optional[str]:
    """Validate and normalize proxy string from env.
    Accepts: http(s)://user:pass@host:port , socks5://host:port , etc.
    Returns normalized proxy URL or None if invalid.
    """
    if not raw:
        return None
    p = raw.strip()
    if not p:
        return None
    # If scheme missing, assume http
    if "://" not in p:
        p = "http://" + p
    try:
        u = urlparse(p)
        if u.scheme not in ("http", "https", "socks5", "socks5h"):
            return None
        # urlparse raises ValueError for bad port in py3.13 sometimes when accessing .port
        host = u.hostname
        if not host:
            return None
        try:
            port = u.port
        except Exception:
            return None
        if port is None:
            return None
    except Exception:
        return None
    return p

def _parse_js_runtimes_env(value: str) -> Dict[str, Dict[str, Any]]:
    """Parse YTDLP_JS_RUNTIME env into yt-dlp Python API format.

    yt-dlp (2026+) expects: dict of {runtime: {config}}
    Examples:
      - "deno" -> {"deno": {}}
      - "node" -> {"node": {}}
      - "node:/usr/bin/node" -> {"node": {"path": "/usr/bin/node"}}
      - "deno,node" -> {"deno": {}, "node": {}}
    """
    v = (value or "").strip()
    if not v:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for part in [p.strip() for p in v.split(",") if p.strip()]:
        if ":" in part:
            rt, pth = part.split(":", 1)
            rt = rt.strip()
            pth = pth.strip()
            if rt:
                cfg: Dict[str, Any] = {}
                if pth:
                    cfg["path"] = pth
                out[rt] = cfg
        else:
            out[part] = {}
    return out


def build_ydl_base(outtmpl: str, workdir: Optional[str] = None) -> Dict[str, Any]:
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": True,
        "continuedl": True,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "extractor_retries": 3,
        "nocheckcertificate": True,
        "buffersize": 1024 * 1024,
        "http_chunk_size": 10 * 1024 * 1024,
    }

    # Env overrides (Railway/Render)
    try:
        st = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "") or 0)
        if st > 0:
            opts["socket_timeout"] = st
    except Exception:
        pass
    try:
        rt = int(os.getenv("YTDLP_RETRIES", "") or 0)
        if rt > 0:
            opts["retries"] = rt
            opts["fragment_retries"] = rt
            opts["extractor_retries"] = max(1, rt // 3)
    except Exception:
        pass


    # Cookies (YouTube datacenter bloklari uchun foydali)
    cookiefile = _ensure_cookiefile(workdir)
    if cookiefile:
        opts["cookiefile"] = cookiefile

    # Let yt-dlp choose the supported YouTube clients.  Forcing web/android/ios
    # makes YouTube return only the 360p fallback on many datacenter IPs because
    # those clients require a per-video PO Token for DASH formats.
    opts.setdefault("extractor_args", {})
    opts["extractor_args"].setdefault("youtube", {})
    clients_env = (os.getenv("YTDLP_YT_CLIENTS") or "").strip()
    clients = ["default"]
    if clients_env and clients_env.lower() not in ("default",):
        log.warning(
            "YTDLP_YT_CLIENTS=%s ignored: using yt-dlp default clients to keep DASH formats available",
            clients_env,
        )
    opts["extractor_args"]["youtube"]["player_client"] = clients
    # HLS (m3u8) manifestlari баъзи тармоқларда manifest.googlevideo.com timeout бериши мумкин.
    # Шунинг учун (default) HLS'ни ўчириб, DASH форматлар билан ишлаймиз.
    # Ўчириб қўйиш: YTDLP_SKIP_HLS=0
    if os.getenv("YTDLP_SKIP_HLS", "0") == "1":
        ysk = opts["extractor_args"]["youtube"].get("skip")
        if not ysk:
            opts["extractor_args"]["youtube"]["skip"] = ["hls"]
        else:
            if isinstance(ysk, str):
                ysk = [ysk]
            if "hls" not in ysk:
                ysk.append("hls")
            opts["extractor_args"]["youtube"]["skip"] = ysk


    # HTTP headers (User-Agent / Accept-Language)
    opts.setdefault("http_headers", {})
    ua = (os.getenv("YTDLP_UA") or "").strip()
    if ua:
        opts["http_headers"]["User-Agent"] = ua
    else:
        # default browser UA
        opts["http_headers"].setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
    opts["http_headers"].setdefault("Accept-Language", "en-US,en;q=0.9")
    opts["http_headers"].setdefault("Referer", "https://www.youtube.com/")


    # Impersonate (ixtiyoriy): YTDLP_IMPERSONATE=chrome|chrome-124:windows-10|safari|...
    # Yangi yt-dlp (2026+) Python API'da opts["impersonate"] satri endi str emas, ImpersonateTarget bo‘lishi kerak.
    imp = (os.getenv("YTDLP_IMPERSONATE") or "").strip()
    if imp:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget  # type: ignore
            opts["impersonate"] = ImpersonateTarget.from_str(imp.lower())
        except Exception as e:
            # Agar kutubxona/target mos kelmasa, bot yiqilib qolmasligi uchun impersonate'ni o‘chirib yuboramiz.
            log.warning("Impersonate sozlamasi o‘chirildi (xato: %s). YTDLP_IMPERSONATE=%s", e, imp)
    # Proxy (ixtiyoriy): YTDLP_PROXY=http://user:pass@host:port
    proxy_raw = (os.getenv("YTDLP_PROXY") or "").strip()
    proxy = _normalize_proxy(proxy_raw)
    if proxy:
        opts["proxy"] = proxy
    elif proxy_raw:
        # noto‘g‘ri proxy bo‘lsa, bot yiqilmasin — proxy’ni e'tiborsiz qoldiramiz
        log.warning("YTDLP_PROXY noto‘g‘ri formatda, e'tiborsiz qoldirildi: %s", proxy_raw)


    # ffmpeg (merge/MP3 uchun) — Railway/Render'да PATH'da bo'lishi mumkin
    try:
        ff = shutil.which('ffmpeg')
        if ff:
            opts['ffmpeg_location'] = ff
    except Exception:
        pass

    # --- YouTube EJS / JS-challenge (formatlar yo‘qolib qolmasligi учун) ---
    # Ba'zi videolarda YouTube "bot-check" qilib, JS-challenge yechilmasa faqat storyboard (rasmlar) qolib ketadi.
    # Buni yechish uchun JS runtime (deno yoki node) va (kerak bo‘lsa) EJS remote component ruxsati kerak bo‘ladi.
    try:
        js_runtime_env = (os.getenv("YTDLP_JS_RUNTIME") or "").strip().lower()
        if js_runtime_env:
            opts["js_runtimes"] = _parse_js_runtimes_env(js_runtime_env)
        else:
            # avtomatik: avval deno, bo‘lmasa node
            if shutil.which("deno"):
                opts["js_runtimes"] = {"deno": {}}
            elif shutil.which("node"):
                opts["js_runtimes"] = {"node": {}}

        # Remote EJS komponentlarini (github) yuklashga ruxsat: kerak bo‘lsa challenge-solver skriptlarini oladi.
        # Istasangiz env bilan o‘chirib qo‘yasiz: YTDLP_REMOTE_EJS=0
        if os.getenv("YTDLP_REMOTE_EJS", "1") == "1":
            rc = opts.get("remote_components")
            if rc is None:
                rc = []
            if isinstance(rc, str):
                rc = [rc]
            if "ejs:github" not in rc:
                rc.append("ejs:github")
            opts["remote_components"] = rc
    except Exception:
        pass



    # Debug summary
    if os.getenv("YTDLP_DEBUG_FORMATS", "0") == "1":
        try:
            yt = (opts.get("extractor_args") or {}).get("youtube") or {}
            clients = yt.get("player_client")
            jsr = opts.get("js_runtimes") or {}
            log.info(
                "YTDLP debug cfg: player_client=%s js_runtimes=%s remote_components=%s proxy=%s cookiefile=%s",
                clients,
                list(jsr.keys()) if isinstance(jsr, dict) else jsr,
                opts.get("remote_components"),
                bool(opts.get("proxy")),
                bool(opts.get("cookiefile")),
            )
        except Exception:
            pass

    return opts

def _extract_info(url: str) -> Dict[str, Any]:
    # Formatlarni ko‘rsatish uchun to‘liq "process=True" kerak bo‘ladi,
    # aks holda ba'zan faqat audio ko‘rinib qoladi.
    ydl_opts = build_ydl_base(outtmpl="%(title)s.%(ext)s", workdir=tempfile.gettempdir())
    ydl_opts["ignore_no_formats_error"] = True
    ydl_opts["skip_download"] = True
    # This must match build_ydl_base(): a forced web/android/ios client list can
    # hide DASH formats and leave only the 360p progressive fallback.
    try:
        ydl_opts.setdefault("extractor_args", {})
        ydl_opts["extractor_args"].setdefault("youtube", {})
        clients_env = (os.getenv("YTDLP_YT_CLIENTS") or "").strip()
        if clients_env and clients_env.lower() not in ("default",):
            log.warning(
                "YTDLP_YT_CLIENTS=%s ignored while extracting formats; using yt-dlp defaults",
                clients_env,
            )
        ydl_opts["extractor_args"]["youtube"]["player_client"] = ["default"]
    except Exception:
        pass
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if os.getenv("YTDLP_DEBUG_FORMATS", "0") == "1":
                _yt_debug_dump_formats(info)
            return info
    except Exception as e:
        msg = str(e)
        if "Impersonate target" in msg and "not available" in msg:
            # Railway/host muhitida curl-cffi yoki kerakli handler bo‘lmasa, impersonate target mavjud bo‘lmay qoladi.
            ydl_opts.pop("impersonate", None)
            log.warning("Impersonate o‘chirildi (mavjud emas): %s", msg)
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        raise


def _select_youtube_formats(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pick a curated set of real, available video formats (no fake 1080/720 labels).

    We only show a resolution button if we can map it to an actual format height.
    This prevents the UI bug where multiple buttons show the same size (because they
    all fall back to the same single available stream).
    """
    formats = info.get("formats") or []
    duration = float(info.get("duration") or 0)

    vids: List[Dict[str, Any]] = []
    for f in formats:
        if not _is_real_youtube_video_format(f):
            continue
        h = _yt_height(f)
        if h <= 0:
            continue
        ff = f
        ff["_h"] = h
        # Ensure bitrate hints exist where possible (helps estimation)
        if ff.get("tbr") is None:
            # If only vbr/abr exist, keep as-is; otherwise leave None
            pass
        vids.append(ff)

    if not vids:
        return []

    # Pick the best representative for each height
    def fmt_score(ff: Dict[str, Any]):
        ext = (ff.get("ext") or "").lower()
        ext_score = 2 if ext == "mp4" else (1 if ext in ("webm", "mkv") else 0)
        br = max(float(ff.get("tbr") or 0), float(ff.get("vbr") or 0), float(ff.get("abr") or 0))
        fs = float(ff.get("filesize") or 0) + float(ff.get("filesize_approx") or 0)
        has_url = 1 if ff.get("url") else 0
        # Prefer formats that have a URL, then mp4, then higher bitrate/size.
        return (has_url, ext_score, br, fs)

    by_h: Dict[int, Dict[str, Any]] = {}
    for f in vids:
        h = int(f.get("_h") or 0)
        best = by_h.get(h)
        if best is None or fmt_score(f) > fmt_score(best):
            by_h[h] = f

    desired = [144, 240, 360, 480, 720, 1080]
    tol = {144: 60, 240: 80, 360: 90, 480: 110, 720: 160, 1080: 220}

    picked: List[Dict[str, Any]] = []
    used_ids: set = set()

    heights = sorted(by_h.keys())

    def pick_near(target: int) -> Optional[int]:
        if not heights:
            return None
        band = tol.get(target, 120)
        cands = [h for h in heights if abs(h - target) <= band]
        if not cands:
            return None
        # Closest height wins; tie -> higher resolution
        cands.sort(key=lambda h: (abs(h - target), -h))
        return cands[0]

    for target in desired:
        h = pick_near(target)
        if h is None:
            continue
        f = by_h[h]
        fid = str(f.get("format_id") or "")
        if not fid or fid in used_ids:
            continue
        used_ids.add(fid)
        f["_label_h"] = h
        picked.append(f)

    # If nothing matched the standard buckets, fall back to top few real heights
    if not picked:
        top_heights = sorted(heights, reverse=True)[:4]
        for h in top_heights:
            f = by_h[h]
            fid = str(f.get("format_id") or "")
            if not fid or fid in used_ids:
                continue
            used_ids.add(fid)
            f["_label_h"] = h
            picked.append(f)

    # Sort descending by label height for nicer UI (1080 → 144)
    picked.sort(key=lambda f: int(f.get("_label_h") or f.get("_h") or 0), reverse=True)
    return picked



def _download_video(url: str, format_id: Optional[str], workdir: str, has_audio: Optional[bool] = None) -> Path:
    """yt-dlp орқали видеони юклаб олиш.

    format_id:
      - рақам (YouTube itag) бўлса: шу форматни танлаймиз
      - 'h:720' каби бўлса: height cap (<=720) бўйича танлаймиз
      - None бўлса: bestvideo+bestaudio/best

    has_audio:
      - True  => format_id'нинг ўзида аудио бор (progressive)
      - False => формат видео-онли (аудиосиз)
      - None  => номаълум (safe fallback)
    """
    outtmpl = os.path.join(workdir, "%(title).200s.%(ext)s")

    def _run_with_opts(opts: Dict[str, Any]) -> Path:
        """Run yt-dlp download and return a non-empty file path from workdir."""
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

            candidates: List[Path] = []
            try:
                fp = ydl.prepare_filename(info)
                candidates.append(Path(fp))
            except Exception:
                pass

            req = info.get("requested_downloads") or info.get("requested_formats") or []
            for r in req:
                p = r.get("filepath") or r.get("filename")
                if p:
                    candidates.append(Path(p))

            files = [p for p in Path(workdir).iterdir() if p.is_file()]
            media = [p for p in files if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4a")]
            if media:
                candidates.insert(0, max(media, key=lambda p: p.stat().st_size))

            for p in candidates:
                try:
                    if p.exists() and p.stat().st_size > 0:
                        return p
                except Exception:
                    continue
        raise RuntimeError("Download finished but file not found")

    ydl_opts = build_ydl_base(outtmpl=outtmpl, workdir=workdir)
    ydl_opts.update({
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    })

    # 1) Height-cap pseudo: h:720
    if format_id and str(format_id).startswith("h:"):
        try:
            h = int(str(format_id).split(":", 1)[1])
        except Exception:
            h = 720
        ydl_opts["format"] = (f"bestvideo*[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"f"bestvideo*[height<={h}]+bestaudio/"f"best[height<={h}][ext=mp4]/best[height<={h}]")
        return _run_with_opts(ydl_opts)

    # 2) Exact itag
    if format_id:
        fid = str(format_id).strip()
        if has_audio is True:
            # Progressive format (audio+video). Don't silently fall back to another quality.
            ydl_opts["format"] = f"{fid}"
        else:
            # Video-only stream: keep the selected video stream and add the best audio.
            # Again, no silent fallback to another video quality.
            ydl_opts["format"] = f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio"
        return _run_with_opts(ydl_opts)

    # 3) Ultimate fallback
    ydl_opts["format"] = "bestvideo*+bestaudio/best"
    return _run_with_opts(ydl_opts)


def _download_audio(url: str, workdir: str) -> Path:
    outtmpl = os.path.join(workdir, "%(id)s.%(ext)s")

    ydl_opts = build_ydl_base(outtmpl=outtmpl, workdir=workdir)
    ydl_opts["format"] = "bestaudio/best"
    ydl_opts["postprocessors"] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }]
    try:
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            msg = str(e)
            if "Impersonate target" in msg and "not available" in msg:
                ydl_opts.pop("impersonate", None)
                log.warning("Impersonate o‘chirildi (mavjud emas): %s", msg)
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
            else:
                raise
        mp3s = sorted(Path(workdir).glob("*.mp3"), key=lambda x: x.stat().st_mtime, reverse=True)
        if mp3s:
            return mp3s[0]
    except Exception as e:
        log.warning("MP3 konvertatsiya muvaffaqiyatsiz (ffmpeg yo'q bo'lishi mumkin). Fallback audio: %s", e)

    ydl_opts2 = build_ydl_base(outtmpl=outtmpl, workdir=workdir)
    ydl_opts2["format"] = "bestaudio/best"
    try:
        with YoutubeDL(ydl_opts2) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        msg = str(e)
        if "Impersonate target" in msg and "not available" in msg:
            ydl_opts2.pop("impersonate", None)
            log.warning("Impersonate o‘chirildi (mavjud emas): %s", msg)
            with YoutubeDL(ydl_opts2) as ydl:
                info = ydl.extract_info(url, download=True)
        else:
            raise
        fp = ydl.prepare_filename(info)
        p = Path(fp)
        if p.exists():
            return p
        files = sorted(Path(workdir).glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("Audio fayl topilmadi")
        return files[0]


# ---------------------------- Bot Handlers ----------------------------

def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and (user_id in ADMIN_IDS)

def start_text_by_lang(lang: str) -> str:
    return START_TEXT_RU if lang == LANG_RU else START_TEXT_UZ

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    # start bosganlarni DBga yozib boramiz
    await STORE.touch_user(update.effective_user)

    # Avval saqlangan til bo'lsa — shuni ishlatamiz, bo'lmasa default uz
    lang = await STORE.get_lang(update.effective_user.id)
    context.user_data["lang"] = lang

    kb = [[
        InlineKeyboardButton(_t(LANG_UZ, "btn_uz"), callback_data="lang|uz"),
        InlineKeyboardButton(_t(LANG_RU, "btn_ru"), callback_data="lang|ru"),
    ]]

    await update.message.reply_text(
        start_text_by_lang(lang),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def on_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user:
        return

    data = q.data or ""
    parts = data.split("|", maxsplit=1)
    if len(parts) != 2:
        return
    lang = parts[1].strip().lower()
    if lang not in (LANG_UZ, LANG_RU):
        lang = LANG_UZ

    # Saqlaymiz
    context.user_data["lang"] = lang
    await STORE.set_lang(q.from_user, lang)

    # Javob
    try:
        await q.answer()
    except Exception:
        pass

    kb = [[
        InlineKeyboardButton(_t(LANG_UZ, "btn_uz"), callback_data="lang|uz"),
        InlineKeyboardButton(_t(LANG_RU, "btn_ru"), callback_data="lang|ru"),
    ]]
    markup = InlineKeyboardMarkup(kb)

    try:
        await q.edit_message_text(
            start_text_by_lang(lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except Exception:
        # Agar edit bo'lmasa, yangi xabar yuboramiz
        try:
            await context.bot.send_message(
                chat_id=q.message.chat_id if q.message else update.effective_chat.id,
                text=start_text_by_lang(lang),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except Exception:
            pass


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if update.message:
        await update.message.reply_text(f"ID: `{uid}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_cacheclear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if uid not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("❌ Admin emas.")
        return
    FILEID_CACHE.clear()
    YOUTUBE_FILEID_CACHE.clear()
    if update.message:
        await update.message.reply_text("✅ file_id cache tozalandi.")

async def cmd_cacheprune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    if uid not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("❌ Admin emas.")
        return
    removed = 0
    removed += _prune_fileid_cache(FILEID_CACHE, max_items=FILEID_CACHE_MAX)
    removed += _prune_fileid_cache(YOUTUBE_FILEID_CACHE, max_items=YOUTUBE_FILEID_CACHE_MAX)
    if update.message:
        await update.message.reply_text(f"✅ Cache prune: {removed} ta o‘chirildi.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = update.effective_user.id if update.effective_user else None
    lang = await get_user_lang(update, context)

    if not is_admin(uid):
        await update.message.reply_text(_t(lang, "not_admin"))
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(_t(lang, "usage_broadcast"))
        return

    msg = parts[1].strip()
    users = await STORE.get_users()
    sent = 0
    failed = 0

    await update.message.reply_text(_t(lang, "bc_started", n=len(users)))
    for u in users:
        try:
            await context.bot.send_message(chat_id=u, text=msg, disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(_t(lang, "bc_done", sent=sent, failed=failed))

async def cmd_broadcastpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = update.effective_user.id if update.effective_user else None
    lang = await get_user_lang(update, context)

    if not is_admin(uid):
        await update.message.reply_text(_t(lang, "not_admin"))
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(_t(lang, "usage_broadcastpost"))
        return

    src: Message = update.message.reply_to_message
    users = await STORE.get_users()
    sent = 0
    failed = 0

    await update.message.reply_text(_t(lang, "bcpost_started", n=len(users)))
    for u in users:
        try:
            await context.bot.copy_message(chat_id=u, from_chat_id=src.chat_id, message_id=src.message_id)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(_t(lang, "bc_done", sent=sent, failed=failed))


async def cmd_broadcastgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = update.effective_user.id if update.effective_user else None
    lang = await get_user_lang(update, context)

    if not is_admin(uid):
        await update.message.reply_text(_t(lang, "not_admin"))
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(_t(lang, "usage_broadcastgroup"))
        return

    msg = parts[1].strip()
    groups = await STORE.get_groups()
    sent = 0
    failed = 0

    await update.message.reply_text(_t(lang, "bcgroup_started", n=len(groups)))
    for cid in groups:
        try:
            await context.bot.send_message(chat_id=cid, text=msg, disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(_t(lang, "bc_done", sent=sent, failed=failed))


async def cmd_broadcastpostgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    uid = update.effective_user.id if update.effective_user else None
    lang = await get_user_lang(update, context)

    if not is_admin(uid):
        await update.message.reply_text(_t(lang, "not_admin"))
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(_t(lang, "usage_broadcastpostgroup"))
        return

    src: Message = update.message.reply_to_message
    groups = await STORE.get_groups()
    sent = 0
    failed = 0

    await update.message.reply_text(_t(lang, "bcpostgroup_started", n=len(groups)))
    for cid in groups:
        try:
            await context.bot.copy_message(chat_id=cid, from_chat_id=src.chat_id, message_id=src.message_id)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(_t(lang, "bc_done", sent=sent, failed=failed))

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    # user touch (langni majburan o'zgartirmaymiz)
    await STORE.touch_user(update.effective_user)

    # chat touch (guruhlar ro‘yxatini saqlash: /broadcastgroup uchun)
    try:
        if update.effective_chat:
            await STORE.touch_chat(update.effective_chat)
    except Exception:
        pass

    url = extract_first_url(update.message.text or "")
    if not url:
        return

    # TikTok short links (vt.tiktok.com/...) ni to‘liq URL ga yechib olamiz,
    # shunda /photo/ postlarni to‘g‘ri aniqlash mumkin.
    url_eff = url
    if is_tiktok(url):
        u_low = url.lower()
        if any(x in u_low for x in ("vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/")):
            loop = asyncio.get_running_loop()
            url_eff = await loop.run_in_executor(None, _resolve_final_url, url)
        url_eff = _strip_query(url_eff)

    lang = await get_user_lang(update, context)

    origin_chat_id = update.message.chat_id
    origin_message_id = update.message.message_id

    if is_youtube(url):
        msg = await update.message.reply_text(_t(lang, "yt_fetching"))
        asyncio.create_task(
            _task_show_youtube_formats(
                context=context,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                url=url,
                origin_chat_id=origin_chat_id,
                origin_message_id=origin_message_id,
                lang=lang,
            )
        )
    else:
        # TikTok photo-post (/photo/) — bu turda faqat audio (MP3) taklif qilamiz
        if is_tiktok_photo(url_eff):
            token_p = _cache_put({
                "url": url_eff, "kind": "tt_photo_audio", "format_id": None,
                "origin_chat_id": origin_chat_id, "origin_message_id": origin_message_id,
                "lang": lang,
            })
            kb = [[InlineKeyboardButton(_t(lang, "btn_mp3"), callback_data=f"dl|{token_p}")]]
            await update.message.reply_text(_t(lang, "tt_photo_audio_only"), reply_markup=InlineKeyboardMarkup(kb))
            return

        url_for_dl = url_eff if is_tiktok(url) else url

        kb = []
        t_v = _cache_put({
            "url": url_for_dl, "kind": "video", "format_id": None,
            "origin_chat_id": origin_chat_id, "origin_message_id": origin_message_id,
            "lang": lang,
        })
        t_a = _cache_put({
            "url": url_for_dl, "kind": "audio", "format_id": None,
            "origin_chat_id": origin_chat_id, "origin_message_id": origin_message_id,
            "lang": lang,
        })
        kb.append([InlineKeyboardButton(_t(lang, "btn_video"), callback_data=f"dl|{t_v}")])
        kb.append([InlineKeyboardButton(_t(lang, "btn_audio"), callback_data=f"dl|{t_a}")])
        await update.message.reply_text(_t(lang, "choose"), reply_markup=InlineKeyboardMarkup(kb))


async def _task_show_youtube_formats(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    url: str,
    origin_chat_id: int,
    origin_message_id: int,
    lang: str,
) -> None:
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, _extract_info, url)
        formats = _select_youtube_formats(info)
        try:
            raw_fmts = info.get("formats") or []
            heights = sorted({int(_yt_height(f) or 0) for f in raw_fmts if _is_real_youtube_video_format(f) and int(_yt_height(f) or 0) > 0}, reverse=True)
            log.info("YT formats: total=%d unique_video_heights=%s", len(raw_fmts), heights[:20])
        except Exception:
            pass


        # Agar yt-dlp формат метамаълумотлари тўлиқ келмаса (ёки 1 та форматгина чиқса),
        # UI барибир 144/240/360/480/720/1080 вариантларни кўрсатади.
        # Бу вариантлар "h:XXX" pseudo format бўлиб, юклаш пайтида height cap сифатида ишлатилади.
        # Лекин ҳажмни кўрсатиш учун real форматдан (height<=cap) битрейт/хажмни тахмин қиламиз.
                # NOTE: only show real formats discovered by yt-dlp (no pseudo h:XXX options).

# Buttonlar: real formatlar (mavjud bo‘lsa)
        btns: List[InlineKeyboardButton] = []
        for f in sorted(formats, key=lambda x: int(x.get("_label_h") or x.get("_h") or x.get("height") or 0), reverse=True):
            fmt_id = str(f.get("format_id"))
            h = int(f.get("_label_h") or f.get("_h") or f.get("height") or 0)
            label_h = h

            has_audio = str(f.get("acodec") or "").lower() not in ("", "none")
            ytid = str(info.get("id") or "")
            yt_key = f"yt:{ytid}:{label_h}p" if ytid else None

            # Size label: avoid misleading identical sizes when per-format size is unknown.
            fmt_for_size = f
            if str(fmt_id).startswith("h:"):
                # For pseudo "height cap" buttons, compute size from the best real format under that cap.
                best_f = _best_video_format_under_height(info, label_h)
                if best_f:
                    fmt_for_size = best_f

            total_bytes = _video_total_size_bytes_strict(info, fmt_for_size)
            size = human_mb_compact(total_bytes) if total_bytes > 0 else ""
            # If size is approximate (filesize_approx/bitrate estimate), show "~" to avoid confusion.
            approx = False
            if total_bytes > 0:
                approx = _format_size_is_approx(info, fmt_for_size)
                if (fmt_for_size.get("acodec") == "none") or not fmt_for_size.get("acodec"):
                    _a_sz, _a_apx = _best_audio_size_bytes_meta(info)
                    if _a_sz > 0 and _a_apx:
                        approx = True
            if size and approx:
                size = "~" + size
            label = f"{label_h}p - {size}" if size else f"{label_h}p"

            token = _cache_put({
                "url": url, "kind": "video", "format_id": fmt_id,
                "has_audio": has_audio,
                "yt_key": yt_key,
                "total_bytes": int(total_bytes) if total_bytes else 0,
                "total_bytes": int(total_bytes) if total_bytes else 0,
                "origin_chat_id": origin_chat_id, "origin_message_id": origin_message_id,
                "lang": lang,
            })
            btns.append(InlineKeyboardButton(label, callback_data=f"dl|{token}"))

        # 2-column layout (rasmdagidek)
        kb: List[List[InlineKeyboardButton]] = []
        for i in range(0, len(btns), 2):
            kb.append(btns[i:i+2])

        token_a = _cache_put({
            "url": url, "kind": "audio", "format_id": None,
            "origin_chat_id": origin_chat_id, "origin_message_id": origin_message_id,
            "lang": lang,
        })
        kb.append([InlineKeyboardButton("🎵 MP3", callback_data=f"dl|{token_a}")])

        # Placeholder "formatlar olinmoqda" xabarini o‘chirib, oblojka (thumbnail) bilan yuboramiz
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

        title_raw = (info.get("title") or "YouTube").strip()
        # Caption limit uchun title’ni qisqartiramiz
        if len(title_raw) > 200:
            title_raw = title_raw[:197] + "..."
        title = html.escape(title_raw)
        dur = human_duration(info.get("duration"))

        caption = _t(lang, "yt_caption", title=title, dur=dur)
        thumb_url = _pick_best_thumbnail_url(info)

        try:
            if thumb_url:
                await context.bot.send_photo(
                    chat_id=origin_chat_id,
                    photo=thumb_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(kb),
                    reply_to_message_id=origin_message_id,
                )
            else:
                await context.bot.send_message(
                    chat_id=origin_chat_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(kb),
                    reply_to_message_id=origin_message_id,
                )
        except Exception:
            # Thumbnail yuborilmasa ham — text bilan yuboramiz
            await context.bot.send_message(
                chat_id=origin_chat_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb),
                reply_to_message_id=origin_message_id,
            )

    except Exception as e:
        log.exception("Formatlarni olishda xato: %s", e)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_t(lang, "fmt_error", err=_friendly_ydl_error(e, lang)),
            )
        except Exception:
            pass



async def on_download_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    q = update.callback_query

    # callback timeout bo'lmasligi uchun darhol javob beramiz
    lang = LANG_UZ
    if q.from_user:
        context.user_data.setdefault("lang", await STORE.get_lang(q.from_user.id))
        lang = context.user_data.get("lang", LANG_UZ)
    try:
        await q.answer(_t(lang, "downloading_answer"), show_alert=False)
    except Exception:
        pass

    data = q.data or ""
    if not data.startswith("dl|"):
        return

    token = data.split("|", maxsplit=1)[1]
    payload = _cache_get(token)
    if not payload:
        try:
            # Eski tugma
            if q.message:
                await q.edit_message_text(_t(lang, "btn_expired"))
        except Exception:
            pass
        return

    # Payload topildi
    url = payload["url"]
    kind = payload["kind"]
    format_id = payload.get("format_id")
    has_audio = payload.get("has_audio")
    yt_key = payload.get("yt_key")
    lang = payload.get("lang") or lang

    # YouTube format hajm cheklovi (default: 150MB). Katta bo'lsa — yuklamaymiz va format menyusini o'chirmaymiz.
    if kind == "video":
        total_bytes = int(payload.get("total_bytes") or 0)
        if total_bytes > 0 and total_bytes > (YT_MAX_MB * 1024 * 1024):
            size_mb = int((total_bytes + (1024 * 1024 - 1)) // (1024 * 1024))
            msg_text = _t(lang, "yt_too_big", size=size_mb, max=YT_MAX_MB)
            try:
                await q.answer(msg_text, show_alert=True)
            except Exception:
                pass
            # Ba'zi klientlarda alert ko'rinmasligi mumkin — shuning uchun reply bilan ham yuboramiz
            try:
                target_chat_id = int(payload.get("origin_chat_id") or (q.message.chat_id if q.message else update.effective_chat.id))
                reply_to = int(payload.get("origin_message_id")) if str(payload.get("origin_message_id")).isdigit() else None
                await context.bot.send_message(chat_id=target_chat_id, text=msg_text, reply_to_message_id=reply_to)
            except Exception:
                pass
            return

    # Format menyusini (tugmalar) xabarini avtomat o‘chirib yuboramiz
    try:
        if q.message is not None:
            await context.bot.delete_message(chat_id=q.message.chat_id, message_id=q.message.message_id)
    except Exception:
        pass

    origin_chat_id = int(payload.get("origin_chat_id") or (q.message.chat_id if q.message else update.effective_chat.id))
    origin_message_id = payload.get("origin_message_id")
    reply_to_message_id = int(origin_message_id) if str(origin_message_id).isdigit() else None

    # "⏳ ..." ogohlantirishni alohida yuboramiz ва yuklab bo‘lganda o‘chirib tashlaymiz
    status_chat_id: Optional[int] = None
    status_message_id: Optional[int] = None
    try:
        m = await context.bot.send_message(
            chat_id=origin_chat_id,
            text=_t(lang, "downloading_wait"),
            reply_to_message_id=reply_to_message_id,
        )
        status_chat_id = m.chat_id
        status_message_id = m.message_id
    except Exception:
        pass

    asyncio.create_task(_task_download_and_send(
        context=context,
        chat_id=origin_chat_id,
        reply_to_message_id=reply_to_message_id,
        url=url,
        kind=kind,
        format_id=format_id,
        has_audio=has_audio,
        yt_key=yt_key,
        lang=lang,
        status_chat_id=status_chat_id,
        status_message_id=status_message_id,
    ))

async def _send_audio_with_retry(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to_message_id: Optional[int],
) -> Optional[Message]:
    last_exc: Optional[Exception] = None
    for _ in range(2):
        try:
            with open(path, "rb") as f:
                msg = await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id,
                )
            return msg
        except TimedOut as e:
            last_exc = e
            await asyncio.sleep(2)
    if last_exc:
        raise last_exc
    raise RuntimeError("send_audio failed")

async def _send_video_with_retry(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to_message_id: Optional[int],
):
    """Видео юбориш (2 марта retry) ва Message'ни қайтариш (file_id кеш учун)."""
    last_exc: Optional[Exception] = None
    for _ in range(2):
        try:
            with open(path, "rb") as f:
                msg = await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    supports_streaming=True,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id,
                )
            return msg
        except TimedOut as e:
            last_exc = e
            await asyncio.sleep(2)
    if last_exc:
        raise last_exc
    raise RuntimeError("send_video failed")

async def _send_document_with_retry(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to_message_id: Optional[int],
) -> None:
    last_exc: Optional[Exception] = None
    for _ in range(2):
        try:
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id,
                )
            return
        except TimedOut as e:
            last_exc = e
            await asyncio.sleep(2)
    if last_exc:
        raise last_exc


def _download_tiktok_photos_zip(url: str, workdir: str) -> Path:
    """Download TikTok /photo/ post images with gallery-dl and pack into ZIP."""
    outdir = Path(workdir) / "tiktok_photos"
    outdir.mkdir(parents=True, exist_ok=True)

    # gallery-dl CLI (pip orqali o‘rnatiladi). requirements.txt ga: gallery-dl
    try:
        subprocess.run(
            ["gallery-dl", "-D", str(outdir), url],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        raise RuntimeError("gallery-dl topilmadi. requirements.txt ga 'gallery-dl' qo‘shing va redeploy qiling.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gallery-dl xato: {e.stderr.strip()[:300] if e.stderr else e}")

    imgs: List[Path] = []
    for p in outdir.rglob("*"):
        if p.is_file() and p.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]:
            imgs.append(p)

    if not imgs:
        raise RuntimeError("TikTok foto topilmadi (ehtimol captcha/blok).")

    zip_path = Path(workdir) / "tiktok_photos.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(imgs):
            z.write(p, arcname=p.name)

    return zip_path


def _download_tiktok_photo_audio(url: str, workdir: str) -> Path:
    """Best-effort: TikTok /photo/ postdan audio (MP3) chiqarib beradi.

    1) /photo/ID -> /video/ID ko‘rinishiga aylantirib yt-dlp orqali audio
    2) Agar bo‘lmasa, gallery-dl orqali medialarni tushirib, eng katta mp4/m4a dan audio ajratadi.
    """
    clean = _strip_query(url)
    # 1) Urinib ko‘ramiz: /photo/<id> -> /video/<id>
    video_variant = re.sub(r"/photo/([0-9]+)/?$", r"/video/\1", clean)

    try:
        return _download_audio(video_variant, workdir)
    except Exception as e1:
        # ba'zi hollarda original URL ham ishlashi mumkin
        try:
            return _download_audio(clean, workdir)
        except Exception:
            pass

        # 2) Fallback: gallery-dl
        outdir = Path(workdir) / "tiktok_media"
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["gallery-dl", "-D", str(outdir), clean],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError:
            # e1 ni yo‘qotmaslik uchun kerakli hint beramiz
            raise RuntimeError(
                "TikTok foto-post audio uchun 'gallery-dl' kerak. requirements.txt ga 'gallery-dl' qo‘shing va redeploy qiling. "
                f"Asl xato: {str(e1)[:200]}"
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"gallery-dl xato: {e.stderr.strip()[:300] if e.stderr else e}")

        candidates: List[Path] = []
        for p in outdir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in [".m4a", ".mp3", ".aac", ".ogg", ".webm", ".mp4"]:
                candidates.append(p)

        if not candidates:
            raise RuntimeError("TikTok media topilmadi (ehtimol captcha/blok).")

        src = max(candidates, key=lambda p: p.stat().st_size)
        if src.suffix.lower() in [".mp3", ".m4a", ".aac", ".ogg"]:
            return src

        # mp4/webm bo‘lsa, audio ajratamiz
        out_mp3 = Path(workdir) / "tiktok_audio.mp3"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # ffmpeg yo‘q bo‘lsa, bor formatni qaytaramiz (Telegram audio sifatida ham yuboriladi)
            return src

        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(src), "-vn", "-acodec", "libmp3lame", "-b:a", "192k", str(out_mp3)],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return out_mp3
        except subprocess.CalledProcessError:
            # oxirgi urinish: audio streamni copy qilib ko‘ramiz
            out_m4a = Path(workdir) / "tiktok_audio.m4a"
            subprocess.run(
                [ffmpeg, "-y", "-i", str(src), "-vn", "-c:a", "copy", str(out_m4a)],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return out_m4a




async def _task_download_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to_message_id: Optional[int],
    url: str,
    kind: str,
    format_id: Optional[str],
    has_audio: Optional[bool],
    yt_key: Optional[str],
    lang: str,
    status_chat_id: Optional[int] = None,
    status_message_id: Optional[int] = None,
) -> None:
    loop = asyncio.get_running_loop()
    try:
        async with DOWNLOAD_SEM:
            with tempfile.TemporaryDirectory(prefix="dlbot_") as td:
                caption = _t(lang, "caption_suffix")

                if kind == "audio":
                    # file_id кеш: шу URL аввал юборилган бўлса, қайта юклаб олмасдан юбориш
                    key = _make_fileid_cache_key(url, kind)
                    fid = _cache_get_fileid(FILEID_CACHE, key)
                    if fid:
                        try:
                            await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=fid,
                                caption=caption,
                                reply_to_message_id=reply_to_message_id,
                            )
                            return
                        except Exception:
                            FILEID_CACHE.pop(key, None)

                    path: Path = await loop.run_in_executor(None, _download_audio, url, td)

                    # Bot ички лимити (RAM/traffic тежаш): 130MB (default) дан катта бўлса юбормаймиз
                    try:
                        size_mb = path.stat().st_size / (1024 * 1024)
                    except Exception:
                        size_mb = 0.0
                    if DL_MAX_MB > 0 and size_mb > DL_MAX_MB:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=_t(lang, "yt_too_big", size=int(size_mb + 0.999), max=DL_MAX_MB),
                            reply_to_message_id=reply_to_message_id,
                        )
                        return

                    msg = await _send_audio_with_retry(context, chat_id, path, caption, reply_to_message_id)
                    try:
                        if msg and getattr(msg, "audio", None) is not None:
                            _cache_put_fileid(FILEID_CACHE, key, msg.audio.file_id, FILEID_CACHE_MAX)
                    except Exception:
                        pass

                elif kind == "tt_photo_audio":
                    key = _make_fileid_cache_key(url, kind)
                    fid = _cache_get_fileid(FILEID_CACHE, key)
                    if fid:
                        try:
                            await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=fid,
                                caption=caption,
                                reply_to_message_id=reply_to_message_id,
                            )
                            return
                        except Exception:
                            FILEID_CACHE.pop(key, None)

                    path = await loop.run_in_executor(None, _download_tiktok_photo_audio, url, td)

                    try:
                        size_mb = path.stat().st_size / (1024 * 1024)
                    except Exception:
                        size_mb = 0.0
                    if DL_MAX_MB > 0 and size_mb > DL_MAX_MB:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=_t(lang, "yt_too_big", size=int(size_mb + 0.999), max=DL_MAX_MB),
                            reply_to_message_id=reply_to_message_id,
                        )
                        return

                    msg = await _send_audio_with_retry(context, chat_id, path, caption, reply_to_message_id)
                    try:
                        if msg and getattr(msg, "audio", None) is not None:
                            _cache_put_fileid(FILEID_CACHE, key, msg.audio.file_id, FILEID_CACHE_MAX)
                    except Exception:
                        pass

                else:
                    # 1) Universal file_id кеш (барча тармоқлар). Агар шу URL/формат аввал юборилган бўлса — дарҳол юборилади.
                    key = _make_fileid_cache_key(url, "video", format_id=format_id, yt_key=yt_key)
                    fid = _cache_get_fileid(FILEID_CACHE, key)
                    if fid:
                        try:
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=fid,
                                supports_streaming=True,
                                caption=caption,
                                reply_to_message_id=reply_to_message_id,
                            )
                            return
                        except Exception:
                            FILEID_CACHE.pop(key, None)

                    if yt_key:
                        fid_cached = _cache_get_fileid(YOUTUBE_FILEID_CACHE, yt_key)
                        if fid_cached:
                            try:
                                await context.bot.send_video(
                                    chat_id=chat_id,
                                    video=fid_cached,
                                    supports_streaming=True,
                                    caption=caption,
                                    reply_to_message_id=reply_to_message_id,
                                )
                                return
                            except Exception:
                                YOUTUBE_FILEID_CACHE.pop(yt_key, None)

                    # 2) Юклаб оламиз
                    path = await loop.run_in_executor(None, _download_video, url, format_id, td, has_audio)

                    # 3) Upload лимити (api.telegram.org учун одатда ~50MB). Local Bot API server бўлса TG_MAX_UPLOAD_MB'ни катта қилиб қўйинг.
                    try:
                        size_mb = path.stat().st_size / (1024 * 1024)
                    except Exception:
                        size_mb = 0.0

                    # Bot ички лимити: 130MB (default). Telegram лимити катта бўлса ҳам шу ерда тўхтатамиз.
                    if DL_MAX_MB > 0 and size_mb > DL_MAX_MB:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=_t(lang, "yt_too_big", size=int(size_mb + 0.999), max=DL_MAX_MB),
                            reply_to_message_id=reply_to_message_id,
                        )
                        return

                    if TG_MAX_UPLOAD_MB > 0 and size_mb > TG_MAX_UPLOAD_MB:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=_t(
                                lang,
                                "err_generic",
                                err=(
                                    f"Файл ҳажми {size_mb:.1f}MB. Telegram Bot API upload чеклови туфайли юборилмади (лимит: {TG_MAX_UPLOAD_MB}MB). "
                                    "Пастроқ формат танланг ёки Local Bot API server ишлатинг."
                                ),
                            ),
                            reply_to_message_id=reply_to_message_id,
                        )
                        return

                    # 4) Юбориш ва file_id кешлаш
                    msg = await _send_video_with_retry(context, chat_id, path, caption, reply_to_message_id)
                    try:
                        if msg and getattr(msg, "video", None) is not None:
                            _cache_put_fileid(FILEID_CACHE, key, msg.video.file_id, FILEID_CACHE_MAX)
                    except Exception:
                        pass
                    if yt_key and msg and getattr(msg, "video", None) is not None:
                        try:
                            _cache_put_fileid(YOUTUBE_FILEID_CACHE, yt_key, msg.video.file_id, YOUTUBE_FILEID_CACHE_MAX)
                        except Exception:
                            pass

    except Exception as e:
        log.exception("Download/send xato: %s", e)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=_t(lang, "err_generic", err=_friendly_ydl_error(e, lang)),
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            pass
    finally:
        if status_chat_id and status_message_id:
            try:
                await context.bot.delete_message(chat_id=status_chat_id, message_id=status_message_id)
            except Exception:
                pass


# ---------------------------- App lifecycle ----------------------------
async def _post_init(app):
    await STORE.init()
    try:
        users = await STORE.get_users()
        log.info("Users loaded: %d", len(users))
    except Exception:
        pass

async def _post_shutdown(app):
    await STORE.close()

def build_app():
    # Telegram upload vaqtida timeout kamayиши учун timeoutlarni kattalashtiramiz
    # Local Bot API bilan катта файл юборилганда write_timeout каттароқ бўлиши керак.
    request = HTTPXRequest(connect_timeout=30, read_timeout=900, write_timeout=900, pool_timeout=30)

    builder = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
    )

    # Agar LOCAL_BOT_API_URL берилса — bot API'ни локал сервер орқали ишлатамиз.
    # Масалан: http://telegram-bot-api.railway.internal:8081
    local_api = (os.getenv("LOCAL_BOT_API_URL") or "").strip()
    if local_api:
        local_api = local_api.rstrip("/")
        builder = builder.base_url(f"{local_api}/bot").base_file_url(f"{local_api}/file/bot")
        log.info("Telegram API endpoint: %s (LOCAL BOT API)", local_api)
    else:
        log.info("Telegram API endpoint: https://api.telegram.org (cloud)")

    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cacheclear", cmd_cacheclear))
    app.add_handler(CommandHandler("cacheprune", cmd_cacheprune))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("broadcastpost", cmd_broadcastpost))
    app.add_handler(CommandHandler("broadcastgroup", cmd_broadcastgroup))
    app.add_handler(CommandHandler("broadcastpostgroup", cmd_broadcastpostgroup))

    app.add_handler(CallbackQueryHandler(on_lang_button, pattern=r"^lang\|"))
    app.add_handler(CallbackQueryHandler(on_download_button, pattern=r"^dl\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    return app


def main() -> None:
    app = build_app()
    log.info("Bot started. Admins: %s", ",".join(str(x) for x in sorted(ADMIN_IDS)) if ADMIN_IDS else "(not set)")

    mode = RUN_MODE
    if mode not in ("webhook", "polling"):
        mode = "webhook" if WEBHOOK_URL_BASE else "polling"

    if mode == "webhook":
        if not WEBHOOK_URL_BASE:
            raise RuntimeError("RUN_MODE=webhook, lekin WEBHOOK_URL (yoki RENDER_EXTERNAL_URL) topilmadi")

        full_webhook_url = WEBHOOK_URL_BASE.rstrip("/") + "/" + WEBHOOK_PATH
        log.info("Webhook mode. URL: %s", full_webhook_url)

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Polling mode.")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
