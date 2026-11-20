"""Telegram bot: send it a TikTok / Instagram Reel / YouTube Shorts link, it replies with the video.

Settings come from a .env file next to this script (or environment variables):
  BOT_TOKEN   the token from @BotFather (required)
  OWNER_ID    your Telegram user id; if empty, the first person to message the bot becomes the owner
  COOKIES     optional path to a cookies.txt (helps with Instagram when it asks for a login)
"""
import asyncio
import hashlib
import hmac
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

HERE = Path(__file__).resolve().parent
URL_RE = re.compile(r"https?://\S+")
MAX_BYTES = 49 * 1024 * 1024  # Telegram bots can send up to 50 MB


def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_FILE = HERE / "owner_id.txt"
# Render "Secret Files" land in /etc/secrets; a cookies.txt there is used automatically.
COOKIES = os.environ.get("COOKIES", "") or ("/etc/secrets/cookies.txt" if Path("/etc/secrets/cookies.txt").exists() else "")


def friendly_error(err: str) -> str:
    e = err.lower()
    if "not a bot" in e or "sign in to confirm" in e:
        return "❌ يوتيوب منع السيرفر وطلب تسجيل دخول.\nالحل: إضافة ملف cookies من حساب يوتيوب ثانوي في Render."
    if any(k in e for k in ("private", "login required", "log in", "logged-in", "empty media", "not granting access")):
        return "❌ المقطع خاص أو يحتاج تسجيل دخول، ما أقدر أحمّله."
    if "unsupported url" in e:
        return "❌ هذا الرابط مو مدعوم. أرسل رابط مقطع من تيك توك، إنستقرام، X أو يوتيوب."
    if "removed" in e or "unavailable" in e or "not available" in e:
        return "❌ المقطع محذوف أو غير متاح."
    return "❌ ما قدرت أحمّل المقطع. جرّب مرة ثانية بعد شوي."


def owner_id() -> int | None:
    v = os.environ.get("OWNER_ID") or (OWNER_FILE.read_text().strip() if OWNER_FILE.exists() else "")
    return int(v) if v.isdigit() else None


async def download(url: str, folder: str) -> tuple[Path, dict]:
    args = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--restrict-filenames", "-N", "8", "--no-simulate",
        # Highest resolution first, H.264 preferred (plays and saves to the iPhone Photos app),
        # staying under Telegram's 50 MB limit for bots.
        "-f", "bv*+ba/b",
        "-S", f"res,vcodec:h264,acodec:aac,filesize~{MAX_BYTES // (1024 * 1024)}M",
        "--merge-output-format", "mp4",
        "-o", os.path.join(folder, "%(id)s.%(ext)s"),
        "--print", "after_move:%(width)s|%(height)s|%(duration)s",
    ]
    if shutil.which("ffmpeg") is None:
        try:
            import imageio_ffmpeg
            args += ["--ffmpeg-location", imageio_ffmpeg.get_ffmpeg_exe()]
        except Exception:
            pass
    if COOKIES and Path(COOKIES).exists():
        args += ["--cookies", COOKIES]
    args.append(url)
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    files = [p for p in Path(folder).iterdir() if p.is_file()]
    if proc.returncode != 0 or not files:
        raise RuntimeError(err.decode(errors="ignore").strip().splitlines()[-1] if err else "download failed")
    meta: dict = {}
    lines = out.decode(errors="ignore").strip().splitlines()
    if lines:
        parts = lines[-1].split("|")
        for key, val in zip(("width", "height", "duration"), parts):
            try:
                meta[key] = int(float(val))
            except ValueError:
                pass
    return max(files, key=lambda p: p.stat().st_size), meta


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    owner = owner_id()
    if owner is None:
        OWNER_FILE.write_text(str(user.id))
        owner = user.id
        await msg.reply_text(f"✅ صرت مالك البوت. أرسل أي رابط مقطع وأرجّعه لك.\nرقمك: {user.id}")
    if user.id != owner:
        return  # private bot: ignore everyone else

    urls = URL_RE.findall(msg.text or msg.caption or "")
    if not urls:
        await msg.reply_text("أرسل رابط تيك توك أو ريلز انستقرام 🎬")
        return

    for url in urls:
        await send_link(context.bot, msg.chat_id, url)


async def send_link(bot, chat_id: int, url: str) -> None:
    """Download one link and send the video to chat_id."""
    status = await bot.send_message(chat_id, "⏳ جاري التحميل…")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path, meta = await download(url, tmp)
            if path.stat().st_size > MAX_BYTES:
                await status.edit_text("❌ المقطع أكبر من 50MB، تلقرام ما يسمح للبوت يرسله.")
                return
            with path.open("rb") as f:
                # Real width/height so Telegram shows it in its true shape and quality.
                await bot.send_video(chat_id, f, supports_streaming=True, read_timeout=300, write_timeout=300, **meta)
            await status.delete()
        except Exception as e:
            print(f"download failed for {url}: {e}", flush=True)
            await status.edit_text(friendly_error(str(e)), disable_web_page_preview=True)


def shortcut_key() -> str:
    """The key the iPhone Shortcut sends; derived from the bot token, so nothing extra to set up."""
    return hashlib.sha256(("shortcut:" + TOKEN).encode()).hexdigest()[:24]


async def serve_web(app: Application, public_url: str) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    port = int(os.environ.get("PORT", "10000"))
    secret = hashlib.sha256(("webhook:" + TOKEN).encode()).hexdigest()[:32]
    tg_path = "/tg-" + hashlib.sha256(TOKEN.encode()).hexdigest()[:16]

    async def telegram(request: Request) -> Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status_code=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def shortcut(request: Request) -> Response:
        # iPhone Shortcut: POST /dl with {"key": "...", "url": "..."} (or the same as query/form fields).
        data: dict = dict(request.query_params)
        try:
            if request.method == "POST":
                if "json" in request.headers.get("content-type", ""):
                    data.update(await request.json())
                else:
                    data.update(dict(await request.form()))
        except Exception:
            pass
        if not hmac.compare_digest(str(data.get("key", "")), shortcut_key()):
            return PlainTextResponse("wrong key", status_code=403)
        owner = owner_id()
        if owner is None:
            return PlainTextResponse("send the bot a message first", status_code=409)
        urls = URL_RE.findall(str(data.get("url", "")))
        if not urls:
            return PlainTextResponse("no link", status_code=400)
        for url in urls:
            asyncio.create_task(send_link(app.bot, owner, url))
        return PlainTextResponse("✅ وصل الرابط، المقطع جاي في تلقرام")

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    web = Starlette(routes=[
        Route(tg_path, telegram, methods=["POST"]),
        Route("/dl", shortcut, methods=["GET", "POST"]),
        Route("/", health),
    ])
    server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=port, log_level="warning"))
    async with app:
        await app.bot.set_webhook(public_url.rstrip("/") + tg_path, secret_token=secret)
        await app.start()
        print(f"Bot running (webhook) on port {port}.")
        await server.serve()
        await app.stop()


def main() -> None:
    if not TOKEN:
        sys.exit("BOT_TOKEN is missing: put it in .env")
    if len(sys.argv) > 1 and sys.argv[1] == "key":
        print(shortcut_key())
        return
    public_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    builder = Application.builder().token(TOKEN).concurrent_updates(True)
    if public_url:
        builder = builder.updater(None)
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_message))
    if public_url:
        # On Render (free) the service sleeps when idle; a request wakes it up.
        asyncio.run(serve_web(app, public_url))
    else:
        print("Bot running. Press Ctrl+C to stop.")
        app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
