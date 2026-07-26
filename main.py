import asyncio
import logging
import os
import re
import time
import uuid
from urllib.parse import urlparse

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
from debrid import DebridError, DebridProvider, UnrestrictedLink, build_providers
from linkproxy import LinkProxy, detect_public_ip
from filecrypt import (
    CaptchaRequired,
    FilecryptError,
    PasswordRequired,
    get_folder_links,
    is_filecrypt,
)

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

# asyncio solo guarda referencias débiles a las tareas: sin esto el GC puede
# matar un monitor_torrent/transfer en marcha y el mensaje se queda congelado
background_tasks: set[asyncio.Task] = set()

MAX_TG_SIZE = 2 * 1024**3  # límite de subida para bots (2 GB)
MAX_TORRENT_FILES = 25
STATUS_EMOJI = {"queued": "🕓", "downloading": "⬇️", "ready": "✅", "error": "❌"}

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


def remember(link: UnrestrictedLink, provider: DebridProvider) -> str:
    if len(pending) > 500:
        for key in list(pending)[:100]:
            pending.pop(key, None)
    token = uuid.uuid4().hex[:12]
    pending[token] = (link, provider.name)
    return token


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


async def unrestrict_url(user_id: int, url: str) -> tuple[UnrestrictedLink, DebridProvider]:
    """Prueba los servicios en orden y devuelve el primero que desbloquee el enlace."""
    errors: list[str] = []
    for provider in providers_for_url(user_id, url):
        try:
            return await provider.unrestrict(url), provider
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
    lines.append(f"⚙️ **Servicio:** {provider_name}")
    return "\n".join(lines)


def action_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Enlace", callback_data=f"link:{token}"),
                InlineKeyboardButton("📤 Archivo", callback_data=f"file:{token}"),
            ]
        ]
    )


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

HELP_TEXT = (
    "👋 **Bot Debrid**\n\n"
    "Envíame:\n"
    "• Un **enlace** de un hoster → lo desbloqueo y eliges: enlace directo o que te suba el archivo.\n"
    "• Un **paste de controlc.com** → extraigo sus enlaces y los desbloqueo todos.\n"
    "• Una **carpeta de filecrypt.cc** → extraigo los enlaces y los desbloqueo. "
    "Si pide contraseña, mándala después del enlace. Con captcha se abre Chrome + uBlock.\n"
    "• Un **magnet** o un archivo **.torrent** → lo descargo en tu servicio debrid "
    "y cuando termine te doy los archivos.\n\n"
    "**Comandos:**\n"
    "/service — elegir servicio debrid\n"
    "/torrents — gestionar tus torrents: ver progreso, obtener enlaces, "
    "reiniciar o eliminar\n"
    "/help — esta ayuda"
)


@app.on_message(filters.command(["start", "help"]) & filters.private & auth)
async def cmd_start(_, message: Message):
    await message.reply_text(HELP_TEXT)


@app.on_message(filters.command("service") & filters.private & auth)
async def cmd_service(_, message: Message):
    await message.reply_text(
        "⚙️ Elige el servicio debrid activo:",
        reply_markup=service_keyboard(message.from_user.id),
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
    provider = provider_for(message.from_user.id)

    if text.startswith("magnet:"):
        await start_torrent(provider, message, magnet=message.text.strip())
        return

    if not is_url(text):
        await message.reply_text("Envíame un enlace, un magnet o un archivo `.torrent` ♿️")
        return

    if urlparse(text).netloc.endswith("controlc.com"):
        await handle_paste(provider, message, text)
        return

    if is_filecrypt(text):
        # segunda palabra opcional = contraseña de la carpeta
        password = parts[1] if len(parts) > 1 else None
        await handle_filecrypt(provider, message, text, password)
        return

    url = normalize_mirrors(text)
    first = providers_for_url(message.from_user.id, url)[0]
    status = await message.reply_text(f"🔎 Desbloqueando con {first.name}...")
    try:
        link, used = await unrestrict_url(message.from_user.id, url)
    except DebridError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    token = remember(link, used)
    await safe_edit(status, describe(link, used.name), reply_markup=action_keyboard(token))


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
            link, used = await unrestrict_url(message.from_user.id, normalize_mirrors(raw_link))
        except DebridError as exc:
            await message.reply_text(f"❌ `{raw_link}`\n{exc}")
            continue
        token = remember(link, used)
        await message.reply_text(describe(link, used.name), reply_markup=action_keyboard(token))
        unlocked += 1
    summary = f"{emoji} {label}: {unlocked}/{total} enlace(s) desbloqueado(s)."
    if len(links) > MAX_CONTAINER_LINKS:
        summary += f" (Había {len(links)}, procesados los primeros {MAX_CONTAINER_LINKS}.)"
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

    if action in ("link", "file"):
        entry = pending.get(value)
        if not entry:
            await query.answer("Este enlace ha caducado, envíalo de nuevo.", show_alert=True)
            return
        link, provider_name = entry

        if action == "link":
            await query.answer()
            await safe_edit(
                query.message,
                describe(link, provider_name) + f"\n\n🔗 [Descargar]({public_url(link)})",
            )
            return

        if link.size and link.size > MAX_TG_SIZE:
            await query.answer()
            await safe_edit(
                query.message,
                describe(link, provider_name)
                + f"\n\n❌ Supera el límite de 2 GB de Telegram.\n🔗 [Descargar]({public_url(link)})",
            )
            return

        await query.answer("Preparando la transferencia...")
        try:
            await query.message.edit_reply_markup(None)
        except Exception:
            pass
        spawn(transfer(client, query.message, link))


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

async def transfer(client: Client, message: Message, link: UnrestrictedLink):
    progress = await message.reply_text("⬇️ Descargando...")
    path = None
    try:
        path = await download_file(link, progress)
        await upload_to_telegram(client, message.chat.id, path, link, progress)
        await progress.delete()
    except FileTooLarge as exc:
        await safe_edit(
            progress,
            f"❌ El archivo pesa {human_size(exc.size)} y supera el límite de 2 GB.\n"
            f"🔗 [Descargar]({public_url(link)})",
        )
    except Exception as exc:
        log.exception("Error transfiriendo %s", link.filename)
        await safe_edit(progress, f"❌ Error en la transferencia: {exc}")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def download_file(link: UnrestrictedLink, progress: Message) -> str:
    os.makedirs(cfg.download_dir, exist_ok=True)
    path = os.path.join(cfg.download_dir, f"{uuid.uuid4().hex[:8]}_{safe_filename(link.filename)}")
    last_edit = 0.0
    async with debrid_http.get(link.url) as resp:
        resp.raise_for_status()
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
                        f"⬇️ Descargando **{link.filename}**\n"
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
    if not providers:
        raise SystemExit(
            "Configura al menos una API key: REALDEBRID_API_KEY, ALLDEBRID_API_KEY o TORBOX_API_KEY"
        )
    await app.start()
    # menú de comandos de Telegram (autocompletado al escribir "/")
    await app.set_bot_commands(
        [
            BotCommand("service", "Elegir el servicio debrid activo"),
            BotCommand("torrents", "Gestionar tus torrents"),
            BotCommand("help", "Ayuda y ejemplos de uso"),
        ]
    )
    me = await app.get_me()
    log.info(
        "Bot @%s iniciado. Servicios: %s",
        me.username,
        ", ".join(p.name for p in providers.values()),
    )
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
