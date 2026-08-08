"""Soporte opcional de yt-dlp para sitios de vídeo/audio (YouTube, Vimeo, …).

No es dependencia obligatoria: solo se usa si YTDLP=true y el paquete está
instalado (`pip install yt-dlp`). Para unir vídeo+audio conviene tener ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from debrid.base import UnrestrictedLink

log = logging.getLogger("bot.ytdlp")

PROVIDER_NAME = "yt-dlp"

# Alturas típicas que se ofrecen en el menú (estilo utube-bot)
_QUALITY_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)


class YtDlpError(Exception):
    """Fallo al extraer o descargar con yt-dlp."""


@dataclass
class QualityOption:
    key: str
    label: str
    format_selector: str
    size: int | None = None
    height: int | None = None
    kind: str = "video"  # video | audio
    ext: str = "mp4"


@dataclass
class MediaProbe:
    url: str
    title: str
    host: str
    duration: int | None = None
    thumbnail: str | None = None
    options: list[QualityOption] = field(default_factory=list)


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def _safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or f"media_{uuid.uuid4().hex[:8]}"


def _base_opts(format_selector: str | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "ignoreconfig": True,
        "logger": logging.getLogger("yt_dlp"),
    }
    if format_selector:
        opts["format"] = format_selector
    return opts


def _pick_info(info: dict) -> dict:
    """Si es playlist, toma la primera entrada válida."""
    if not info:
        raise YtDlpError("yt-dlp no devolvió información")
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise YtDlpError("La playlist no tiene entradas")
        info = entries[0]
    return info


def _filename_from_info(info: dict, ext_override: str | None = None) -> str:
    title = (info.get("title") or info.get("id") or "media").strip()
    ext = ext_override or (info.get("ext") or "mp4")
    ext = str(ext).strip().lstrip(".")
    requested = info.get("requested_downloads") or []
    if not ext_override and requested and requested[0].get("ext"):
        ext = requested[0]["ext"]
    return _safe_filename(f"{title}.{ext}")


def _size_from_info(info: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = info.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    total = 0
    found = False
    for fmt in info.get("requested_formats") or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if isinstance(size, (int, float)) and size > 0:
            total += int(size)
            found = True
    return total if found else None


def _host_from_info(info: dict, url: str) -> str:
    extractor = (info.get("extractor_key") or info.get("extractor") or "").strip()
    if extractor:
        return extractor
    return (urlparse(url).hostname or "yt-dlp").lower()


def _fmt_size(size: int | None) -> str:
    if not size or size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _format_duration(seconds: int | None) -> str | None:
    if not seconds or seconds < 0:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _is_storyboard(fmt: dict) -> bool:
    note = (fmt.get("format_note") or "").lower()
    fid = str(fmt.get("format_id") or "")
    return "storyboard" in note or fid.startswith("sb")


def _vcodec_ok(fmt: dict) -> bool:
    vcodec = fmt.get("vcodec")
    return bool(vcodec) and vcodec != "none"


def _acodec_ok(fmt: dict) -> bool:
    acodec = fmt.get("acodec")
    return bool(acodec) and acodec != "none"


def _fmt_size_bytes(fmt: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _rank_video(fmt: dict) -> tuple:
    """Mayor = mejor candidato dentro de la misma altura."""
    ext = (fmt.get("ext") or "").lower()
    vcodec = (fmt.get("vcodec") or "").lower()
    proto = (fmt.get("protocol") or "").lower()
    tbr = fmt.get("tbr") or 0
    # preferir mp4/h264 y no-hls para tamaños más predecibles
    ext_score = 3 if ext == "mp4" else 2 if ext in ("webm", "mkv") else 1
    codec_score = 3 if "avc" in vcodec or vcodec.startswith("h264") else 2 if "vp9" in vcodec else 1
    proto_score = 0 if "m3u8" in proto or "hls" in proto else 1
    has_audio = 1 if _acodec_ok(fmt) else 0
    return (ext_score, codec_score, proto_score, has_audio, tbr)


def _best_audio(formats: list[dict]) -> dict | None:
    audio_only = [
        f
        for f in formats
        if _acodec_ok(f) and not _vcodec_ok(f) and not _is_storyboard(f)
    ]
    if not audio_only:
        return None

    def rank(f: dict) -> tuple:
        ext = (f.get("ext") or "").lower()
        abr = f.get("abr") or f.get("tbr") or 0
        ext_score = 3 if ext in ("m4a", "mp4") else 2 if ext == "webm" else 1
        return (ext_score, abr)

    return max(audio_only, key=rank)


def _build_quality_options(info: dict) -> list[QualityOption]:
    formats = [f for f in (info.get("formats") or []) if f and not _is_storyboard(f)]
    if not formats:
        # info ya resuelta a un formato único
        return [
            QualityOption(
                key="best",
                label="🎬 Mejor calidad",
                format_selector="bv*+ba/b",
                size=_size_from_info(info),
                height=info.get("height"),
                kind="video",
                ext=(info.get("ext") or "mp4"),
            )
        ]

    audio = _best_audio(formats)
    audio_size = _fmt_size_bytes(audio) if audio else None

    # Mejor formato de vídeo por altura exacta
    by_height: dict[int, dict] = {}
    for fmt in formats:
        if not _vcodec_ok(fmt):
            continue
        height = fmt.get("height")
        if not isinstance(height, int) or height <= 0:
            continue
        prev = by_height.get(height)
        if prev is None or _rank_video(fmt) > _rank_video(prev):
            by_height[height] = fmt

    options: list[QualityOption] = []

    # Mejor calidad global
    best_size = None
    if by_height:
        top = by_height[max(by_height)]
        best_size = _fmt_size_bytes(top)
        if best_size and audio_size and not _acodec_ok(top):
            best_size += audio_size
    size_txt = _fmt_size(best_size)
    options.append(
        QualityOption(
            key="best",
            label="🎬 Mejor" + (f" · ~{size_txt}" if size_txt else ""),
            format_selector="bv*+ba/b",
            size=best_size,
            height=max(by_height) if by_height else None,
            kind="video",
            ext="mp4",
        )
    )

    # Solo audio
    if audio:
        a_ext = (audio.get("ext") or "m4a").lower()
        if a_ext == "mp4":
            a_ext = "m4a"
        abr = audio.get("abr") or audio.get("tbr")
        a_size = audio_size
        parts = ["🎵 Audio"]
        if abr:
            parts.append(f"{int(abr)}kbps")
        a_txt = _fmt_size(a_size)
        if a_txt:
            parts.append(f"~{a_txt}")
        options.append(
            QualityOption(
                key="audio",
                label=" · ".join(parts),
                format_selector="ba/b",
                size=a_size,
                height=None,
                kind="audio",
                ext=a_ext,
            )
        )

    # Calidades por altura (elige la altura disponible <= pedida más cercana,
    # pero solo listamos alturas que existen para no duplicar)
    for height in sorted(by_height.keys(), reverse=True):
        if height not in _QUALITY_HEIGHTS and height < 240:
            continue
        fmt = by_height[height]
        v_size = _fmt_size_bytes(fmt)
        total = v_size
        if total and audio_size and not _acodec_ok(fmt):
            total += audio_size
        ext = (fmt.get("ext") or "mp4").lower()
        # vídeo-only + audio → mp4 tras merge
        if not _acodec_ok(fmt):
            ext = "mp4"
        size_txt = _fmt_size(total)
        note = (fmt.get("format_note") or "").strip()
        # etiqueta compacta: "1080p · ~45.2 MB"
        label = f"{height}p"
        if note and note.lower() not in label.lower() and len(note) <= 12:
            # p.ej. "Premium", "HDR" — omitir basura larga
            if note.lower() not in ("medium", "small", "large", "tiny"):
                pass  # no meter notes raras
        if size_txt:
            label += f" · ~{size_txt}"
        # selector: mejor vídeo a esa altura + mejor audio; fallback progressive
        selector = (
            f"bv*[height={height}]+ba/"
            f"b[height={height}]/"
            f"bv*[height<={height}]+ba/"
            f"b[height<={height}]"
        )
        options.append(
            QualityOption(
                key=str(height),
                label=label,
                format_selector=selector,
                size=total,
                height=height,
                kind="video",
                ext=ext if ext in ("mp4", "webm", "mkv") else "mp4",
            )
        )

    # Limitar botones: best + audio + hasta 8 calidades
    video_opts = [o for o in options if o.kind == "video" and o.key != "best"]
    audio_opts = [o for o in options if o.kind == "audio"]
    best_opts = [o for o in options if o.key == "best"]
    return best_opts + audio_opts + video_opts[:8]


def _probe_sync(url: str) -> MediaProbe:
    import yt_dlp

    opts = _base_opts()
    opts["skip_download"] = True
    # sin "format" para listar todos los formatos
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).splitlines()[-1] if str(exc) else "error de descarga") from exc
    except Exception as exc:
        raise YtDlpError(str(exc) or type(exc).__name__) from exc

    info = _pick_info(info)
    title = (info.get("title") or info.get("id") or "media").strip()
    duration = info.get("duration")
    if isinstance(duration, float):
        duration = int(duration)
    elif not isinstance(duration, int):
        duration = None

    return MediaProbe(
        url=url,
        title=title,
        host=_host_from_info(info, url),
        duration=duration,
        thumbnail=info.get("thumbnail"),
        options=_build_quality_options(info),
    )


def _extract_sync(url: str, format_selector: str) -> dict:
    import yt_dlp

    opts = _base_opts(format_selector)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).splitlines()[-1] if str(exc) else "error de descarga") from exc
    except Exception as exc:
        raise YtDlpError(str(exc) or type(exc).__name__) from exc
    return _pick_info(info)


def _stream_url_sync(url: str, format_selector: str) -> str:
    """Mejor URL directa de un solo archivo (progresivo); si no hay, la del formato principal."""
    import yt_dlp

    # Intentar el selector pedido; si sale stream dual/HLS, buscar progressive
    try:
        info = _extract_sync(url, format_selector)
        direct = info.get("url")
        # requested_formats = fusión de varios streams → no hay URL única útil
        if direct and not info.get("requested_formats"):
            return direct
    except YtDlpError:
        pass

    progressive = (
        "best[protocol^=http][protocol!=m3u8_native][protocol!=m3u8]/"
        "best[ext=mp4]/best"
    )
    opts = _base_opts(progressive)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _pick_info(ydl.extract_info(url, download=False))
    except Exception as exc:
        raise YtDlpError(
            "No hay una URL directa usable (stream segmentado). Usa la opción 📤 Archivo."
        ) from exc

    direct = info.get("url")
    if direct and not info.get("requested_formats"):
        return direct
    for fmt in reversed(info.get("formats") or []):
        candidate = fmt.get("url")
        if candidate and _vcodec_ok(fmt):
            return candidate
    for fmt in reversed(info.get("formats") or []):
        if fmt.get("url"):
            return fmt["url"]
    raise YtDlpError(
        "No hay una URL directa usable (stream segmentado). Usa la opción 📤 Archivo."
    )


def _download_sync(
    url: str,
    outtmpl: str,
    format_selector: str,
    progress_hook: Callable[[dict], None] | None = None,
    max_filesize: int | None = None,
) -> str:
    import yt_dlp

    opts = _base_opts(format_selector)
    opts.update(
        {
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "restrictfilenames": False,
            "windowsfilenames": True,
        }
    )
    if max_filesize:
        opts["max_filesize"] = max_filesize
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            info = _pick_info(info)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                base, _ = os.path.splitext(path)
                for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus"):
                    candidate = base + ext
                    if os.path.exists(candidate):
                        path = candidate
                        break
            if not os.path.exists(path):
                for item in info.get("requested_downloads") or []:
                    fp = item.get("filepath")
                    if fp and os.path.exists(fp):
                        path = fp
                        break
            if not os.path.exists(path):
                raise YtDlpError("La descarga terminó pero no encuentro el archivo")
            return path
    except YtDlpError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise YtDlpError(str(exc).splitlines()[-1] if str(exc) else "error de descarga") from exc
    except Exception as exc:
        raise YtDlpError(str(exc) or type(exc).__name__) from exc


async def probe(url: str) -> MediaProbe:
    """Lista metadatos y calidades disponibles sin descargar."""
    return await asyncio.to_thread(_probe_sync, url)


async def extract(url: str, format_selector: str = "bv*+ba/b") -> UnrestrictedLink:
    """Extrae metadatos con un formato concreto (p.ej. lotes sin menú de calidad)."""
    info = await asyncio.to_thread(_extract_sync, url, format_selector)
    return UnrestrictedLink(
        url=url,
        filename=_filename_from_info(info),
        host=_host_from_info(info, url),
        size=_size_from_info(info),
        via="ytdlp",
        format_selector=format_selector,
    )


def link_from_probe(media: MediaProbe, option: QualityOption) -> UnrestrictedLink:
    """Construye UnrestrictedLink a partir de una calidad elegida."""
    ext = option.ext or ("m4a" if option.kind == "audio" else "mp4")
    return UnrestrictedLink(
        url=media.url,
        filename=_safe_filename(f"{media.title}.{ext}"),
        host=media.host,
        size=option.size,
        via="ytdlp",
        format_selector=option.format_selector,
    )


def describe_media(media: MediaProbe) -> str:
    lines = [f"🎬 **{media.title}**", f"🌐 **{media.host}**"]
    dur = _format_duration(media.duration)
    if dur:
        lines.append(f"⏱ **Duración:** {dur}")
    lines.append("")
    lines.append("Elige la **calidad**:")
    return "\n".join(lines)


async def stream_url(url: str, format_selector: str = "bv*+ba/b") -> str:
    return await asyncio.to_thread(_stream_url_sync, url, format_selector)


async def download(
    url: str,
    download_dir: str,
    format_selector: str = "bv*+ba/b",
    on_progress: Callable[[int, int], None] | None = None,
    max_filesize: int | None = None,
) -> str:
    """Descarga con yt-dlp. `on_progress(downloaded, total)` se llama desde el hilo de yt-dlp."""
    os.makedirs(download_dir, exist_ok=True)
    prefix = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(download_dir, f"{prefix}_%(title).180B.%(ext)s")

    def hook(d: dict) -> None:
        if not on_progress or d.get("status") != "downloading":
            return
        downloaded = int(d.get("downloaded_bytes") or 0)
        total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        try:
            on_progress(downloaded, total)
        except Exception:
            log.debug("progress hook falló", exc_info=True)

    return await asyncio.to_thread(
        _download_sync,
        url,
        outtmpl,
        format_selector,
        hook if on_progress else None,
        max_filesize,
    )
