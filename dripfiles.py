"""Cliente de la API pública free de DripFiles (https://dripfiles.com/api/v1/free).

Sin API key. Flujo:
  1. POST /uploads              → upload_id + upload_token
  2. POST /uploads/{id}/files   → trozos multipart (files[]) + Content-Range
  3. POST /uploads/{id}/complete
  4. GET  /uploads/{id}         → poll hasta status=ready → url de descarga

Límites free (aprox.): 2 GB total, 50 archivos, caduca en 2 días, 20 subidas/IP/hora.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Callable
from urllib.parse import quote

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
