"""Cliente de la API pública free de DripFiles (https://dripfiles.com/api/v1/free).

Subida (sin API key):
  1. POST /uploads              → upload_id + upload_token
  2. POST /uploads/{id}/files   → trozos multipart (files[]) + Content-Range
  3. POST /uploads/{id}/complete
  4. GET  /uploads/{id}         → poll hasta status=ready → url de descarga

Descarga (`fetch_share`): la API de estado exige el upload_token, que solo tiene
quien subió, así que para un envío ajeno se lee la página pública del envío y se
sacan sus enlaces `handler/file`, que sí son descargables por cualquiera.

Límites free (aprox.): 2 GB por archivo, 50 archivos, caduca en 2 días.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, unquote, urlparse

import aiohttp

log = logging.getLogger("bot.dripfiles")

API_BASE = "https://dripfiles.com/api/v1/free"
DEFAULT_CHUNK = 1024 * 1024  # 1 MiB (recommended_chunk_bytes)
MAX_SIZE = 2 * 1024**3
READY_POLL_ATTEMPTS = 40
READY_POLL_DELAY = 0.75


class DripFilesError(Exception):
    """Error al crear, subir o finalizar un envío en DripFiles."""


def _api_message(data: dict | None, fallback: str) -> str:
    if not isinstance(data, dict):
        return fallback
    return str(data.get("message") or data.get("error") or fallback)


async def _read_json(resp: aiohttp.ClientResponse) -> dict:
    try:
        data = await resp.json(content_type=None)
    except Exception as exc:
        text = await resp.text()
        raise DripFilesError(f"HTTP {resp.status}: respuesta no JSON ({text[:200]})") from exc
    if not isinstance(data, dict):
        raise DripFilesError(f"HTTP {resp.status}: JSON inesperado")
    return data


async def create_upload(
    session: aiohttp.ClientSession,
    *,
    message: str | None = None,
) -> dict:
    """Crea un envío free. `message` = descripción del envío (campo Droppy)."""
    body: dict = {}
    if message and message.strip():
        body["message"] = message.strip()
    async with session.post(f"{API_BASE}/uploads", json=body) as resp:
        data = await _read_json(resp)
        if resp.status >= 400 or not data.get("ok"):
            raise DripFilesError(_api_message(data, f"no se pudo crear el envío (HTTP {resp.status})"))
        if not data.get("upload_id") or not data.get("upload_token"):
            raise DripFilesError("la API no devolvió upload_id/upload_token")
        return data


async def _upload_chunk(
    session: aiohttp.ClientSession,
    *,
    upload_id: str,
    upload_token: str,
    file_uid: str,
    filename: str,
    chunk: bytes,
    start: int,
    total: int,
) -> dict:
    end = start + len(chunk) - 1
    form = aiohttp.FormData()
    form.add_field("upload_id", upload_id)
    form.add_field("file_uid", file_uid)
    form.add_field("original_path", filename)
    # el front y la API free esperan el campo blueimp `files[]`
    form.add_field(
        "files[]",
        chunk,
        filename=filename,
        content_type="application/octet-stream",
    )
    headers = {
        "X-Upload-Token": upload_token,
        "X-File-Uid": file_uid,
        "X-File-Name": quote(filename),
        "Content-Range": f"bytes {start}-{end}/{total}",
    }
    url = f"{API_BASE}/uploads/{upload_id}/files"
    async with session.post(url, data=form, headers=headers) as resp:
        data = await _read_json(resp)
        if resp.status >= 400 or not data.get("ok"):
            raise DripFilesError(
                _api_message(data, f"error subiendo trozo {start}-{end} (HTTP {resp.status})")
            )
        return data


async def complete_upload(
    session: aiohttp.ClientSession,
    upload_id: str,
    upload_token: str,
    *,
    message: str | None = None,
) -> dict:
    headers = {"X-Upload-Token": upload_token}
    body: dict = {}
    # por si el backend solo guarda message al completar (además de al create)
    if message and message.strip():
        body["message"] = message.strip()
    async with session.post(
        f"{API_BASE}/uploads/{upload_id}/complete", json=body, headers=headers
    ) as resp:
        data = await _read_json(resp)
        if resp.status >= 400 or not data.get("ok"):
            raise DripFilesError(_api_message(data, f"error al completar (HTTP {resp.status})"))
        return data


async def get_status(
    session: aiohttp.ClientSession, upload_id: str, upload_token: str | None = None
) -> dict:
    headers = {}
    if upload_token:
        headers["X-Upload-Token"] = upload_token
    async with session.get(
        f"{API_BASE}/uploads/{upload_id}", headers=headers or None
    ) as resp:
        data = await _read_json(resp)
        if resp.status >= 400 or not data.get("ok"):
            raise DripFilesError(_api_message(data, f"error consultando estado (HTTP {resp.status})"))
        return data


async def wait_ready(
    session: aiohttp.ClientSession,
    upload_id: str,
    upload_token: str,
    *,
    attempts: int = READY_POLL_ATTEMPTS,
    delay: float = READY_POLL_DELAY,
) -> dict:
    last: dict | None = None
    for _ in range(attempts):
        last = await get_status(session, upload_id, upload_token)
        status = (last.get("status") or "").lower()
        if status == "ready":
            return last
        if status in ("error", "failed", "expired"):
            raise DripFilesError(
                _api_message(last, f"el envío quedó en estado {status}")
            )
        await asyncio.sleep(delay)
    raise DripFilesError(
        _api_message(last, "timeout esperando a que DripFiles finalice el envío")
    )


async def upload_path(
    session: aiohttp.ClientSession,
    path: str,
    filename: str | None = None,
    *,
    message: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    chunk_size: int | None = None,
) -> dict:
    """Sube un archivo local y devuelve el dict de estado listo (incluye `url`).

    `message`: descripción/mensaje del envío (mismo campo que Droppy en web).
    Se envía en create y en complete por compatibilidad con el backend.
    """
    if not os.path.isfile(path):
        raise DripFilesError(f"no existe el archivo: {path}")
    total = os.path.getsize(path)
    if total <= 0:
        raise DripFilesError("el archivo está vacío")
    if total > MAX_SIZE:
        raise DripFilesError(f"supera el límite free de DripFiles ({MAX_SIZE} bytes)")

    name = filename or os.path.basename(path) or f"file_{uuid.uuid4().hex[:8]}"
    msg = message.strip() if message and message.strip() else None
    meta = await create_upload(session, message=msg)
    upload_id = meta["upload_id"]
    token = meta["upload_token"]
    chunk = int(chunk_size or meta.get("chunk_size") or DEFAULT_CHUNK)
    if chunk < 64 * 1024:
        chunk = DEFAULT_CHUNK
    file_uid = str(uuid.uuid4())

    log.info(
        "DripFiles: subiendo %s (%s bytes) como %s / %s%s",
        name,
        total,
        upload_id,
        file_uid,
        f" msg={msg!r}" if msg else "",
    )

    sent = 0
    with open(path, "rb") as fh:
        while sent < total:
            data = fh.read(chunk)
            if not data:
                break
            await _upload_chunk(
                session,
                upload_id=upload_id,
                upload_token=token,
                file_uid=file_uid,
                filename=name,
                chunk=data,
                start=sent,
                total=total,
            )
            sent += len(data)
            if on_progress:
                try:
                    on_progress(sent, total)
                except Exception:
                    log.debug("progress dripfiles falló", exc_info=True)

    await complete_upload(session, upload_id, token, message=msg)
    status = await wait_ready(session, upload_id, token)
    url = status.get("url") or f"https://dripfiles.com/{upload_id}"
    status["url"] = url
    log.info("DripFiles listo: %s", url)
    return status


# ------------------------------------------------------------------ descarga

SHARE_HOSTS = ("dripfiles.com",)
# rutas del sitio que no son envíos (evita tratarlas como enlaces de descarga)
_RESERVED_PATHS = frozenset({
    "api", "handler", "page", "assets", "login", "logout", "register",
    "signup", "account", "admin", "premium", "terms", "privacy", "faq",
    "hola", "index.php", "robots.txt", "favicon.ico",
})
_SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
# la web responde el archivo (o un zip con todo) cuando el User-Agent no parece
# un navegador; con UA de navegador devuelve el HTML con los enlaces por archivo
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_FILE_LINK_RE = re.compile(
    r"handler/file\?file_id=(?P<id>\d+)&(?:amp;)?file_secret=(?P<secret>[0-9a-zA-Z]+)",
    re.I,
)
_NAME_RE = re.compile(r'<span class="name">(.*?)</span>', re.I | re.S)
# ventana de HTML tras un enlace donde buscar su nombre (el bloque de la tarjeta)
_NAME_WINDOW = 4000


@dataclass(frozen=True)
class ShareFile:
    """Un archivo descargable dentro de un envío público."""

    name: str
    url: str


@dataclass(frozen=True)
class Share:
    """Envío público de DripFiles con sus archivos."""

    upload_id: str
    url: str
    files: list[ShareFile]


def is_share_url(url: str) -> bool:
    """True si la URL es un envío de DripFiles (dripfiles.com/XXXXXXXX)."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not any(host == h or host.endswith("." + h) for h in SHARE_HOSTS):
        return False
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts or len(parts) > 2:
        return False
    if parts[0].lower() in _RESERVED_PATHS:
        return False
    return all(_SHARE_ID_RE.match(p) for p in parts)


