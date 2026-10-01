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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote

import aiohttp
from aiohttp import web

from debrid import UnrestrictedLink

log = logging.getLogger("linkproxy")

TOKEN_TTL = 24 * 3600  # los enlaces del relay caducan a las 24 h
CHUNK = 256 * 1024

# cabeceras del debrid que se reenvían tal cual al cliente
_PASSTHROUGH = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type")


@dataclass(frozen=True)
class RelayTarget:
    """URL upstream y cabeceras necesarias para descargarla."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)


TargetResolver = Callable[[], Awaitable[RelayTarget]]


@dataclass
class _RelayEntry:
    link: UnrestrictedLink
    created_at: float
    session: aiohttp.ClientSession
    resolver: TargetResolver | None = None
    resolve_ttl: float = 0
    cached_target: RelayTarget | None = None
    cached_at: float = 0
    resolve_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def target(self) -> RelayTarget:
        if self.resolver is None:
            return RelayTarget(self.link.url)

        now = time.monotonic()
        if self.cached_target and now - self.cached_at < self.resolve_ttl:
            return self.cached_target

        # Un navegador suele hacer HEAD seguido de GET y los gestores abren
        # varios Range a la vez. Una sola extracción sirve a todo ese grupo.
        async with self.resolve_lock:
            now = time.monotonic()
            if self.cached_target and now - self.cached_at < self.resolve_ttl:
                return self.cached_target
            target = await self.resolver()
            self.cached_target = target
            self.cached_at = time.monotonic()
            return target


class LinkProxy:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self._links: dict[str, _RelayEntry] = {}
        self._runner: web.AppRunner | None = None

    def register(
        self,
        link: UnrestrictedLink,
        *,
        resolver: TargetResolver | None = None,
        session: aiohttp.ClientSession | None = None,
        resolve_ttl: float = 0,
    ) -> str:
        self._purge()
        token = secrets.token_urlsafe(16)
        self._links[token] = _RelayEntry(
            link=link,
            created_at=time.time(),
            session=session or self.session,
            resolver=resolver,
            resolve_ttl=resolve_ttl,
        )
        return f"{self.base_url}/dl/{token}"

    def _purge(self) -> None:
        cutoff = time.time() - TOKEN_TTL
        for token in [t for t, entry in self._links.items() if entry.created_at < cutoff]:
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
        if not entry or time.time() - entry.created_at > TOKEN_TTL:
            raise web.HTTPNotFound(text="Enlace caducado, pídelo de nuevo al bot.")
        link = entry.link

        try:
            target = await entry.target()
        except Exception:
            log.exception("No se pudo resolver el origen temporal para %s", link.filename)
            raise web.HTTPBadGateway(
                text="No se pudo preparar el enlace temporal. Pídelo de nuevo al bot."
            )

        # yt-dlp puede necesitar User-Agent, Referer u otras cabeceras del
        # extractor. Host/Content-Length/Range los controla esta petición.
        headers = {
            key: value
            for key, value in target.headers.items()
            if key.lower() not in ("host", "content-length", "range")
        }
        if "Range" in request.headers:  # reanudar / descarga por tramos
            headers["Range"] = request.headers["Range"]
        if "If-Range" in request.headers:
            headers["If-Range"] = request.headers["If-Range"]

        async with entry.session.get(target.url, headers=headers) as upstream:
            if upstream.status >= 400:
                log.warning("El origen respondió %s para %s", upstream.status, link.filename)
                return web.Response(
                    status=upstream.status, text=f"El servidor de origen respondió {upstream.status}"
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
            if request.method == "HEAD":
                await resp.write_eof()
                return resp
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
