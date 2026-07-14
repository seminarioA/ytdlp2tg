import asyncio
import html
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
TG_LOCAL_URL = os.environ.get("TG_LOCAL_URL", "")   # vacío → usa api.telegram.org
MAX_BYTES = 1_900 * 1024 * 1024 if TG_LOCAL_URL else 49 * 1024 * 1024

# { (chat_id, msg_id): url }  — guardado hasta que el usuario elige calidad
PENDING: dict[tuple[int, int], str] = {}

_FALLBACK_FORMATS = [(1080, 1920), (720, 1280), (480, 854), (360, 640)]


def _fmt_for_height(h: int) -> str:
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/best[height<={h}][ext=mp4]/best[height<={h}]/best"
    )

_PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%\s+of\s+([\d.]+)(MiB|KiB|GiB).*?ETA\s+(\S+)')

_ERROR_PATTERNS = [
    (re.compile(r'版权地区受限|not available in your (country|region)|geo.?block|geo.?restrict|only available in', re.I),
     "El video no está disponible en la region del servidor."),
    (re.compile(r'private video|video is private', re.I),
     "El video es privado."),
    (re.compile(r'video unavailable|has been removed|no longer available|been deleted', re.I),
     "El video no existe o fue eliminado."),
    (re.compile(r'age.?restrict|confirm your age|you must be', re.I),
     "El video tiene restriccion de edad y requiere inicio de sesion."),
    (re.compile(r'login|sign in|log in|authentication|requires? an? account', re.I),
     "El video requiere inicio de sesion."),
    (re.compile(r'copyright|copyright claim', re.I),
     "El video fue bloqueado por derechos de autor."),
    (re.compile(r'unable to extract|unsupported url|no video formats', re.I),
     "No se pudo extraer el video. La URL puede no ser compatible."),
    (re.compile(r'429|too many requests|rate.?limit', re.I),
     "Demasiadas solicitudes. Intenta de nuevo en unos minutos."),
]

def _friendly_error(raw: str) -> str:
    for pattern, msg in _ERROR_PATTERNS:
        if pattern.search(raw):
            return msg
    return None
