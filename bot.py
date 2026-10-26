import os
import re
import sys
import json
import time
import pickle
import asyncio
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import telegram
import yt_dlp
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.error import Conflict
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    PicklePersistence,
    PersistenceInput,
    filters,
)

BOT_VERSION = "final-2026-10-26"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID", "").strip()  # رقم حسابك على تيليجرام (اختياري)
MAX_UPLOAD = 45 * 1024 * 1024
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
STATE_FILE = os.getenv("BOT_STATE_FILE", "bot_state.pickle")
STATS_PATH = Path(os.getenv("BOT_USERS_FILE", "bot_users.json"))

# أقصى عدد تنزيلات متزامنة (حتى لا تتعب الاستضافة)
DOWNLOAD_SLOTS = asyncio.Semaphore(2)

VIDEO_BUTTON = "🎬 تنزيل فيديو"
AUDIO_BUTTON = "🎵 تنزيل MP3"
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(VIDEO_BUTTON), KeyboardButton(AUDIO_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="اختر نوع التنزيل أو أرسل الرابط...",
)

SUPPORT_TEXT = (
    "✅ المدعوم: إنستغرام وفيسبوك (فيديو أو MP3)\n"
    "🚫 يوتيوب غير مدعوم حاليًا"
)

# ---------------------------------------------------------------
# تخزين المشتركين (أرقام الحسابات فقط، بدون روابط ولا رسائل)
# ---------------------------------------------------------------

DATA = {"users": {}, "video": 0, "audio": 0}


def log(message):
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def day_string(days_ago=0):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days_ago * 86400))


def load_data():
    global DATA
    try:
        if STATS_PATH.exists():
            with STATS_PATH.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
                DATA = {
                    "users": loaded["users"],
                    "video": int(loaded.get("video", 0)),
                    "audio": int(loaded.get("audio", 0)),
                }
        log(f"USERS_FILE: users={len(DATA['users'])} path={STATS_PATH}")
    except Exception as exc:
        log(f"USERS_FILE: could not read ({type(exc).__name__}); starting fresh")


def save_data():
    try:
        tmp = STATS_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False)
        os.replace(tmp, STATS_PATH)
    except Exception as exc:
        log(f"USERS_FILE: could not save ({type(exc).__name__})")


def track_user(update):
    """يسجّل المستخدم. يرجع True إذا كان جديدًا."""
    user = update.effective_user
    if user is None or user.is_bot:
        return False
    uid = str(user.id)
    today = day_string()
    entry = DATA["users"].get(uid)
    if entry is None:
        DATA["users"][uid] = {"f": today, "l": today}
        save_data()
        return True
    if entry.get("l") != today:
        entry["l"] = today
        save_data()
    return False


def count_job(kind):
    key = "audio" if kind == "audio" else "video"
    DATA[key] = int(DATA.get(key, 0)) + 1
    save_data()


def is_owner(update):
    user = update.effective_user
    return bool(user) and OWNER_ID.isdigit() and str(user.id) == OWNER_ID


async def register_user(update, context):
    """يسجّل المستخدم وينبّه المالك إذا كان جديدًا."""
    is_new = track_user(update)
    if is_new:
        total = len(DATA["users"])
        log(f"NEW_USER: total={total}")
        if OWNER_ID.isdigit() and not is_owner(update):
            try:
                await context.bot.send_message(
                    chat_id=int(OWNER_ID),
                    text=f"👤 مشترك جديد! الإجمالي الآن: {total}",
                )
            except Exception as exc:
                log(f"NOTIFY_ERROR: {exc.__class__.__name__}")
    return is_new


# ---------------------------------------------------------------
# أدوات عامة
# ---------------------------------------------------------------

def mode_title(kind):
    return "🎵 MP3" if kind == "audio" else "🎬 فيديو"


def clean_error(exc):
    text = str(exc) or exc.__class__.__name__
    text = ANSI_RE.sub("", text)
    if TOKEN:
        text = text.replace(TOKEN, "[hidden token]")
    return text[:700]


