import asyncio
import logging
import mimetypes
import os
import re
import time
import uuid
from urllib.parse import unquote, urlparse

import aiohttp
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import load_config
from controlc import get_paste_links
from debrid import (
    AdStreamProbe,
    AllDebrid,
    DebridError,
    DebridProvider,
    NeedsStreamChoice,
    UnrestrictedLink,
    build_providers,
)
from linkproxy import LinkProxy, detect_public_ip
from filecrypt import (
    CaptchaRequired,
    FilecryptError,
    PasswordRequired,
    get_folder_links,
    is_filecrypt,
)
import dripfiles as dripfiles_mod
import ytdlp as ytdlp_mod

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
if LOG_LEVEL != "DEBUG":
    # pyrogram narra cada reconexión y cada arranque de sesión; solo interesan sus avisos
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

cfg = load_config()

app = Client(
    "debrid_bot",
    api_id=cfg.api_id,
    api_hash=cfg.api_hash,
    bot_token=cfg.bot_token,
)

http: aiohttp.ClientSession
debrid_http: aiohttp.ClientSession  # con DEBRID_PROXY sale por el proxy; si no, es `http`
link_proxy: LinkProxy | None = None
providers: dict[str, DebridProvider] = {}
user_service: dict[int, str] = {}
pending: dict[str, tuple[UnrestrictedLink, str]] = {}
# token -> MediaProbe (menú de calidades yt-dlp, estilo utube-bot)
pending_ytdlp: dict[str, ytdlp_mod.MediaProbe] = {}
# token -> AdStreamProbe (menú de calidades AllDebrid streaming)
pending_ad_streams: dict[str, AdStreamProbe] = {}

# Sitios donde yt-dlp va primero (si YTDLP=true), antes que debrid
_YTDLP_FIRST_HOST_SUFFIXES = (
    "instagram.com",
    "instagr.am",
    "cdninstagram.com",
)

# asyncio solo guarda referencias débiles a las tareas: sin esto el GC puede
# matar un monitor_torrent/transfer en marcha y el mensaje se queda congelado
background_tasks: set[asyncio.Task] = set()

MAX_TG_SIZE = 2 * 1024**3  # límite de subida para bots (2 GB)
MAX_TORRENT_FILES = 25
STATUS_EMOJI = {"queued": "🕓", "downloading": "⬇️", "ready": "✅", "error": "❌"}
DIRECT_PROVIDER = "Directo"
DRIPFILES_PROVIDER = "DripFiles"
YTDLP_PROVIDER = ytdlp_mod.PROVIDER_NAME
# servicios que se descargan con la sesión normal, no por el proxy del debrid
LOCAL_PROVIDERS = (DIRECT_PROVIDER, DRIPFILES_PROVIDER)
DIRECT_USER_AGENT = (
    "Mozilla/5.0 (compatible; DebridBot/1.0; +https://github.com/Oihalitz/DebridBot)"
)
# /wget se identifica como wget de verdad (sin "Mozilla"), no como navegador
WGET_USER_AGENT = "Wget/1.21.4 (DebridBot/1.0; +https://github.com/Oihalitz/DebridBot)"
# Content-Types que casi siempre son páginas, no descargas de archivo
_PAGE_CONTENT_TYPES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "application/xhtml",
    "text/xhtml",
})
# Extensiones de "página web" si no hay Content-Disposition de archivo
_PAGE_EXTENSIONS = frozenset({
    ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".cgi", ".shtml",
})
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
# Content-Types que mimetypes no conoce y que sí aparecen en hosters
_CTYPE_EXTENSIONS = {
    "application/x-zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-rar": ".rar",
    "application/x-rar-compressed": ".rar",
    "application/x-matroska": ".mkv",
    "application/macbinary": "",  # genérico: no dice nada del contenido real
    "binary/octet-stream": ".bin",
}
_CD_FILENAME_RE = re.compile(
    r"""filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\s]+)|filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)""",
    re.I,
)
# github.com/owner/repo/blob/ref/path → raw.githubusercontent.com/owner/repo/ref/path
_GITHUB_BLOB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
    re.I,
)

# Mirrors de hosters que el debrid no reconoce pero que apuntan al mismo sitio
MIRRORS = {
    "turbobit.net": [
        "turbobyt.net", "turbobif.com", "turbobit.com", "turb.to", "turb.pw",
        "turb.cc", "turbo.to", "turbo.pw", "turbo.cc", "trbbt.net",
    ],
}


class FileTooLarge(Exception):
    def __init__(self, size: int):
        self.size = size


class DirectDownloadError(Exception):
    """La URL no es un archivo descargable (HTML, error de red, etc.)."""


def _auth(_, __, update) -> bool:
    user = update.from_user
    return bool(user) and (not cfg.allowed_users or user.id in cfg.allowed_users)


auth = filters.create(_auth)


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} TB"


def progress_bar(pct: float) -> str:
    filled = min(20, int(pct / 5))
    return "█" * filled + "░" * (20 - filled)