# Títulos genéricos que yt-dlp genera cuando no hay título real (ej. "TikTok video #123")
_GENERIC_TITLE_RE = re.compile(r'^.+\s+video\s+#[\w-]+$', re.IGNORECASE)


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _fmt_duration(seconds) -> str:
    try:
        total_s = int(float(seconds))
    except (ValueError, TypeError):
        return str(seconds)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _build_caption(tmpdir: str) -> str:
    info_files = list(Path(tmpdir).glob("*.info.json"))
    if not info_files:
        return ""
    try:
        info = json.loads(info_files[0].read_text())
    except Exception:
        return ""

    raw_title     = (info.get("title") or "").strip()
    description   = (info.get("description") or "").strip()
    uploader      = (info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()
    uploader_url  = (info.get("uploader_url") or info.get("channel_url") or "").strip()
    webpage_url   = (info.get("webpage_url") or "").strip()
    duration      = info.get("duration") or info.get("duration_string")
    view_count    = info.get("view_count")
    like_count    = info.get("like_count")
    comment_count = info.get("comment_count")
    repost_count  = info.get("repost_count")
    extractor     = (info.get("extractor_key") or info.get("extractor") or "").split(":")[0]

    if not extractor and webpage_url:
        m = re.search(r'(?:www\.)?([^./]+)\.[a-z]{2,}', webpage_url)
        if m:
            extractor = m.group(1).capitalize()

    title = "" if _GENERIC_TITLE_RE.match(raw_title) else raw_title

    lines = []

    # Bloque 1: contenido (descripción o título)
    if description and description != raw_title:
        if title:
            lines.append(f"<b>{html.escape(title[:200])}</b>")
        lines.append(html.escape(description[:300]))
    elif title:
        lines.append(f"<b>{html.escape(title[:300])}</b>")

    # Bloque 2: cuenta + link a la plataforma
    lines.append("")
    if uploader:
        account_text = f"👤 <b>{html.escape(uploader)}</b>"
        if uploader_url:
            lines.append(f'<a href="{html.escape(uploader_url)}">{account_text}</a>')
        else:
            lines.append(account_text)
    video_id = (info.get("id") or "").strip()
    if webpage_url:
        link_text = html.escape(video_id) if video_id else html.escape(extractor or webpage_url[:40])
        lines.append(f'<a href="{html.escape(webpage_url)}">{link_text}</a>')

    # Bloque 3: métricas, una por línea
    lines.append("")
    if view_count:
        lines.append(f"👁 {_fmt_count(view_count)}")
    if like_count:
        lines.append(f"❤️ {_fmt_count(like_count)}")
    if comment_count:
        lines.append(f"💬 {_fmt_count(comment_count)}")
    if repost_count:
        lines.append(f"🔁 {_fmt_count(repost_count)}")

    return "\n".join(lines).strip()[:1024]


class _ProgressFile:
    """File wrapper that tracks bytes read so upload progress can be shown."""
    def __init__(self, path: Path):
        self._f = open(path, "rb")
        self.total = path.stat().st_size
        self.sent = 0

    def read(self, n=-1):
        chunk = self._f.read(n)
        self.sent += len(chunk)
        return chunk

    def close(self): self._f.close()
    def seek(self, *a): return self._f.seek(*a)
    def tell(self): return self._f.tell()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _bar(pct: float) -> str:
    filled = int(pct / 10)
    return "█" * filled + "░" * (10 - filled)


async def _fetch_formats(url: str) -> list[tuple[int, int]]:
    """Returns available (height, width) pairs descending by height, bucketed to 1080/720/480/360."""
    cmd = [
        "yt-dlp", "--dump-json", "--no-playlist",
        "--extractor-args", "youtube:player_client=android,web",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    try:
        info = json.loads(stdout)
    except Exception:
        return []

    # Collect best width per height for video-only or video+audio formats
    best: dict[int, int] = {}
    for f in (info.get("formats") or []):
        h, w = f.get("height"), f.get("width")
        if h and w and f.get("vcodec", "none") != "none":
            if h not in best or w > best[h]:
                best[h] = w

    # Bucket into 1080/720/480/360 — pick the best format that fits each bucket
    result, seen = [], set()
    for bucket in (1080, 720, 480, 360):
        candidates = [(h, w) for h, w in best.items() if h <= bucket]
        if not candidates:
            continue
        h, w = max(candidates, key=lambda x: x[0])
        if h not in seen:
            seen.add(h)
            result.append((h, w))
    return result


def _quality_keyboard(formats: list[tuple[int, int]]) -> InlineKeyboardMarkup:
    rows = []
    for i, (h, w) in enumerate(formats):
        label = f"{'✅ ' if i == 0 else ''}{h}p ({w}×{h})"
        rows.append([InlineKeyboardButton(label, callback_data=f"dl:{h}")])
    rows.append([InlineKeyboardButton("🎵 Solo audio", callback_data="dl:audio")])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Envíame un enlace de video y elige la calidad.\n"
        "Soporta YouTube, TikTok, Instagram, Twitter y más.\n"
        "Límite: 49 MB."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — bienvenida\n"
        "/help  — esta ayuda\n"
        "URL    — descarga el video"
    )


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url or "vm.tiktok.com" in url


async def _download_and_send(url: str, quality: str, msg, reply_to):
    is_audio = quality == "audio"
    fmt = "bestaudio[ext=m4a]/bestaudio" if is_audio else _fmt_for_height(int(quality))

    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            "yt-dlp",
            "-f", fmt,
            *(["--merge-output-format", "mp4"] if not is_audio else []),
            "--no-playlist",
            "--extractor-args", "youtube:player_client=android,web",
            "--write-info-json",
            "--newline",
            "-o", f"{tmpdir}/video.%(ext)s",
            url,
        ]
        logger.info("yt-dlp [%s] %s", quality, url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            await msg.edit_text(f"Error al iniciar descarga: {e}")
            return

        output_lines: list[str] = []
        last_edit = asyncio.get_event_loop().time()
        last_status = ""

        async def read_output():
            nonlocal last_edit, last_status
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                output_lines.append(line)
                m = _PROGRESS_RE.search(line)
                if not m:
                    continue
                pct, size, unit, eta = m.group(1), m.group(2), m.group(3), m.group(4)
                unit_label = {"MiB": "MB", "KiB": "KB", "GiB": "GB"}.get(unit, unit)
                new_status = f"⏳ [{_bar(float(pct))}] {float(pct):.0f}%\n{size} {unit_label} · ETA {eta}"
                now = asyncio.get_event_loop().time()
                if new_status != last_status and now - last_edit >= 4:
                    try:
                        await msg.edit_text(new_status)
                        last_status = new_status
                        last_edit = now
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(read_output(), timeout=300)
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await msg.edit_text("Tiempo agotado (>5 min).")
            return

        if proc.returncode != 0:
            full_err = "\n".join(output_lines)
            logger.error("yt-dlp error: %s", full_err[-1000:])
            friendly = _friendly_error(full_err)
            await msg.edit_text(friendly or f"Error al descargar:\n{full_err[-400:]}")
            return

        media_files = [f for f in Path(tmpdir).iterdir() if f.suffix in (".mp4", ".m4a", ".mp3", ".webm")]
        if not media_files:
            await msg.edit_text("No se encontró archivo tras la descarga.")
            return

        filepath = media_files[0]
        size = filepath.stat().st_size
        logger.info("Descargado: %s (%.1f MB)", filepath.name, size / 1024 / 1024)

        limit_mb = MAX_BYTES // 1024 // 1024
        if size > MAX_BYTES:
            await msg.edit_text(
                f"Archivo demasiado grande ({size // 1024 // 1024} MB, límite {limit_mb} MB). "
                "Elige una calidad menor."
            )
            return

        caption = _build_caption(tmpdir)
        logger.info("caption=%r", caption)

        mb_total = size / 1024 / 1024
        await msg.edit_text(f"⬆️ [{_bar(0)}] 0%\n0.0 / {mb_total:.1f} MB")

        pf = _ProgressFile(filepath)

        async def _monitor_upload():
            while True:
                await asyncio.sleep(3)
                pct = pf.sent / pf.total * 100 if pf.total else 0
                mb_sent = pf.sent / 1024 / 1024
                try:
                    await msg.edit_text(
                        f"⬆️ [{_bar(pct)}] {pct:.0f}%\n{mb_sent:.1f} / {mb_total:.1f} MB"
                    )
                except Exception:
                    pass
                if pf.sent >= pf.total:
                    break

        monitor_task = asyncio.create_task(_monitor_upload())
        try:
            if is_audio:
                await reply_to.reply_audio(audio=pf, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await reply_to.reply_video(video=pf, caption=caption, parse_mode=ParseMode.HTML, supports_streaming=True)
        finally:
            monitor_task.cancel()
            pf.close()
        await msg.delete()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith(("http://", "https://")):
        await update.message.reply_text("Envíame una URL de video.")
        return

    if _is_tiktok(text):
        msg = await update.message.reply_text("⏳ Descargando...")
        await _download_and_send(url=text, quality="1080", msg=msg, reply_to=update.message)
        return

    msg = await update.message.reply_text("Obteniendo formatos...")
    formats = await _fetch_formats(text) or _FALLBACK_FORMATS
    await msg.edit_text("Elige la calidad:", reply_markup=_quality_keyboard(formats))
    PENDING[(update.effective_chat.id, msg.message_id)] = text


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, quality = query.data.split(":", 1)
    key = (query.message.chat_id, query.message.message_id)
    url = PENDING.pop(key, None)

    if not url:
        await query.edit_message_text("Sesión expirada. Envía el URL de nuevo.")
        return

    await query.edit_message_text("⏳ Descargando...", reply_markup=None)
    await _download_and_send(url=url, quality=quality, msg=query.message, reply_to=query.message)


def main():
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if TG_LOCAL_URL:
        builder = (
            builder
            .base_url(f"{TG_LOCAL_URL}/bot")
            .base_file_url(f"{TG_LOCAL_URL}/file/bot")
            .connect_timeout(30)
            .read_timeout(3600)    # hasta 1 hora para subidas grandes
            .write_timeout(3600)
            .media_write_timeout(3600)
        )
        logger.info("Usando servidor local: %s", TG_LOCAL_URL)
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^dl:"))
    logger.info("Bot iniciado. Límite de archivo: %d MB", MAX_BYTES // 1024 // 1024)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
