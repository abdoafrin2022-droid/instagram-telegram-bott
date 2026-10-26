import os
import re
import time
import asyncio
import tempfile
import subprocess
from pathlib import Path

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_UPLOAD = 45 * 1024 * 1024
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


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
        if p.is_file() and p.name.startswith("media.")
        and not p.name.endswith(".part")
    ]
    if not files:
        raise RuntimeError("لم يُعثر على الملف بعد التنزيل.")

    return max(files, key=lambda p: p.stat().st_size)


def prepare_file(media, folder, kind):
    if media.stat().st_size <= MAX_UPLOAD:
        return media

    print("STATS: file is large; compressing...", flush=True)
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

    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    result = Path(output)
    if not result.exists():
        raise RuntimeError("فشل تجهيز الملف للإرسال.")
    if result.stat().st_size > MAX_UPLOAD:
        raise RuntimeError(
            "الملف أكبر من حد الإرسال حتى بعد الضغط. "
            "جرّب فيديو أقصر."
        )

    print(
        f"STATS: compressed_size={result.stat().st_size} bytes",
        flush=True,
    )
    return result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🎬 تنزيل فيديو", callback_data="video"),
            InlineKeyboardButton("🎵 استخراج MP3", callback_data="audio"),
        ]
    ]
    await update.message.reply_text(
        "أهلًا بك! اختر ما تريد تنزيله، ثم أرسل رابط المنشور أو الفيديو.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_format(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    context.user_data["kind"] = query.data

    if query.data == "audio":
        message = "🎵 أرسل رابط الفيديو الذي تريد استخراج الصوت منه بصيغة MP3."
    else:
        message = "🎬 أرسل رابط الفيديو الذي تريد تنزيله."

    await query.message.reply_text(message)


async def handle_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text or ""
    match = URL_PATTERN.search(text)

    if not match:
        await update.message.reply_text(
            "أرسل رابطًا يبدأ بـ https:// أو http://"
        )
        return

    url = match.group(0).rstrip(".,;!?)\u005d}")
    kind = context.user_data.get("kind", "video")
    status = await update.message.reply_text(
        "⏳ بدأت عملية التنزيل. قد يستغرق إنستغرام بعض الوقت..."
    )

    try:
        with tempfile.TemporaryDirectory(prefix="media_bot_") as folder:
            started = time.monotonic()
            media = await asyncio.to_thread(
                download_media, url, kind, folder
            )
            download_seconds = time.monotonic() - started

            print(
                f"STATS: download={download_seconds:.1f}s "
                f"size={media.stat().st_size} bytes",
                flush=True,
            )

            await status.edit_text(
                "✅ اكتمل التنزيل.\n"
                f"⏱️ وقت التنزيل: {download_seconds:.1f} ثانية\n"
                "📤 أجهّز الملف للإرسال إلى تيليجرام..."
            )

            media = await asyncio.to_thread(
                prepare_file, media, folder, kind
            )

            upload_started = time.monotonic()
            with media.open("rb") as file:
                if kind == "audio":
                    await update.message.reply_audio(
                        audio=file,
                        filename=media.name,
                        read_timeout=600,
                        write_timeout=600,
                        connect_timeout=60,
                        pool_timeout=60,
                    )
                else:
                    await update.message.reply_video(
                        video=file,
                        filename=media.name,
                        supports_streaming=True,
                        read_timeout=600,
                        write_timeout=600,
                        connect_timeout=60,
                        pool_timeout=60,
                    )

            upload_seconds = time.monotonic() - upload_started
            print(
                f"STATS: upload={upload_seconds:.1f}s",
                flush=True,
            )
            await status.edit_text(
                "✅ تم إرسال الملف بنجاح!\n"
                f"⏬ وقت التنزيل: {download_seconds:.1f} ثانية\n"
                f"📤 وقت الإرسال: {upload_seconds:.1f} ثانية"
            )

    except Exception as exc:
        error = str(exc).replace(TOKEN or "", "[hidden token]")
        error = error[:700]
        print(f"ERROR: {error}", flush=True)
        try:
            await status.edit_text(
                "❌ لم تكتمل العملية.\n\n"
                f"السبب: {error}\n\n"
                "قد يكون الرابط خاصًا، أو يتطلب تسجيل الدخول، "
                "أو أن إنستغرام يقيّد الطلبات، أو أن الاتصال بتيليجرام انقطع."
            )
        except Exception as status_error:
            print(
                f"STATUS_ERROR: {status_error.__class__.__name__}",
                flush=True,
            )


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
):
    error = context.error
    print(
        f"BOT_ERROR: {type(error).__name__}: {error}",
        flush=True,
    )


def main():
    if not TOKEN:
        raise RuntimeError(
            "متغير TELEGRAM_BOT_TOKEN غير موجود. "
            "لم يتم تغيير التوكن أو طباعته."
        )

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=60,
        read_timeout=600,
        write_timeout=600,
        pool_timeout=60,
    )

    app = Application.builder().token(TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(choose_format))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    app.add_error_handler(error_handler)

    print("البوت يعمل. أوقفه باستخدام Ctrl+C.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