def share_id(url: str) -> str:
    parts = [p for p in (urlparse(url).path or "").split("/") if p]
    return parts[0] if parts else url


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_share_html(html: str, base_url: str) -> list[ShareFile]:
    """Saca los archivos (enlace + nombre) de la página pública de un envío."""
    origin = _origin(base_url)
    # cada tarjeta repite su enlace (vista previa, botón…): nos quedamos con la
    # primera aparición de cada file_id y mantenemos el orden de la página
    first: dict[str, tuple[str, int]] = {}
    for match in _FILE_LINK_RE.finditer(html):
        first.setdefault(match.group("id"), (match.group("secret"), match.start()))

    entries = sorted(first.items(), key=lambda item: item[1][1])
    files: list[ShareFile] = []
    for index, (file_id, (secret, start)) in enumerate(entries):
        # el nombre vive dentro del bloque de la tarjeta, entre este enlace y el siguiente
        end = entries[index + 1][1][1] if index + 1 < len(entries) else len(html)
        block = html[start : min(end, start + _NAME_WINDOW)]
        name_match = _NAME_RE.search(block)
        name = (
            html_mod.unescape(name_match.group(1)).strip() if name_match else ""
        ) or f"archivo_{file_id}"
        files.append(
            ShareFile(
                name=name,
                url=(
                    f"{origin}/handler/file?file_id={file_id}"
                    f"&file_secret={secret}&download=true"
                ),
            )
        )
    return files


