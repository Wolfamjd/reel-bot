"""Telegram bot: send it a TikTok / Instagram Reel / YouTube Shorts link, it replies with the video.

Settings come from a .env file next to this script (or environment variables):
  BOT_TOKEN   the token from @BotFather (required)
  OWNER_ID    your Telegram user id; if empty, the first person to message the bot becomes the owner
  COOKIES     optional path to a cookies.txt (helps with Instagram when it asks for a login)
"""
import asyncio
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
COOKIES = os.environ.get("COOKIES", "")


def owner_id() -> int | None:
    v = os.environ.get("OWNER_ID") or (OWNER_FILE.read_text().strip() if OWNER_FILE.exists() else "")
    return int(v) if v.isdigit() else None


async def download(url: str, folder: str) -> Path:
    args = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--restrict-filenames", "-N", "8",
        # MP4 (H.264) plays and saves to the iPhone Photos app.
        "-f", "bv*[ext=mp4][vcodec^=avc]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "-S", f"filesize:{MAX_BYTES // (1024 * 1024)}M",
        "--merge-output-format", "mp4",
        "-o", os.path.join(folder, "%(id)s.%(ext)s"),
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
    _, err = await proc.communicate()
    files = [p for p in Path(folder).iterdir() if p.is_file()]
    if proc.returncode != 0 or not files:
        raise RuntimeError(err.decode(errors="ignore").strip().splitlines()[-1] if err else "download failed")
    return max(files, key=lambda p: p.stat().st_size)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    owner = owner_id()
    if owner is None:
        OWNER_FILE.write_text(str(user.id))
        owner = user.id
        await msg.reply_text("✅ صرت مالك البوت. أرسل أي رابط مقطع وأرجّعه لك.")
    if user.id != owner:
        return  # private bot: ignore everyone else

    urls = URL_RE.findall(msg.text or msg.caption or "")
    if not urls:
        await msg.reply_text("أرسل رابط تيك توك أو ريلز انستقرام 🎬")
        return

    for url in urls:
        status = await msg.reply_text("⏳ جاري التحميل…")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await download(url, tmp)
                if path.stat().st_size > MAX_BYTES:
                    await status.edit_text("❌ المقطع أكبر من 50MB، تلقرام ما يسمح للبوت يرسله.")
                    continue
                with path.open("rb") as f:
                    await msg.reply_video(f, supports_streaming=True, read_timeout=300, write_timeout=300)
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ ما قدرت أحمّل المقطع.\n{str(e)[:300]}")


def main() -> None:
    if not TOKEN:
        sys.exit("BOT_TOKEN is missing: put it in .env")
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_message))
    # On Render (free) the service sleeps when idle; a webhook wakes it up when you send a link.
    public_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if public_url:
        port = int(os.environ.get("PORT", "10000"))
        secret = TOKEN.split(":")[0] + "x" + str(abs(hash(TOKEN)) % 10**8)
        path = "tg-" + TOKEN.split(":")[-1][-12:]
        print(f"Bot running (webhook) on port {port}.")
        app.run_webhook(listen="0.0.0.0", port=port, url_path=path,
                        webhook_url=f"{public_url.rstrip('/')}/{path}", secret_token=secret)
    else:
        print("Bot running. Press Ctrl+C to stop.")
        app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