def is_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
        return all([parsed.scheme in ("http", "https"), parsed.netloc])
    except ValueError:
        return False


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def normalize_mirrors(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    for canonical, mirrors in MIRRORS.items():
        for mirror in mirrors:
            if hostname == mirror or hostname.endswith("." + mirror):
                new_host = hostname[: len(hostname) - len(mirror)] + canonical
                netloc = new_host + (f":{parsed.port}" if parsed.port else "")
                return parsed._replace(netloc=netloc).geturl()
    return url


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name or f"archivo_{uuid.uuid4().hex[:8]}"


def active_slug(user_id: int) -> str:
    slug = user_service.get(user_id)
    return slug if slug in providers else next(iter(providers))


def provider_for(user_id: int) -> DebridProvider:
    return providers[active_slug(user_id)]


def remember(link: UnrestrictedLink, provider: DebridProvider | str) -> str:
    if len(pending) > 500:
        for key in list(pending)[:100]:
            pending.pop(key, None)
    token = uuid.uuid4().hex[:12]
    name = provider if isinstance(provider, str) else provider.name
    pending[token] = (link, name)
    return token


def normalize_github_url(url: str) -> str:
    """Convierte enlaces /blob/ de GitHub a raw.githubusercontent.com."""
    match = _GITHUB_BLOB_RE.match(url.strip())
    if not match:
        return url
    owner, repo, ref, path = match.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    match = _CD_FILENAME_RE.search(header)
    if not match:
        return None
    raw = next(g for g in match.groups() if g)
    name = unquote(raw.strip().strip('"'))
    # algunos servidores (DripFiles entre ellos) codifican el nombre dos veces:
    # filename*=UTF-8''debuginfo%2520%25281%2529.zip → debuginfo (1).zip
    if _PERCENT_ESCAPE_RE.search(name):
        name = unquote(name)
    return safe_filename(name)


def extension_for(content_type: str | None) -> str:
    """Extensión típica de un Content-Type, con los tipos raros más habituales."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype:
        return ""
    return _CTYPE_EXTENSIONS.get(ctype) or mimetypes.guess_extension(ctype) or ""


def filename_from_url(url: str) -> str | None:
    path = unquote(urlparse(url).path or "")
    name = os.path.basename(path.rstrip("/"))
    if not name or "." not in name:
        return None
    return safe_filename(name)


def _looks_like_file_name(name: str | None) -> bool:
    if not name or "." not in name:
        return False
    ext = os.path.splitext(name)[1].lower()
    return bool(ext) and ext not in _PAGE_EXTENSIONS


def is_downloadable_file(
    content_type: str | None,
    content_disposition: str | None,
    final_url: str,
) -> bool:
    """True solo si la respuesta parece un archivo, no una página HTML."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    cd = content_disposition or ""
    cd_name = filename_from_content_disposition(cd)
    has_attachment = "attachment" in cd.lower()
    url_name = filename_from_url(final_url)

    # HTML / XHTML: nunca lo tratamos como descarga de archivo
    if ctype in _PAGE_CONTENT_TYPES:
        return False

    if has_attachment or cd_name:
        # attachment/.filename en CD = archivo, salvo extensión de página web
        if cd_name:
            ext = os.path.splitext(cd_name)[1].lower()
            if ext in _PAGE_EXTENSIONS:
                return False
        return True

    if not ctype or ctype == "application/octet-stream":
        # sin tipo claro: exigir nombre con extensión que no sea de página
        return _looks_like_file_name(url_name)

    if ctype.startswith(("application/", "video/", "audio/", "image/", "font/", "model/")):
        return True

    # text/* (csv, plain, markdown…): solo con nombre de archivo reconocible
    if ctype.startswith("text/"):
        return _looks_like_file_name(cd_name or url_name)

    return False


async def _probe_headers(
    session: aiohttp.ClientSession,
    url: str,
    *,
    user_agent: str = DIRECT_USER_AGENT,
    use_head: bool = True,
) -> tuple[str, dict[str, str], int]:
    """Devuelve (url_final, headers_lower, status). Prefiere HEAD; si falla, GET corto.

    `use_head=False` va directo al GET: hay servidores que responden al HEAD con
    la página web y al GET con el archivo (DripFiles lo hace), así que para el
    modo wget solo vale lo que diga el GET.
    """
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    timeout = aiohttp.ClientTimeout(total=30, connect=15, sock_read=20)

    try:
        if use_head:
            async with session.head(
                url, headers=headers, allow_redirects=True, timeout=timeout
            ) as resp:
                if resp.status < 400:
                    return (
                        str(resp.url),
                        {k.lower(): v for k, v in resp.headers.items()},
                        resp.status,
                    )
                # 405/501 = HEAD no soportado; otros 4xx/5xx se reintentan con GET
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass

    # GET con Range: solo cabeceras (y 1 byte si lo sirven); el context manager cierra el body
    get_headers = {**headers, "Range": "bytes=0-0"}
    async with session.get(
        url, headers=get_headers, allow_redirects=True, timeout=timeout
    ) as resp:
        return (
            str(resp.url),
            {k.lower(): v for k, v in resp.headers.items()},
            resp.status,
        )


async def probe_direct_file(session: aiohttp.ClientSession, url: str) -> UnrestrictedLink:
    """Comprueba que la URL entrega un archivo real y construye UnrestrictedLink."""
    url = normalize_github_url(url.strip())
    try:
        final_url, hdrs, status = await _probe_headers(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise DirectDownloadError(f"No se pudo conectar: {exc}") from exc

    if status >= 400:
        raise DirectDownloadError(f"El servidor respondió HTTP {status}")

    content_type = hdrs.get("content-type")
    content_disposition = hdrs.get("content-disposition")
    if not is_downloadable_file(content_type, content_disposition, final_url):
        ctype = (content_type or "desconocido").split(";")[0].strip()
        raise DirectDownloadError(
            f"No parece un archivo descargable (Content-Type: {ctype}). "
            "Solo acepto enlaces directos a archivos, no páginas web."
        )

    filename = (
        filename_from_content_disposition(content_disposition)
        or filename_from_url(final_url)
        or f"archivo_{uuid.uuid4().hex[:8]}"
    )
    size = None
    # Content-Length normal, o Content-Range: bytes 0-0/12345
    if "content-range" in hdrs:
        cr = hdrs["content-range"]
        if "/" in cr:
            total = cr.rsplit("/", 1)[-1]
            if total.isdigit():
                size = int(total)
    if size is None and "content-length" in hdrs:
        try:
            cl = int(hdrs["content-length"])
            # con Range: bytes=0-0 el Content-Length suele ser 1
            if "content-range" not in hdrs:
                size = cl
        except ValueError:
            pass

    host = (urlparse(final_url).hostname or "direct").lower()
    return UnrestrictedLink(url=final_url, filename=filename, host=host, size=size)


def session_for(provider_name: str) -> aiohttp.ClientSession:
    """Enlaces propios (directo/DripFiles) van por la sesión normal; el resto por el debrid."""
    return http if provider_name in LOCAL_PROVIDERS else debrid_http


async def dripfiles_links(url: str) -> tuple[list[UnrestrictedLink], list[str], int]:
    """Convierte un envío de DripFiles en enlaces descargables (uno por archivo).

    Devuelve (enlaces, errores, total de archivos del envío). Se procesan como
    mucho MAX_CONTAINER_LINKS archivos, igual que en pastes y carpetas.
    """
    share = await dripfiles_mod.fetch_share(http, url)
    links: list[UnrestrictedLink] = []
    errors: list[str] = []
    for item in share.files[:MAX_CONTAINER_LINKS]:
        try:
            # el handler de DripFiles ya manda Content-Disposition y tamaño reales
            links.append(await probe_direct_file(http, item.url))
        except DirectDownloadError as exc:
            log.info("DripFiles: %s no se pudo preparar (%s)", item.name, exc)
            errors.append(f"`{item.name}`: {exc}")
    return links, errors, len(share.files)


async def probe_raw_file(session: aiohttp.ClientSession, url: str) -> UnrestrictedLink:
    """Como probe_direct_file pero sin filtros: lo que responda el servidor vale.

    Es el modo `wget`: se sondea con el User-Agent de wget (los sitios que miran
    el UA responden distinto a un navegador), no se comprueba si parece un
    archivo (HTML incluido) y si no hay cabeceras se sigue con el nombre de la URL.
    """
    url = normalize_github_url(url.strip())
    blind = UnrestrictedLink(
        url=url,
        filename=filename_from_url(url) or f"archivo_{uuid.uuid4().hex[:8]}",
        host=(urlparse(url).hostname or "direct").lower(),
        size=None,
    )
    try:
        final_url, hdrs, status = await _probe_headers(
            session, url, user_agent=WGET_USER_AGENT, use_head=False
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # sin cabeceras seguimos igual: manda el GET de la descarga, que dirá la verdad
        log.info("wget: no pude sondear %s (%s); descargo a ciegas", url, exc)
        return blind
    if status >= 400:
        # puede ser el HEAD o el Range del sondeo, no la URL; que decida la descarga
        log.info("wget: el sondeo de %s devolvió HTTP %s; descargo a ciegas", url, status)
        return blind

    content_type = (hdrs.get("content-type") or "").split(";")[0].strip().lower()
    filename = (
        filename_from_content_disposition(hdrs.get("content-disposition"))
        or filename_from_url(final_url)
        or f"archivo_{uuid.uuid4().hex[:8]}"
    )
    if "." not in filename:
        # sin extensión Telegram no sabe qué es; la deducimos del Content-Type
        filename += extension_for(content_type)
    size = None
    if "content-range" in hdrs:
        total = hdrs["content-range"].rsplit("/", 1)[-1]
        if total.isdigit():
            size = int(total)
    elif "content-length" in hdrs and hdrs["content-length"].isdigit():
        size = int(hdrs["content-length"])
    host = (urlparse(final_url).hostname or "direct").lower()
    return UnrestrictedLink(url=final_url, filename=filename, host=host, size=size)


def prefers_ytdlp_first(url: str) -> bool:
    """Instagram y similares: yt-dlp antes que debrid (si YTDLP=true)."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    for suffix in _YTDLP_FIRST_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


async def resolve_link(
    user_id: int,
    url: str,
    *,
    allow_ytdlp: bool = True,
    auto_pick_stream: bool | None = None,
) -> tuple[UnrestrictedLink, str]:
    """Orden por defecto: debrid → archivo directo → yt-dlp.

    En Instagram (y hosts de `_YTDLP_FIRST_HOST_SUFFIXES`), si YTDLP=true y
    allow_ytdlp, yt-dlp va **antes** del debrid.

    En mensajes sueltos se usa allow_ytdlp=False y luego el menú de calidades.
    En lotes (paste/filecrypt) se deja allow_ytdlp=True con la calidad por defecto.
    auto_pick_stream: AllDebrid streaming elige la mejor calidad sin menú
    (por defecto igual que allow_ytdlp).
    """
    url = normalize_mirrors(url)
    errors: list[str] = []
    if auto_pick_stream is None:
        auto_pick_stream = allow_ytdlp

    # envío de DripFiles: los enlaces por archivo salen de su página pública
    if dripfiles_mod.is_share_url(url):
        try:
            links, drip_errors, total = await dripfiles_links(url)
        except dripfiles_mod.DripFilesError as drip_exc:
            raise DebridError(f"💧 DripFiles: {drip_exc}") from drip_exc
        if total > 1:
            raise DebridError(
                f"💧 DripFiles: el envío tiene {total} archivos; "
                "mándame el enlace suelto para elegir cuál quieres."
            )
        if not links:
            raise DebridError(
                "💧 DripFiles: " + ("\n".join(drip_errors) or "envío sin archivos")
            )
        return links[0], DRIPFILES_PROVIDER

    # Instagram etc.: yt-dlp primero
    if allow_ytdlp and cfg.ytdlp and prefers_ytdlp_first(url):
        try:
            link = await ytdlp_mod.extract(url, cfg.ytdlp_format)
            return link, YTDLP_PROVIDER
        except ytdlp_mod.YtDlpError as ytdlp_exc:
            log.info("yt-dlp (preferido) no pudo extraer %s: %s", url, ytdlp_exc)
            errors.append(f"🎬 yt-dlp: {ytdlp_exc}")

    if providers:
        try:
            link, used = await unrestrict_url(
                user_id, url, auto_pick_stream=auto_pick_stream
            )
            return link, used.name
        except NeedsStreamChoice:
            raise
        except DebridError as debrid_exc:
            log.info("Debrid no pudo desbloquear %s; probando fallbacks", url)
            errors.append(str(debrid_exc))

    try:
        link = await probe_direct_file(http, url)
        return link, DIRECT_PROVIDER
    except DirectDownloadError as direct_exc:
        log.info("Descarga directa no válida para %s: %s", url, direct_exc)
        errors.append(f"⬇️ Directo: {direct_exc}")

    if allow_ytdlp and cfg.ytdlp and not prefers_ytdlp_first(url):
        try:
            link = await ytdlp_mod.extract(url, cfg.ytdlp_format)
            return link, YTDLP_PROVIDER
        except ytdlp_mod.YtDlpError as ytdlp_exc:
            log.info("yt-dlp no pudo extraer %s: %s", url, ytdlp_exc)
            errors.append(f"🎬 yt-dlp: {ytdlp_exc}")
    elif allow_ytdlp and cfg.ytdlp and prefers_ytdlp_first(url):
        # ya se intentó al principio; no repetir
        pass

    raise DebridError("\n\n".join(errors) if errors else "No se pudo resolver el enlace")


def providers_for_url(user_id: int, url: str) -> list[DebridProvider]:
    """Orden de servicios a probar: regla de host > servicio activo > resto (failover)."""
    hostname = (urlparse(url).hostname or "").lower()
    ordered: list[DebridProvider] = []
    for rule_host, slug in cfg.host_rules:
        if rule_host in hostname and slug in providers:
            ordered.append(providers[slug])
            break
    active = provider_for(user_id)
    if active not in ordered:
        ordered.append(active)
    if not cfg.failover:
        return ordered[:1]
    return ordered + [p for p in providers.values() if p not in ordered]


async def unrestrict_url(
    user_id: int, url: str, *, auto_pick_stream: bool = False
) -> tuple[UnrestrictedLink, DebridProvider]:
    """Prueba los servicios en orden y devuelve el primero que desbloquee el enlace."""
    errors: list[str] = []
    for provider in providers_for_url(user_id, url):
        try:
            if isinstance(provider, AllDebrid):
                return (
                    await provider.unrestrict(url, auto_pick_stream=auto_pick_stream),
                    provider,
                )
            return await provider.unrestrict(url), provider
        except NeedsStreamChoice:
            # el caller debe mostrar menú de calidades; no failover
            raise
        except DebridError as exc:
            log.info("Failover: %s no desbloqueó %s (%s)", provider.name, url, exc)
            errors.append(str(exc))
        except Exception:
            log.exception("Error inesperado desbloqueando %s con %s", url, provider.name)
            errors.append(f"{provider.name}: error inesperado")
    raise DebridError("\n".join(errors))


def public_url(link: UnrestrictedLink) -> str:
    # con LINK_PROXY el usuario recibe una URL del bot, no la del debrid,
    # para que el debrid solo vea descargas desde la IP del servidor
    if link_proxy:
        return link_proxy.register(link)
    return link.url


def describe(link: UnrestrictedLink, provider_name: str) -> str:
    lines = [f"📄 **Archivo:** {link.filename}", f"🌐 **Host:** {link.host}"]
    if link.size:
        lines.append(f"📦 **Tamaño:** {human_size(link.size)}")
    if link.format_selector and provider_name == YTDLP_PROVIDER:
        # muestra algo legible si es una altura concreta
        sel = link.format_selector
        if "height=" in sel:
            m = re.search(r"height=(\d+)", sel)
            if m:
                lines.append(f"📺 **Calidad:** {m.group(1)}p")
        elif sel in ("ba/b", "bestaudio/best"):
            lines.append("📺 **Calidad:** solo audio")
        elif sel in ("bv*+ba/b", "best"):
            lines.append("📺 **Calidad:** mejor")
    lines.append(f"⚙️ **Servicio:** {provider_name}")
    return "\n".join(lines)


def action_keyboard(token: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton("🔗 Enlace", callback_data=f"link:{token}"),
        InlineKeyboardButton("📤 Archivo", callback_data=f"file:{token}"),
    ]
    rows = [row]
    if cfg.dripfiles:
        rows.append(
            [
                InlineKeyboardButton(
                    "💧 DripFiles", callback_data=f"drip:{token}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def remember_ytdlp(media: ytdlp_mod.MediaProbe) -> str:
    if len(pending_ytdlp) > 200:
        for key in list(pending_ytdlp)[:50]:
            pending_ytdlp.pop(key, None)
    token = uuid.uuid4().hex[:12]
    pending_ytdlp[token] = media
    return token


def remember_ad_streams(probe: AdStreamProbe) -> str:
    if len(pending_ad_streams) > 200:
        for key in list(pending_ad_streams)[:50]:
            pending_ad_streams.pop(key, None)
    token = uuid.uuid4().hex[:12]
    pending_ad_streams[token] = probe
    return token


def quality_keyboard(token: str, options: list[ytdlp_mod.QualityOption]) -> InlineKeyboardMarkup:
    """Botones de calidad yt-dlp en filas de 2 (estilo utube-bot)."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for opt in options:
        # callback_data max 64 bytes: yq:{12}:{key} cabe holgado
        row.append(
            InlineKeyboardButton(opt.label, callback_data=f"yq:{token}:{opt.key}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def ad_stream_keyboard(token: str, probe: AdStreamProbe) -> InlineKeyboardMarkup:
    """Botones de calidad AllDebrid streaming (aq:token:stream_id)."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for opt in probe.streams:
        # stream ids tipo "137+140" → caben en 64 bytes con token de 12
        sid = opt.id.replace(":", "_")
        label = opt.label[:60] if len(opt.label) > 60 else opt.label
        row.append(
            InlineKeyboardButton(label, callback_data=f"aq:{token}:{sid}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def describe_ad_streams(probe: AdStreamProbe) -> str:
    host = probe.host or probe.host_domain or "stream"
    lines = [
        f"🎬 **{probe.filename}**",
        f"🌐 **{host}**",
        f"⚙️ **Servicio:** AllDebrid",
        "",
        "Elige la **calidad** (stream AllDebrid):",
    ]
    return "\n".join(lines)


def ytdlp_format(link: UnrestrictedLink) -> str:
    return link.format_selector or cfg.ytdlp_format


def service_keyboard(user_id: int) -> InlineKeyboardMarkup:
    active = active_slug(user_id)
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if slug == active else "") + provider.name,
                callback_data=f"svc:{slug}",
            )
        ]
        for slug, provider in providers.items()
    ]
    return InlineKeyboardMarkup(rows)


async def safe_edit(message: Message, text: str, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except MessageNotModified:
        pass
    except Exception:
        log.exception("No se pudo editar el mensaje")


# ---------------------------------------------------------------- comandos

def build_help_text() -> str:
    lines = [
        "👋 **Bot Debrid**\n",
        "Envíame:",
        "• Un **enlace** de un hoster → lo desbloqueo y eliges: enlace directo o que te suba el archivo.",
        "• Un **enlace directo** a un archivo (GitHub Releases, CDN, etc.) → si el debrid no lo "
        "soporta, lo descargo yo (solo archivos reales, no páginas HTML).",
    ]
    if cfg.ytdlp:
        lines.append(
            "• Un **vídeo** de YouTube, Vimeo y otros sitios soportados por **yt-dlp** → "
            "elijo la **calidad** (1080p, 720p, audio…) y luego enlace o archivo."
        )
    lines.append(
        "• Un **envío de DripFiles** (`https://dripfiles.com/XXXXXXXX`) → te lo descargo; "
        "si tiene varios archivos, eliges cuál."
    )
    if cfg.dripfiles:
        lines.append(
            "• Tras desbloquear: **💧 DripFiles** sube el archivo a "
            "[dripfiles.com](https://dripfiles.com) (API free, enlace ~2 días)."
        )
    lines.extend(
        [
            "• Un **paste de controlc.com** → extraigo sus enlaces y los desbloqueo todos.",
            "• Una **carpeta de filecrypt.cc** → extraigo los enlaces y los desbloqueo. "
            "Si pide contraseña, mándala después del enlace. Con captcha se abre Chrome + uBlock.",
            "• Un **magnet** o un archivo **.torrent** → lo descargo en tu servicio debrid "
            "y cuando termine te doy los archivos.\n",
            "**Comandos:**",
            "/wget `URL [nombre]` — descarga la URL **tal cual** (sin debrid ni "
            "comprobaciones) y te la subo aquí; el nombre es opcional, como `wget -O`",
            "/service — elegir servicio debrid",
            "/torrents — gestionar tus torrents: ver progreso, obtener enlaces, "
            "reiniciar o eliminar",
            "/help — esta ayuda",
        ]
    )
    return "\n".join(lines)


@app.on_message(filters.command(["start", "help"]) & filters.private & auth)
async def cmd_start(_, message: Message):
    await message.reply_text(build_help_text())


@app.on_message(filters.command("service") & filters.private & auth)
async def cmd_service(_, message: Message):
    if not providers:
        await message.reply_text("❌ No hay servicios debrid configurados.")
        return
    await message.reply_text(
        "⚙️ Elige el servicio debrid activo:",
        reply_markup=service_keyboard(message.from_user.id),
    )


@app.on_message(filters.command("wget") & filters.private & auth)
async def cmd_wget(client: Client, message: Message):
    """/wget URL [nombre] — descarga la URL tal cual, sin debrid ni comprobaciones."""
    parts = message.text.split(maxsplit=2)
    url = parts[1].strip() if len(parts) > 1 else ""
    if not is_url(url):
        await message.reply_text(
            "Uso: `/wget https://ejemplo.com/archivo.zip [nombre.ext]`\n\n"
            "Descarga la URL tal cual (sin debrid, sin yt-dlp y sin comprobar si "
            "«parece» un archivo) y te la subo aquí. El nombre es opcional, como `wget -O`."
        )
        return

    rename = safe_filename(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else None
    status = await message.reply_text("🔎 Comprobando la URL...")
    try:
        link = await probe_raw_file(http, url)
    except DirectDownloadError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    except Exception as exc:
        log.exception("wget: no pude abrir %s", url)
        await safe_edit(status, f"❌ No pude abrir la URL: {exc}")
        return

    if rename:
        link = UnrestrictedLink(
            url=link.url, filename=rename, host=link.host, size=link.size
        )
    if link.size and link.size > MAX_TG_SIZE:
        await safe_edit(
            status,
            f"❌ **{link.filename}** pesa {human_size(link.size)} y supera "
            "el límite de 2 GB de Telegram.",
        )
        return

    try:
        await status.delete()
    except Exception:
        pass
    # session=http: es una descarga propia, no debe salir por DEBRID_PROXY.
    # autoname: sin nombre pedido, el del servidor manda (el sondeo puede fallar)
    spawn(
        transfer(client, message, link, session=http, raw=True, autoname=not rename)
    )


TORRENTS_PAGE_SIZE = 8
# clave -> (etiqueta del botón, estados que incluye; None = todos)
TORRENT_FILTERS = {
    "all": ("Todos", None),
    "active": ("⏬", ("queued", "downloading")),
    "ready": ("✅", ("ready",)),
    "error": ("❌", ("error",)),
}
# última página/filtro que veía cada chat, para que "Volver" no te mande a la primera
torrents_view_state: dict[int, tuple[int, str]] = {}


async def torrents_list_view(
    provider: DebridProvider, chat_id: int, page: int = 0, flt: str = "all"
) -> tuple[str, InlineKeyboardMarkup]:
    if flt not in TORRENT_FILTERS:
        flt = "all"
    torrents = await provider.list_torrents()
    statuses = TORRENT_FILTERS[flt][1]
    filtered = [t for t in torrents if statuses is None or t.status in statuses]

    pages = max(1, -(-len(filtered) // TORRENTS_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    torrents_view_state[chat_id] = (page, flt)
    slug = provider.slug

    rows = []
    for torrent in filtered[page * TORRENTS_PAGE_SIZE : (page + 1) * TORRENTS_PAGE_SIZE]:
        emoji = STATUS_EMOJI.get(torrent.status, "❔")
        label = f"{emoji} {torrent.name[:35]} · {torrent.progress:.0f}%"
        rows.append([InlineKeyboardButton(label, callback_data=f"tor:{slug}:{torrent.id}")])

    filter_row = [
        InlineKeyboardButton(
            f"· {label} ·" if key == flt else label,
            callback_data=f"torlist:{slug}:0:{key}",
        )
        for key, (label, _) in TORRENT_FILTERS.items()
    ]
    rows.append(filter_row)

    nav_row = []
    if pages > 1:
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("⬅️", callback_data=f"torlist:{slug}:{page - 1}:{flt}")
            )
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop:-"))
        if page < pages - 1:
            nav_row.append(
                InlineKeyboardButton("➡️", callback_data=f"torlist:{slug}:{page + 1}:{flt}")
            )
    nav_row.append(InlineKeyboardButton("🔄", callback_data=f"torlist:{slug}:{page}:{flt}"))
    rows.append(nav_row)

    if not torrents:
        text = f"No tienes torrents en {provider.name}."
    elif not filtered:
        text = f"🧲 **{provider.name}**: ningún torrent con ese filtro (hay {len(torrents)} en total)."
    else:
        shown = f"{len(filtered)}" if flt == "all" else f"{len(filtered)} de {len(torrents)}"
        text = f"🧲 **Torrents en {provider.name}** ({shown})\nToca uno para gestionarlo:"
    return text, InlineKeyboardMarkup(rows)


async def torrent_detail_view(
    provider: DebridProvider, torrent_id: str
) -> tuple[str, InlineKeyboardMarkup]:
    info = await provider.torrent_info(torrent_id)
    emoji = STATUS_EMOJI.get(info.status, "❔")
    text = (
        f"{emoji} **{info.name}**\n"
        f"`[{progress_bar(info.progress)}]` {info.progress:.1f}%\n"
        f"Estado: {info.detail or info.status} · {provider.name}"
    )
    slug = provider.slug
    rows = []
    if info.status == "ready":
        rows.append(
            [InlineKeyboardButton("📂 Obtener enlaces", callback_data=f"torlinks:{slug}:{torrent_id}")]
        )
    action_row = [InlineKeyboardButton("🔄 Actualizar", callback_data=f"tor:{slug}:{torrent_id}")]
    if provider.supports_restart and info.status == "error":
        action_row.append(
            InlineKeyboardButton("♻️ Reiniciar", callback_data=f"torre:{slug}:{torrent_id}")
        )
    rows.append(action_row)
    last_row = []
    if provider.supports_delete:
        last_row.append(
            InlineKeyboardButton("🗑 Eliminar", callback_data=f"tordel:{slug}:{torrent_id}")
        )
    last_row.append(InlineKeyboardButton("⬅️ Volver", callback_data=f"torlist:{slug}"))
    rows.append(last_row)
    return text, InlineKeyboardMarkup(rows)


@app.on_message(filters.command("torrents") & filters.private & auth)
async def cmd_torrents(_, message: Message):
    if not providers:
        await message.reply_text("❌ No hay servicios debrid configurados.")
        return
    provider = provider_for(message.from_user.id)
    status = await message.reply_text(f"🔎 Consultando torrents en {provider.name}...")
    try:
        text, keyboard = await torrents_list_view(provider, message.chat.id)
    except DebridError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    await safe_edit(status, text, reply_markup=keyboard)


# ---------------------------------------------------------------- mensajes

@app.on_message(filters.document & filters.private & auth)
async def handle_document(client: Client, message: Message):
    document = message.document
    if not document.file_name or not document.file_name.lower().endswith(".torrent"):
        await message.reply_text("Solo acepto archivos `.torrent`.")
        return
    if not providers:
        await message.reply_text("❌ No hay servicios debrid configurados para torrents.")
        return
    buffer = await client.download_media(message, in_memory=True)
    raw = bytes(buffer.getbuffer())
    provider = provider_for(message.from_user.id)
    await start_torrent(provider, message, raw=raw, filename=document.file_name)


@app.on_message(filters.text & filters.private & auth)
async def handle_text(_, message: Message):
    parts = message.text.strip().split()
    text = parts[0]
    if text.startswith("/"):
        return

    if text.startswith("magnet:"):
        if not providers:
            await message.reply_text("❌ No hay servicios debrid configurados para torrents.")
            return
        provider = provider_for(message.from_user.id)
        await start_torrent(provider, message, magnet=message.text.strip())
        return

    if not is_url(text):
        await message.reply_text("Envíame un enlace, un magnet o un archivo `.torrent` ♿️")
        return

    if urlparse(text).netloc.endswith("controlc.com"):
        if not providers:
            await message.reply_text("❌ No hay servicios debrid configurados.")
            return
        provider = provider_for(message.from_user.id)
        await handle_paste(provider, message, text)
        return

    if dripfiles_mod.is_share_url(text):
        await handle_dripfiles(message, text)
        return

    if is_filecrypt(text):
        if not providers:
            await message.reply_text("❌ No hay servicios debrid configurados.")
            return
        # segunda palabra opcional = contraseña de la carpeta
        password = parts[1] if len(parts) > 1 else None
        provider = provider_for(message.from_user.id)
        await handle_filecrypt(provider, message, text, password)
        return

    url = normalize_mirrors(text)
    status: Message | None = None

    # Instagram y similares: menú yt-dlp primero (si está habilitado)
    if cfg.ytdlp and prefers_ytdlp_first(url):
        status = await message.reply_text("🎬 Buscando calidades con yt-dlp...")
        try:
            media = await ytdlp_mod.probe(url)
        except ytdlp_mod.YtDlpError as ytdlp_exc:
            log.info("yt-dlp (preferido) falló para %s: %s; debrid", url, ytdlp_exc)
            await safe_edit(
                status,
                f"🎬 yt-dlp no pudo (se prueba debrid)…\n`{ytdlp_exc}`",
            )
        else:
            if media.options:
                token = remember_ytdlp(media)
                await safe_edit(
                    status,
                    ytdlp_mod.describe_media(media),
                    reply_markup=quality_keyboard(token, media.options),
                )
                return
            await safe_edit(status, "🎬 yt-dlp sin formatos; probando debrid…")

    if providers:
        first = providers_for_url(message.from_user.id, url)[0]
        status_text = f"🔎 Desbloqueando con {first.name}..."
    else:
        status_text = "🔎 Procesando enlace..."
    if status is None:
        status = await message.reply_text(status_text)
    else:
        await safe_edit(status, status_text)

    try:
        # sin yt-dlp automático aquí: si falla debrid/directo, menú de calidades
        # (excepto Instagram, ya intentado arriba)
        link, provider_name = await resolve_link(
            message.from_user.id, url, allow_ytdlp=False, auto_pick_stream=False
        )
    except NeedsStreamChoice as stream_exc:
        probe = stream_exc.probe
        token = remember_ad_streams(probe)
        await safe_edit(
            status,
            describe_ad_streams(probe),
            reply_markup=ad_stream_keyboard(token, probe),
        )
        return
    except DebridError as debrid_exc:
        if not cfg.ytdlp:
            await safe_edit(status, f"❌ {debrid_exc}")
            return
        if prefers_ytdlp_first(url):
            # ya se intentó yt-dlp al principio
            await safe_edit(status, f"❌ {debrid_exc}")
            return
        await safe_edit(status, "🎬 Buscando calidades con yt-dlp...")
        try:
            media = await ytdlp_mod.probe(url)
        except ytdlp_mod.YtDlpError as ytdlp_exc:
            await safe_edit(
                status,
                f"❌ {debrid_exc}\n\n🎬 yt-dlp: {ytdlp_exc}",
            )
            return
        if not media.options:
            await safe_edit(status, f"❌ {debrid_exc}\n\n🎬 yt-dlp: sin formatos")
            return
        token = remember_ytdlp(media)
        await safe_edit(
            status,
            ytdlp_mod.describe_media(media),
            reply_markup=quality_keyboard(token, media.options),
        )
        return

    if provider_name == DIRECT_PROVIDER:
        await safe_edit(status, "⬇️ Enlace directo detectado...")
    token = remember(link, provider_name)
    await safe_edit(status, describe(link, provider_name), reply_markup=action_keyboard(token))


MAX_CONTAINER_LINKS = 20


async def unlock_many(
    provider: DebridProvider,
    message: Message,
    status: Message,
    links: list[str],
    label: str,
    emoji: str,
):
    """Desbloquea una lista de enlaces y manda cada uno con sus botones."""
    total = min(len(links), MAX_CONTAINER_LINKS)
    await safe_edit(status, f"{emoji} {total} enlace(s). Desbloqueando con {provider.name}...")
    unlocked = 0
    for raw_link in links[:MAX_CONTAINER_LINKS]:
        try:
            link, provider_name = await resolve_link(
                message.from_user.id, normalize_mirrors(raw_link)
            )
        except DebridError as exc:
            await message.reply_text(f"❌ `{raw_link}`\n{exc}")
            continue
        token = remember(link, provider_name)
        await message.reply_text(
            describe(link, provider_name), reply_markup=action_keyboard(token)
        )
        unlocked += 1
    summary = f"{emoji} {label}: {unlocked}/{total} enlace(s) desbloqueado(s)."
    if len(links) > MAX_CONTAINER_LINKS:
        summary += f" (Había {len(links)}, procesados los primeros {MAX_CONTAINER_LINKS}.)"
    await safe_edit(status, summary)


async def handle_dripfiles(message: Message, url: str):
    """Envío de DripFiles: lista sus archivos y ofrece enlace/archivo para cada uno."""
    status = await message.reply_text("💧 Leyendo el envío de DripFiles...")
    try:
        links, errors, total = await dripfiles_links(url)
    except dripfiles_mod.DripFilesError as exc:
        await safe_edit(status, f"❌ DripFiles: {exc}")
        return
    except Exception as exc:
        log.exception("Error leyendo el envío de DripFiles %s", url)
        await safe_edit(status, f"❌ No pude leer el envío de DripFiles: {exc}")
        return

    if not links:
        detail = "\n".join(errors) if errors else "no tiene archivos descargables."
        await safe_edit(status, f"❌ DripFiles: {detail}")
        return

    if total == 1:
        # un solo archivo: el mensaje de estado se convierte en la ficha
        token = remember(links[0], DRIPFILES_PROVIDER)
        await safe_edit(
            status,
            describe(links[0], DRIPFILES_PROVIDER),
            reply_markup=action_keyboard(token),
        )
        return

    for link in links:
        token = remember(link, DRIPFILES_PROVIDER)
        await message.reply_text(
            describe(link, DRIPFILES_PROVIDER), reply_markup=action_keyboard(token)
        )
    for error in errors:
        await message.reply_text(f"❌ {error}")

    summary = f"💧 Envío de DripFiles: {len(links)}/{min(total, MAX_CONTAINER_LINKS)} archivo(s) listo(s)."
    if total > MAX_CONTAINER_LINKS:
        summary += f" (Tiene {total}, procesados los primeros {MAX_CONTAINER_LINKS}.)"
    await safe_edit(status, summary)


async def handle_paste(provider: DebridProvider, message: Message, url: str):
    status = await message.reply_text("📋 Leyendo el paste de controlc...")
    try:
        paste_links = await get_paste_links(http, url)
    except Exception:
        log.exception("Error leyendo paste %s", url)
        await safe_edit(status, "❌ No pude leer el paste de controlc.")
        return
    if not paste_links:
        await safe_edit(status, "❌ El paste no contiene ningún enlace.")
        return
    await unlock_many(provider, message, status, paste_links, "Paste procesado", "📋")


async def handle_filecrypt(
    provider: DebridProvider, message: Message, url: str, password: str | None
):
    status = await message.reply_text(
        "🔐 Abriendo carpeta de filecrypt...\n"
        "_(Si hay captcha se abre Chromium + uBlock; al pasarlo se guardan cookies "
        "para no repetirlo. Puede tardar un minuto.)_"
    )
    try:
        folder_links = await get_folder_links(http, url, password)
    except PasswordRequired as exc:
        await safe_edit(
            status,
            f"🔑 {exc}.\nMándame de nuevo el enlace seguido de la contraseña:\n"
            "`https://filecrypt.cc/Container/XXXX.html micontraseña`",
        )
        return
    except CaptchaRequired as exc:
        await safe_edit(
            status,
            f"🤖 No pude pasar el captcha de filecrypt.\n{exc}\n\n"
            "Ábrela en el navegador (con adblock) y mándame los enlaces.",
        )
        return
    except FilecryptError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    except Exception:
        log.exception("Error leyendo filecrypt %s", url)
        await safe_edit(status, "❌ No pude leer la carpeta de filecrypt.")
        return
    await unlock_many(provider, message, status, folder_links, "Carpeta procesada", "🔐")


# ---------------------------------------------------------------- torrents

async def start_torrent(
    provider: DebridProvider,
    message: Message,
    *,
    magnet: str | None = None,
    raw: bytes | None = None,
    filename: str | None = None,
):
    status = await message.reply_text(f"🧲 Añadiendo torrent a {provider.name}...")
    try:
        if magnet:
            torrent_id = await provider.add_magnet(magnet)
        else:
            torrent_id = await provider.add_torrent_file(raw, filename or "upload.torrent")
    except DebridError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    except Exception:
        log.exception("Error añadiendo torrent")
        await safe_edit(status, "❌ Error inesperado añadiendo el torrent.")
        return
    spawn(monitor_torrent(provider, status, torrent_id))


async def monitor_torrent(provider: DebridProvider, status: Message, torrent_id: str):
    last_text = ""
    deadline = time.monotonic() + 3 * 3600
    while time.monotonic() < deadline:
        try:
            info = await provider.torrent_info(torrent_id)
        except DebridError as exc:
            await safe_edit(status, f"❌ {exc}")
            return
        except Exception:
            log.exception("Error consultando torrent %s", torrent_id)
            await asyncio.sleep(15)
            continue

        if info.status == "error":
            await safe_edit(status, f"❌ Torrent en error: {info.detail or 'desconocido'}")
            return

        if info.status == "ready":
            await safe_edit(status, f"✅ **{info.name}**\nCompletado, generando enlaces...")
            try:
                links = await provider.torrent_links(torrent_id)
            except DebridError as exc:
                await safe_edit(status, f"❌ {exc}")
                return
            if not links:
                await safe_edit(status, "❌ El torrent no generó ningún enlace.")
                return
            await safe_edit(status, f"✅ **{info.name}** — {len(links)} archivo(s)")
            for link in links[:MAX_TORRENT_FILES]:
                token = remember(link, provider)
                await status.reply_text(
                    describe(link, provider.name), reply_markup=action_keyboard(token)
                )
            if len(links) > MAX_TORRENT_FILES:
                await status.reply_text(
                    f"… y {len(links) - MAX_TORRENT_FILES} archivo(s) más en la web de {provider.name}."
                )
            return

        text = (
            f"🧲 **{info.name}**\n"
            f"`[{progress_bar(info.progress)}]` {info.progress:.1f}%\n"
            f"Estado: {info.detail or info.status} · {provider.name}"
        )
        if text != last_text:
            await safe_edit(status, text)
            last_text = text
        await asyncio.sleep(10)

    await safe_edit(status, "⏰ Tiempo de espera agotado. Usa /torrents para revisar el estado.")


# ---------------------------------------------------------------- callbacks

@app.on_callback_query()
async def on_callback(client: Client, query: CallbackQuery):
    if cfg.allowed_users and query.from_user.id not in cfg.allowed_users:
        await query.answer("No autorizado.", show_alert=True)
        return

    action, _, value = query.data.partition(":")

    if action == "noop":  # indicador de página, no hace nada
        await query.answer()
        return

    if action in ("tor", "torlist", "torlinks", "torre", "tordel", "tordelok"):
        await handle_torrent_callback(query, action, value)
        return

    if action == "svc":
        if value not in providers:
            await query.answer("Servicio no disponible.", show_alert=True)
            return
        user_service[query.from_user.id] = value
        await query.answer(f"Servicio activo: {providers[value].name}")
        try:
            await query.message.edit_reply_markup(service_keyboard(query.from_user.id))
        except MessageNotModified:
            pass
        return

    # yq:token:quality_key — menú de calidades yt-dlp
    if action == "yq":
        token, _, qkey = value.partition(":")
        media = pending_ytdlp.get(token)
        if not media:
            await query.answer("Este menú ha caducado, envía el enlace de nuevo.", show_alert=True)
            return
        option = next((o for o in media.options if o.key == qkey), None)
        if not option:
            await query.answer("Calidad no disponible.", show_alert=True)
            return
        await query.answer(option.label)
        link = ytdlp_mod.link_from_probe(media, option)
        # mantenemos el probe por si quiere volver atrás… pero simplificamos:
        # pasamos a Enlace/Archivo y liberamos el menú de calidades
        pending_ytdlp.pop(token, None)
        action_token = remember(link, YTDLP_PROVIDER)
        back_rows = action_keyboard(action_token).inline_keyboard
        # botón para reabrir calidades si el probe aún… ya lo borramos; re-probe es caro
        await safe_edit(
            query.message,
            describe(link, YTDLP_PROVIDER) + f"\n\n✅ Calidad: **{option.label}**",
            reply_markup=InlineKeyboardMarkup(back_rows),
        )
        return

    # aq:token:stream_id — menú de calidades AllDebrid streaming
    if action == "aq":
        token, _, stream_key = value.partition(":")
        probe = pending_ad_streams.get(token)
        if not probe:
            await query.answer(
                "Este menú ha caducado, envía el enlace de nuevo.", show_alert=True
            )
            return
        # restaurar id por si sustituimos ":" al crear el botón
        option = next(
            (o for o in probe.streams if o.id == stream_key or o.id.replace(":", "_") == stream_key),
            None,
        )
        if not option:
            await query.answer("Calidad no disponible.", show_alert=True)
            return
        ad = providers.get(AllDebrid.slug)
        if not isinstance(ad, AllDebrid):
            await query.answer("AllDebrid no está configurado.", show_alert=True)
            return
        await query.answer(option.label)
        await safe_edit(
            query.message,
            describe_ad_streams(probe)
            + f"\n\n⏳ Generando **{option.label}**…",
        )
        try:
            link = await ad.select_stream(
                probe.unlock_id,
                option.id,
                filename=probe.filename,
                host=probe.host or "stream",
            )
        except DebridError as exc:
            await safe_edit(
                query.message,
                describe_ad_streams(probe) + f"\n\n❌ {exc}",
                reply_markup=ad_stream_keyboard(token, probe),
            )
            return
        pending_ad_streams.pop(token, None)
        action_token = remember(link, ad.name)
        await safe_edit(
            query.message,
            describe(link, ad.name) + f"\n\n✅ Calidad: **{option.label}**",
            reply_markup=action_keyboard(action_token),
        )
        return

    if action in ("link", "file", "drip"):
        entry = pending.get(value)
        if not entry:
            await query.answer("Este enlace ha caducado, envíalo de nuevo.", show_alert=True)
            return
        link, provider_name = entry
        is_ytdlp = provider_name == YTDLP_PROVIDER or link.via == "ytdlp"

        if action == "link":
            await query.answer()
            if is_ytdlp:
                await safe_edit(
                    query.message,
                    describe(link, provider_name) + "\n\n🎬 Obteniendo URL directa...",
                )
                try:
                    direct = await ytdlp_mod.stream_url(
                        link.url, ytdlp_format(link)
                    )
                except ytdlp_mod.YtDlpError as exc:
                    await safe_edit(
                        query.message,
                        describe(link, provider_name)
                        + f"\n\n❌ No hay URL directa usable: {exc}\n"
                        "Prueba la opción **📤 Archivo** o **💧 DripFiles**.",
                    )
                    return
                await safe_edit(
                    query.message,
                    describe(link, provider_name)
                    + f"\n\n🔗 [Descargar]({direct})\n\n"
                    "_URL temporal de la plataforma; puede caducar. "
                    "Para HLS/DASH usa 📤 Archivo o 💧 DripFiles._",
                )
                return
            await safe_edit(
                query.message,
                describe(link, provider_name) + f"\n\n🔗 [Descargar]({public_url(link)})",
            )
            return

        if action == "drip":
            if not cfg.dripfiles:
                await query.answer("DripFiles está desactivado.", show_alert=True)
                return
            if link.size and link.size > dripfiles_mod.MAX_SIZE:
                await query.answer()
                await safe_edit(
                    query.message,
                    describe(link, provider_name)
                    + f"\n\n❌ El tamaño estimado ({human_size(link.size)}) supera "
                    "el límite free de DripFiles (2 GB).",
                )
                return
            await query.answer("Subiendo a DripFiles...")
            try:
                await query.message.edit_reply_markup(None)
            except Exception:
                pass
            spawn(
                transfer_to_dripfiles(
                    query.message,
                    link,
                    session=session_for(provider_name),
                    via_ytdlp=is_ytdlp,
                )
            )
            return

        # action == "file"
        if link.size and link.size > MAX_TG_SIZE:
            await query.answer()
            size_note = (
                f"\n\n❌ Supera el límite de 2 GB de Telegram."
                if not is_ytdlp
                else f"\n\n❌ El tamaño estimado ({human_size(link.size)}) supera el límite "
                "de 2 GB de Telegram."
            )
            if is_ytdlp:
                await safe_edit(query.message, describe(link, provider_name) + size_note)
            else:
                await safe_edit(
                    query.message,
                    describe(link, provider_name)
                    + size_note
                    + f"\n🔗 [Descargar]({public_url(link)})",
                )
            return

        await query.answer("Preparando la transferencia...")
        try:
            await query.message.edit_reply_markup(None)
        except Exception:
            pass
        spawn(
            transfer(
                client,
                query.message,
                link,
                session=session_for(provider_name),
                via_ytdlp=is_ytdlp,
            )
        )


async def handle_torrent_callback(query: CallbackQuery, action: str, value: str):
    slug, _, torrent_id = value.partition(":")
    provider = providers.get(slug)
    if not provider:
        await query.answer("Ese servicio ya no está configurado.", show_alert=True)
        return

    chat_id = query.message.chat.id
    try:
        if action == "torlist":
            await query.answer()
            # torlist:slug[:página:filtro]; sin ellos ("Volver") recupera los últimos
            page_s, _, flt = torrent_id.partition(":")
            if page_s:
                page, flt = int(page_s), flt or "all"
            else:
                page, flt = torrents_view_state.get(chat_id, (0, "all"))
            text, keyboard = await torrents_list_view(provider, chat_id, page, flt)
            await safe_edit(query.message, text, reply_markup=keyboard)

        elif action == "tor":
            await query.answer()
            text, keyboard = await torrent_detail_view(provider, torrent_id)
            await safe_edit(query.message, text, reply_markup=keyboard)

        elif action == "torlinks":
            await query.answer("Generando enlaces...")
            await safe_edit(query.message, "📂 Generando enlaces...")
            links = await provider.torrent_links(torrent_id)
            if not links:
                await safe_edit(query.message, "❌ El torrent no generó ningún enlace.")
                return
            for link in links[:MAX_TORRENT_FILES]:
                token = remember(link, provider)
                await query.message.reply_text(
                    describe(link, provider.name), reply_markup=action_keyboard(token)
                )
            extra = (
                f" (+{len(links) - MAX_TORRENT_FILES} más en la web)"
                if len(links) > MAX_TORRENT_FILES
                else ""
            )
            await safe_edit(query.message, f"📂 {len(links)} archivo(s){extra} ⬇️")

        elif action == "torre":
            await provider.restart_torrent(torrent_id)
            await query.answer("♻️ Torrent reiniciado.")
            text, keyboard = await torrent_detail_view(provider, torrent_id)
            await safe_edit(query.message, text, reply_markup=keyboard)

        elif action == "tordel":
            await query.answer()
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚠️ Sí, eliminar", callback_data=f"tordelok:{slug}:{torrent_id}"
                        ),
                        InlineKeyboardButton(
                            "Cancelar", callback_data=f"tor:{slug}:{torrent_id}"
                        ),
                    ]
                ]
            )
            try:
                await query.message.edit_reply_markup(keyboard)
            except MessageNotModified:
                pass

        elif action == "tordelok":
            await provider.delete_torrent(torrent_id)
            await query.answer("🗑 Torrent eliminado.")
            page, flt = torrents_view_state.get(chat_id, (0, "all"))
            text, keyboard = await torrents_list_view(provider, chat_id, page, flt)
            await safe_edit(query.message, text, reply_markup=keyboard)

    except DebridError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception:
        log.exception("Error en callback de torrent %s", query.data)
        await query.answer("❌ Error inesperado.", show_alert=True)


# ---------------------------------------------------------------- transferencia

async def transfer(
    client: Client,
    message: Message,
    link: UnrestrictedLink,
    session: aiohttp.ClientSession | None = None,
    via_ytdlp: bool = False,
    raw: bool = False,
    autoname: bool = False,
):
    progress = await message.reply_text("⬇️ Descargando...")
    path = None
    try:
        path, link = await materialize_download(
            link, progress, session=session, via_ytdlp=via_ytdlp, raw=raw, autoname=autoname
        )
        await upload_to_telegram(client, message.chat.id, path, link, progress)
        await progress.delete()
    except FileTooLarge as exc:
        extra = ""
        if not via_ytdlp:
            extra = f"\n🔗 [Descargar]({public_url(link)})"
        await safe_edit(
            progress,
            f"❌ El archivo pesa {human_size(exc.size)} y supera el límite de 2 GB."
            + extra,
        )
    except ytdlp_mod.YtDlpError as exc:
        await safe_edit(progress, f"❌ yt-dlp: {exc}")
    except aiohttp.ClientResponseError as exc:
        log.info("El servidor respondió %s descargando %s", exc.status, link.filename)
        await safe_edit(progress, f"❌ El servidor respondió HTTP {exc.status}")
    except aiohttp.ClientError as exc:
        log.info("Fallo de red descargando %s: %s", link.filename, exc)
        await safe_edit(progress, f"❌ Error de red: {exc}")
    except Exception as exc:
        log.exception("Error transfiriendo %s", link.filename)
        await safe_edit(progress, f"❌ Error en la transferencia: {exc}")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def materialize_download(
    link: UnrestrictedLink,
    progress: Message,
    *,
    session: aiohttp.ClientSession | None = None,
    via_ytdlp: bool = False,
    raw: bool = False,
    autoname: bool = False,
) -> tuple[str, UnrestrictedLink]:
    """Descarga el archivo a disco (debrid/directo o yt-dlp) y ajusta el nombre."""
    if via_ytdlp:
        path = await download_with_ytdlp(link, progress)
    else:
        path = await download_file(
            link,
            progress,
            session=session or debrid_http,
            allow_html=raw,
            # el modo wget se anuncia como wget también al descargar, no solo al sondear
            user_agent=WGET_USER_AGENT if raw else None,
            name_from_response=autoname,
        )
    final_name = os.path.basename(path)
    if (via_ytdlp or autoname) and final_name and final_name != link.filename:
        display = re.sub(r"^[0-9a-f]{8}_", "", final_name, count=1)
        link = UnrestrictedLink(
            url=link.url,
            filename=display or link.filename,
            host=link.host,
            size=link.size,
            via=link.via,
            format_selector=link.format_selector,
        )
    return path, link


def _dripfiles_message(link: UnrestrictedLink, size: int | None = None) -> str:
    """Construye el mensaje/descripción para la API free de DripFiles."""
    template = cfg.dripfiles_message or "{filename}"
    try:
        return template.format(
            filename=link.filename or "archivo",
            host=link.host or "—",
            size=human_size(size if size is not None else (link.size or 0)),
        ).strip()
    except (KeyError, ValueError) as exc:
        log.warning("DRIPFILES_MESSAGE inválido (%s); uso solo el nombre", exc)
        return link.filename or "archivo"


async def transfer_to_dripfiles(
    message: Message,
    link: UnrestrictedLink,
    session: aiohttp.ClientSession | None = None,
    via_ytdlp: bool = False,
):
    """Descarga el archivo y lo sube a DripFiles; devuelve el enlace público."""
    progress = await message.reply_text("⬇️ Descargando para DripFiles...")
    path = None
    try:
        path, link = await materialize_download(
            link, progress, session=session, via_ytdlp=via_ytdlp
        )
        size = os.path.getsize(path)
        if size > dripfiles_mod.MAX_SIZE:
            raise FileTooLarge(size)

        last_edit = [0.0]

        def on_progress(uploaded: int, total: int) -> None:
            now = time.monotonic()
            if now - last_edit[0] < 3 and uploaded < total:
                return
            last_edit[0] = now
            if total:
                pct = min(99.0, uploaded * 100 / total)
                text = (
                    f"💧 Subiendo a **DripFiles** · {link.filename}\n"
                    f"`[{progress_bar(pct)}]` {pct:.1f}% "
                    f"({human_size(uploaded)}/{human_size(total)})"
                )
            else:
                text = (
                    f"💧 Subiendo a **DripFiles** · {link.filename}\n"
                    f"Subido: {human_size(uploaded)}"
                )
            spawn(safe_edit(progress, text))

        await safe_edit(
            progress,
            f"💧 Subiendo a **DripFiles** · {link.filename}\nPreparando...",
        )
        drip_message = _dripfiles_message(link, size)
        result = await dripfiles_mod.upload_path(
            http,
            path,
            link.filename,
            message=drip_message,
            on_progress=on_progress,
        )
        url = result.get("url") or ""
        expires = result.get("expires_at")
        expire_note = ""
        if isinstance(expires, (int, float)) and expires > 0:
            # expires_at viene como unix; free = ~2 días
            expire_note = "\n⏱ Caduca en ~2 días (plan free)."
        msg_note = f"\n💬 {drip_message}" if drip_message else ""
        await safe_edit(
            progress,
            describe(link, "DripFiles")
            + f"\n\n💧 [Descargar en DripFiles]({url})"
            + msg_note
            + expire_note,
        )
    except FileTooLarge as exc:
        await safe_edit(
            progress,
            f"❌ El archivo pesa {human_size(exc.size)} y supera el límite free "
            "de DripFiles (2 GB).",
        )
    except dripfiles_mod.DripFilesError as exc:
        await safe_edit(progress, f"❌ DripFiles: {exc}")
    except ytdlp_mod.YtDlpError as exc:
        await safe_edit(progress, f"❌ yt-dlp: {exc}")
    except Exception as exc:
        log.exception("Error subiendo a DripFiles %s", link.filename)
        await safe_edit(progress, f"❌ Error subiendo a DripFiles: {exc}")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def download_with_ytdlp(link: UnrestrictedLink, progress: Message) -> str:
    """Descarga con yt-dlp (soporta HLS/DASH y fusión vídeo+audio)."""
    loop = asyncio.get_running_loop()
    last_edit = [0.0]
    # el hook corre en un hilo: encola ediciones en el loop del bot
    pending_update: list[tuple[int, int] | None] = [None]
    update_scheduled = [False]

    async def flush_progress():
        update_scheduled[0] = False
        data = pending_update[0]
        if not data:
            return
        downloaded, total = data
        now = time.monotonic()
        if now - last_edit[0] < 3:
            return
        last_edit[0] = now
        if total:
            pct = min(99.0, downloaded * 100 / total)
            text = (
                f"⬇️ yt-dlp · **{link.filename}**\n"
                f"`[{progress_bar(pct)}]` {pct:.1f}% "
                f"({human_size(downloaded)}/{human_size(total)})"
            )
        else:
            text = (
                f"⬇️ yt-dlp · **{link.filename}**\n"
                f"Descargado: {human_size(downloaded)}"
            )
        await safe_edit(progress, text)

    def on_progress(downloaded: int, total: int) -> None:
        pending_update[0] = (downloaded, total)
        if update_scheduled[0]:
            return
        update_scheduled[0] = True
        loop.call_soon_threadsafe(lambda: spawn(flush_progress()))

    await safe_edit(progress, f"⬇️ yt-dlp · **{link.filename}**\nPreparando...")
    path = await ytdlp_mod.download(
        link.url,
        cfg.download_dir,
        format_selector=ytdlp_format(link),
        on_progress=on_progress,
        max_filesize=MAX_TG_SIZE,
    )
    if cfg.ytdlp_reencode_h264:
        try:
            if ytdlp_mod.needs_h264_reencode(path):
                await safe_edit(
                    progress,
                    f"🎬 Re-codificando a H.264 · **{link.filename}**\n"
                    "Puede tardar (compatible QuickTime/Mac)…",
                )
            path = await ytdlp_mod.ensure_h264_aac(path)
        except ytdlp_mod.YtDlpError:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise
    size = os.path.getsize(path)
    if size > MAX_TG_SIZE:
        os.remove(path)
        raise FileTooLarge(size)
    return path


async def download_file(
    link: UnrestrictedLink,
    progress: Message,
    session: aiohttp.ClientSession | None = None,
    allow_html: bool = False,
    user_agent: str | None = None,
    name_from_response: bool = False,
) -> str:
    session = session or debrid_http
    os.makedirs(cfg.download_dir, exist_ok=True)
    last_edit = 0.0
    # con debrid_http manda la sesión (proxy y cabeceras propias del servicio)
    headers = (
        {"User-Agent": user_agent or DIRECT_USER_AGENT} if session is http else None
    )
    async with session.get(link.url, headers=headers) as resp:
        resp.raise_for_status()
        # si el servidor sirve HTML al descargar (anti-hotlink), abortar;
        # en modo wget (allow_html) se guarda lo que venga, como haría wget
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype in _PAGE_CONTENT_TYPES and not allow_html:
            raise DirectDownloadError(
                f"El servidor devolvió HTML en lugar del archivo ({ctype})"
            )
        filename = link.filename
        if name_from_response:
            # el sondeo puede haber visto otra cosa que el GET (HEAD ≠ GET en
            # algunos hosters): para nombrar el archivo manda quien trae los bytes
            filename = (
                filename_from_content_disposition(resp.headers.get("Content-Disposition"))
                or filename_from_url(str(resp.url))
                or link.filename
            )
            if "." not in filename:
                filename += extension_for(ctype)
        path = os.path.join(
            cfg.download_dir, f"{uuid.uuid4().hex[:8]}_{safe_filename(filename)}"
        )
        total = int(resp.headers.get("Content-Length") or 0) or (link.size or 0)
        if total > MAX_TG_SIZE:
            raise FileTooLarge(total)
        downloaded = 0
        with open(path, "wb") as fh:
            async for chunk in resp.content.iter_chunked(256 * 1024):
                fh.write(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_TG_SIZE:
                    raise FileTooLarge(downloaded)
                now = time.monotonic()
                if total and now - last_edit >= 3:
                    last_edit = now
                    pct = downloaded * 100 / total
                    await safe_edit(
                        progress,
                        f"⬇️ Descargando **{filename}**\n"
                        f"`[{progress_bar(pct)}]` {pct:.1f}% "
                        f"({human_size(downloaded)}/{human_size(total)})",
                    )
    return path


async def upload_progress(current: int, total: int, progress: Message, last_edit: list):
    now = time.monotonic()
    if not total or now - last_edit[0] < 3:
        return
    last_edit[0] = now
    pct = current * 100 / total
    await safe_edit(
        progress,
        f"⬆️ Subiendo...\n`[{progress_bar(pct)}]` {pct:.1f}% "
        f"({human_size(current)}/{human_size(total)})",
    )


async def upload_to_telegram(
    client: Client, chat_id: int, path: str, link: UnrestrictedLink, progress: Message
):
    await safe_edit(progress, "⬆️ Subiendo a Telegram...")
    caption = f"📄 **{link.filename}**\n🌐 {link.host}"
    last_edit = [0.0]
    kwargs = dict(caption=caption, progress=upload_progress, progress_args=(progress, last_edit))
    if path.lower().endswith((".mp4", ".mkv", ".mov", ".webm")):
        await client.send_video(chat_id, path, **kwargs)
    else:
        await client.send_document(chat_id, path, **kwargs)


# ---------------------------------------------------------------- arranque

def _masked_proxy(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        return f"{parsed.scheme}://***@{host}"
    return url


async def main():
    global http, debrid_http, providers
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
    http = aiohttp.ClientSession(timeout=timeout)
    if cfg.debrid_proxy:
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            raise SystemExit(
                "DEBRID_PROXY necesita el paquete aiohttp-socks: pip install aiohttp-socks"
            )
        proxy_url = cfg.debrid_proxy
        if proxy_url.startswith("socks5h://"):
            # la librería no conoce el esquema socks5h: es socks5 con DNS remoto
            connector = ProxyConnector.from_url("socks5://" + proxy_url[10:], rdns=True)
        else:
            connector = ProxyConnector.from_url(proxy_url)
        debrid_http = aiohttp.ClientSession(connector=connector, timeout=timeout)
        log.info("Proxy para servicios debrid: %s", _masked_proxy(cfg.debrid_proxy))
    else:
        debrid_http = http
    global link_proxy
    if cfg.link_proxy:
        base_url = cfg.link_proxy_url
        if not base_url:
            # sin URL pública configurada, usa la IP pública del servidor
            base_url = f"http://{await detect_public_ip(http)}:{cfg.link_proxy_port}"
        link_proxy = LinkProxy(debrid_http, base_url)
        await link_proxy.start("0.0.0.0", cfg.link_proxy_port)
        log.info(
            "Relay de enlaces activo en %s (puerto %d abierto hacia fuera, recuérdalo)",
            link_proxy.base_url,
            cfg.link_proxy_port,
        )

    providers = build_providers(cfg, debrid_http)
    for rule_host, slug in cfg.host_rules:
        if slug not in providers:
            log.warning(
                "HOST_RULES: la regla '%s:%s' apunta a un servicio no configurado; se ignora",
                rule_host,
                slug,
            )
    if cfg.ytdlp:
        if not ytdlp_mod.available():
            raise SystemExit(
                "YTDLP=true pero yt-dlp no está instalado. "
                "Instálalo con: pip install yt-dlp"
            )
        log.info(
            "yt-dlp activo (formato: %s, reencode_h264: %s)",
            cfg.ytdlp_format,
            cfg.ytdlp_reencode_h264,
        )
        if cfg.ytdlp_reencode_h264 and not ytdlp_mod.ffmpeg_available():
            log.warning(
                "YTDLP_REENCODE_H264=true pero no hay ffmpeg/ffprobe en PATH; "
                "la re-codificación fallará al descargar. Instala ffmpeg."
            )
    if cfg.dripfiles:
        log.info("DripFiles activo (API free, sin key)")
    if not providers and not cfg.ytdlp:
        raise SystemExit(
            "Configura al menos una API key de debrid "
            "(REALDEBRID_API_KEY, ALLDEBRID_API_KEY, …) o activa YTDLP=true"
        )
    await app.start()
    # menú de comandos de Telegram (autocompletado al escribir "/")
    await app.set_bot_commands(
        [
            BotCommand("service", "Elegir el servicio debrid activo"),
            BotCommand("wget", "Descargar una URL tal cual y subirla aquí"),
            BotCommand("torrents", "Gestionar tus torrents"),
            BotCommand("help", "Ayuda y ejemplos de uso"),
        ]
    )
    me = await app.get_me()
    services = ", ".join(p.name for p in providers.values()) or "(ninguno)"
    if cfg.ytdlp:
        services = f"{services}, yt-dlp" if providers else "yt-dlp"
    log.info("Bot @%s iniciado. Servicios: %s", me.username, services)
    await idle()
    await app.stop()
    if link_proxy:
        await link_proxy.stop()
    if debrid_http is not http:
        await debrid_http.close()
    await http.close()


if __name__ == "__main__":
    # El Client queda ligado al loop creado en el import; usar asyncio.run()
    # crearía un segundo loop y pyrogram fallaría con "attached to a different loop"
    app.loop.run_until_complete(main())