async def fetch_share(
    session: aiohttp.ClientSession, url: str, *, timeout: float = 45
) -> Share:
    """Lee un envío público y devuelve sus archivos con enlaces directos."""
    url = url.strip()
    headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html,*/*"}
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=15, sock_read=30)
    try:
        async with session.get(
            url, headers=headers, allow_redirects=True, timeout=client_timeout
        ) as resp:
            if resp.status == 404:
                raise DripFilesError("el envío no existe o ha caducado")
            if resp.status >= 400:
                raise DripFilesError(f"la página del envío respondió HTTP {resp.status}")
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if "html" not in ctype:
                # el servidor sirvió el archivo/zip directamente: no hay nada que parsear
                name = _filename_from_disposition(
                    resp.headers.get("Content-Disposition")
                ) or f"{share_id(url)}.zip"
                return Share(
                    upload_id=share_id(url),
                    url=url,
                    files=[ShareFile(name=name, url=str(resp.url))],
                )
            body = await resp.text(errors="replace")
            final_url = str(resp.url)
    except aiohttp.ClientError as exc:
        raise DripFilesError(f"no se pudo abrir el envío: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise DripFilesError("timeout abriendo el envío") from exc

    files = parse_share_html(body, final_url)
    if not files:
        raise DripFilesError(
            "no encontré archivos en ese envío (¿caducado, vacío o con contraseña?)"
        )
    log.info("DripFiles: %s archivo(s) en %s", len(files), url)
    return Share(upload_id=share_id(url), url=url, files=files)


_CD_FILENAME_RE = re.compile(
    r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\s]+)|filename\s*=\s*\"([^\"]+)\"",
    re.I,
)


def _filename_from_disposition(header: str | None) -> str | None:
    match = _CD_FILENAME_RE.search(header or "")
    if not match:
        return None
    raw = next(group for group in match.groups() if group)
    return unquote(raw).strip() or None