def is_youtube(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return (
        host in ("youtu.be", "youtube.com", "youtube-nocookie.com")
        or host.endswith(".youtube.com")
    )


def detect_mode_button(text):
    """يرجع video أو audio إذا كانت الرسالة ضغطة على أحد الزرين."""
    t = text.strip()
    if len(t) > 40:
        return None
    if t == VIDEO_BUTTON or "تنزيل فيديو" in t:
        return "video"
    if t == AUDIO_BUTTON or "تنزيل MP3" in t or "mp3" in t.lower():
        return "audio"
    return None


# ---------------------------------------------------------------
# التنزيل وتجهيز الملف
# ---------------------------------------------------------------

def download_media(url, kind, folder):
    output = str(Path(folder) / "media.%(ext)s")
    options = {
        "outtmpl": output,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 1,
        "fragment_retries": 2,
        "max_filesize": 500 * 1024 * 1024,
    }

    if kind == "audio":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        })
    else:
        options.update({
            "format": (
                "best[height<=480][ext=mp4][acodec!=none]"
                "/best[height<=480][acodec!=none]"
                "/best[ext=mp4]/best"
            ),
            "merge_output_format": "mp4",
        })

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [
        p for p in Path(folder).iterdir()
        if p.is_file()
        and p.name.startswith("media.")
        and not p.name.endswith(".part")
    ]
    if not files:
        raise RuntimeError("لم يُعثر على الملف بعد التنزيل.")

    return max(files, key=lambda p: p.stat().st_size)


def prepare_file(media, folder, kind):
    if media.stat().st_size <= MAX_UPLOAD:
        return media

    log("STATS: file is large; compressing...")
    output = str(Path(folder) / (
        "compressed.mp4" if kind == "video" else "compressed.mp3"
    ))

    if kind == "audio":
        command = [
            "ffmpeg", "-y", "-i", str(media),
            "-vn", "-b:a", "96k", output
        ]
    else:
        command = [
            "ffmpeg", "-y", "-i", str(media),
            "-vf", "scale=-2:480",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "450k", "-maxrate", "500k",
            "-bufsize", "1000k",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart",
            output
        ]

    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("برنامج ffmpeg غير مثبت على الاستضافة.") from None
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "ignore")[-300:]
        raise RuntimeError(f"فشل ضغط الملف: {tail}") from None

    result = Path(output)
    if not result.exists():
        raise RuntimeError("فشل تجهيز الملف للإرسال.")
    if result.stat().st_size > MAX_UPLOAD:
        raise RuntimeError(
            "الملف أكبر من حد الإرسال حتى بعد الضغط. "
            "جرّب فيديو أقصر."
        )

    log(f"STATS: compressed_size={result.stat().st_size} bytes")
    return result


