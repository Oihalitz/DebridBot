"""Relay de descargas: sirve los enlaces debrid desde la IP del propio bot.

Los debrid ligan cada enlace generado a la IP que lo pidió; si el bot corre en
una VPS y el usuario abre el enlace desde su casa/móvil, el servicio ve IPs
distintas y puede banear la cuenta. Con LINK_PROXY el bot entrega URLs propias
(http://IP_DEL_BOT:PUERTO/dl/token) y descarga él mismo del debrid en streaming,
así el servicio solo ve una IP: la del bot (o la de DEBRID_PROXY si está activo).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from urllib.parse import quote

import aiohttp
from aiohttp import web

from debrid import UnrestrictedLink

log = logging.getLogger("linkproxy")

TOKEN_TTL = 24 * 3600  # los enlaces del relay caducan a las 24 h
CHUNK = 256 * 1024

# cabeceras del debrid que se reenvían tal cual al cliente
_PASSTHROUGH = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type")


class LinkProxy:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self._links: dict[str, tuple[UnrestrictedLink, float]] = {}
        self._runner: web.AppRunner | None = None

    def register(self, link: UnrestrictedLink) -> str:
        self._purge()
        token = secrets.token_urlsafe(16)
        self._links[token] = (link, time.time())
        return f"{self.base_url}/dl/{token}"

    def _purge(self) -> None:
        cutoff = time.time() - TOKEN_TTL
        for token in [t for t, (_, ts) in self._links.items() if ts < cutoff]:
            del self._links[token]

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_get("/dl/{token}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        entry = self._links.get(request.match_info["token"])
        if not entry or time.time() - entry[1] > TOKEN_TTL:
            raise web.HTTPNotFound(text="Enlace caducado, pídelo de nuevo al bot.")
        link = entry[0]

        headers = {}
        if "Range" in request.headers:  # reanudar / descarga por tramos
            headers["Range"] = request.headers["Range"]

        async with self.session.get(link.url, headers=headers) as upstream:
            if upstream.status >= 400:
                log.warning("El debrid respondió %s para %s", upstream.status, link.filename)
                return web.Response(
                    status=upstream.status, text=f"El servicio debrid respondió {upstream.status}"
                )
            resp = web.StreamResponse(status=upstream.status)
            for header in _PASSTHROUGH:
                if header in upstream.headers:
                    resp.headers[header] = upstream.headers[header]
            ascii_name = link.filename.encode("ascii", "replace").decode()
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(link.filename)}"
            )
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                pass  # el cliente cortó la descarga; nada que hacer
            return resp


async def detect_public_ip(session: aiohttp.ClientSession) -> str:
    async with session.get("https://api.ipify.org") as resp:
        return (await resp.text()).strip()