# ---------------------------------------------------------------
# الأوامر
# ---------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_new = await register_user(update, context)

    # الفيديو هو الوضع الافتراضي لأي محادثة جديدة
    kind = context.user_data.setdefault("kind", "video")
    chat = update.effective_chat
    log(f"START: chat={chat.id if chat else '?'} mode={kind} new={is_new}")

    if is_new:
        user = update.effective_user
        name = (user.first_name or "").strip()[:30] if user else ""
        greeting = f"أهلًا وسهلًا {name}! 🎉" if name else "أهلًا وسهلًا! 🎉"
        text = (
            f"{greeting}\n"
            "يسعدنا انضمامك إلى بوت التنزيل 🤍\n\n"
            "أقدر أساعدك في تنزيل:\n"
            "📸 فيديوهات إنستغرام\n"
            "📘 فيديوهات فيسبوك\n"
            "🎵 أو استخراج الصوت MP3 منها\n\n"
            "طريقة الاستخدام:\n"
            "1️⃣ اختر 🎬 فيديو أو 🎵 MP3 من الأزرار أسفل المحادثة\n"
            "2️⃣ أرسل رابط الفيديو\n"
            "3️⃣ انتظر قليلًا وسيصلك الملف\n\n"
            "سيبقى الوضع الذي اخترته فعالًا حتى تغيّره.\n\n"
            f"{SUPPORT_TEXT}\n\n"
            f"الوضع الحالي: {mode_title(kind)}"
        )
    else:
        text = (
            "أهلًا بعودتك! 👋\n\n"
            "اختر من الأزرار أسفل المحادثة ثم أرسل الرابط.\n\n"
            f"{SUPPORT_TEXT}\n\n"
            f"الوضع الحالي: {mode_title(kind)}\n"
            f"🔖 نسخة البوت: {BOT_VERSION}"
        )

    await update.effective_message.reply_text(
        text,
        reply_markup=MAIN_KEYBOARD,
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    await update.effective_message.reply_text(
        f"رقم حسابك على تيليجرام:\n{user.id}",
        reply_markup=MAIN_KEYBOARD,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not OWNER_ID.isdigit():
        await message.reply_text(
            "لم يتم تحديد مالك البوت بعد.\n"
            "أرسل /myid ثم ضع رقمك في متغير OWNER_ID بالاستضافة.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if not is_owner(update):
        await message.reply_text(
            "هذا الأمر لمالك البوت فقط.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    users = DATA["users"]
    today = day_string()
    week_start = day_string(6)

    total = len(users)
    new_today = sum(1 for u in users.values() if u.get("f") == today)
    active_today = sum(1 for u in users.values() if u.get("l") == today)
    active_week = sum(1 for u in users.values() if str(u.get("l", "")) >= week_start)

    await message.reply_text(
        "📊 إحصائيات البوت\n\n"
        f"👥 إجمالي المشتركين: {total}\n"
        f"🆕 جدد اليوم: {new_today}\n"
        f"🟢 نشطون اليوم: {active_today}\n"
        f"📅 نشطون آخر 7 أيام: {active_week}\n\n"
        f"🎬 فيديوهات أُرسلت: {DATA.get('video', 0)}\n"
        f"🎵 ملفات MP3 أُرسلت: {DATA.get('audio', 0)}\n\n"
        "المشترك = كل من فتح البوت. التواريخ بتوقيت UTC.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    if message is None or not message.text:
        return

    await register_user(update, context)

    text = message.text.strip()
    chat = update.effective_chat
    chat_id = chat.id if chat else "?"

    match = URL_PATTERN.search(text)

    # ضغطة على أحد زرّي الكيبورد (قبل البحث عن رابط)
    if not match:
        chosen = detect_mode_button(text)
        if chosen:
            context.user_data["kind"] = chosen
            log(f"MODE: chat={chat_id} mode={chosen}")
            if chosen == "video":
                await message.reply_text(
                    "🎬 تم اختيار تنزيل الفيديو.\n"
                    "أرسل رابط فيديو من إنستغرام أو فيسبوك.",
                    reply_markup=MAIN_KEYBOARD,
                )
            else:
                await message.reply_text(
                    "🎵 تم اختيار استخراج MP3.\n"
                    "أرسل رابط فيديو من إنستغرام أو فيسبوك لاستخراج صوته.",
                    reply_markup=MAIN_KEYBOARD,
                )
            return

        await message.reply_text(
            "أرسل رابطًا يبدأ بـ https:// أو http://، "
            "أو اختر نوع التنزيل من الأزرار أسفل المحادثة.\n\n"
            f"{SUPPORT_TEXT}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    url = match.group(0).rstrip(".,;!?)]}")
    kind = context.user_data.get("kind", "video")
    log(f"JOB: chat={chat_id} mode={kind}")

    if is_youtube(url):
        await message.reply_text(
            "🚫 روابط يوتيوب غير مدعومة حاليًا.\n\n"
            "المدعوم: إنستغرام وفيسبوك (فيديو أو MP3).",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    status = await message.reply_text(
        "⏳ بدأت عملية التنزيل. قد يستغرق إنستغرام بعض الوقت...",
        reply_markup=MAIN_KEYBOARD,
    )

    async with DOWNLOAD_SLOTS:
        try:
            with tempfile.TemporaryDirectory(prefix="media_bot_") as folder:
                started = time.monotonic()
                media = await asyncio.to_thread(
                    download_media, url, kind, folder
                )
                download_seconds = time.monotonic() - started

                log(
                    f"STATS: download={download_seconds:.1f}s "
                    f"size={media.stat().st_size} bytes"
                )

                try:
                    await status.edit_text(
                        "✅ اكتمل التنزيل.\n"
                        f"⏱️ وقت التنزيل: {download_seconds:.1f} ثانية\n"
                        "📤 أجهّز الملف للإرسال إلى تيليجرام..."
                    )
                except Exception as edit_error:
                    log(f"STATUS_ERROR: {edit_error.__class__.__name__}")

                media = await asyncio.to_thread(
                    prepare_file, media, folder, kind
                )

                upload_started = time.monotonic()
                with media.open("rb") as file:
                    if kind == "audio":
                        await message.reply_audio(
                            audio=file,
                            filename=media.name,
                            reply_markup=MAIN_KEYBOARD,
                            read_timeout=600,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60,
                        )
                    else:
                        await message.reply_video(
                            video=file,
                            filename=media.name,
                            supports_streaming=True,
                            reply_markup=MAIN_KEYBOARD,
                            read_timeout=600,
                            write_timeout=600,
                            connect_timeout=60,
                            pool_timeout=60,
                        )

                upload_seconds = time.monotonic() - upload_started
                log(f"STATS: upload={upload_seconds:.1f}s")
                count_job(kind)

            try:
                await status.edit_text(
                    "✅ تم إرسال الملف بنجاح!\n"
                    f"⏬ وقت التنزيل: {download_seconds:.1f} ثانية\n"
                    f"📤 وقت الإرسال: {upload_seconds:.1f} ثانية"
                )
            except Exception as edit_error:
                log(f"STATUS_ERROR: {edit_error.__class__.__name__}")

        except Exception as exc:
            error = clean_error(exc)
            log(f"ERROR: mode={kind} {error}")

            # التعديل على رسالة قديمة ما بيحمل لوحة الأزرار،
            # فنحذف رسالة الحالة ونرسل الخطأ كرسالة جديدة مع الأزرار.
            try:
                await status.delete()
            except Exception:
                pass

            try:
                await message.reply_text(
                    "❌ لم تكتمل العملية.\n\n"
                    f"السبب: {error}\n\n"
                    "قد يكون الرابط خاصًا، أو يتطلب تسجيل الدخول، "
                    "أو أن المنصة تقيّد الطلبات، أو أن الاتصال بتيليجرام انقطع.",
                    reply_markup=MAIN_KEYBOARD,
                )
            except Exception as reply_error:
                log(f"REPLY_ERROR: {reply_error.__class__.__name__}")


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
):
    error = context.error
    if isinstance(error, Conflict):
        log(
            "CONFLICT: في نسخة ثانية من البوت شغالة بنفس التوكن. "
            "أوقف النسخة الأخرى (استضافة قديمة أو جهاز ثاني)."
        )
        return
    log(f"BOT_ERROR: {type(error).__name__}: {error}")


async def post_init(app: Application):
    me = await app.bot.get_me()
    log(f"BOT_USERNAME=@{me.username}")
    log(f"OWNER_ID: {'set' if OWNER_ID.isdigit() else 'NOT SET'}")
    log(f"READY: version={BOT_VERSION}")


def build_persistence():
    """حفظ الوضع المختار بملف حتى يبقى بعد إعادة التشغيل."""
    path = Path(STATE_FILE)
    try:
        if path.exists():
            with path.open("rb") as f:
                pickle.load(f)
        persistence = PicklePersistence(
            filepath=str(path),
            store_data=PersistenceInput(
                bot_data=False,
                chat_data=False,
                callback_data=False,
            ),
        )
        log(f"STATE: saving mode to {path}")
        return persistence
    except Exception as exc:
        log(f"STATE: persistence disabled ({type(exc).__name__})")
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        return None


def main():
    log(
        f"BOOT: version={BOT_VERSION} "
        f"python={sys.version.split()[0]} "
        f"ptb={telegram.__version__}"
    )

    if not TOKEN:
        raise RuntimeError(
            "متغير TELEGRAM_BOT_TOKEN غير موجود. "
            "لم يتم تغيير التوكن أو طباعته."
        )

    load_data()

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=60,
        read_timeout=600,
        write_timeout=600,
        pool_timeout=60,
    )

    builder = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .concurrent_updates(8)
        .post_init(post_init)
    )

    persistence = build_persistence()
    if persistence is not None:
        builder = builder.persistence(persistence)

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    app.add_error_handler(error_handler)

    log("البوت يعمل. أوقفه باستخدام Ctrl+C.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
