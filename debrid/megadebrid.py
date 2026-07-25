from __future__ import annotations

from urllib.parse import unquote, urlparse

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://www.mega-debrid.eu/api.php"

_READY = {"complete", "completed", "finished", "ready", "seeding", "uploaded"}
_QUEUED = {"pending", "queued", "new", "waiting", "added"}
_ERROR = {"error", "dead", "failed"}


class MegaDebrid(DebridProvider):
    name = "Mega-Debrid"
    slug = "megadebrid"
    supports_delete = False  # la API no expone borrado de torrents

    def __init__(
        self,
        api_key: str,
        session: aiohttp.ClientSession,
        login: str | None = None,
        password: str | None = None,
    ):
        super().__init__(api_key, session)
        self.login = login
        self.password = password
        # una API key de aplicación vale directamente como token; con
        # login/password el token se pide en la primera petición
        self._token: str | None = api_key or None

    async def _connect(self) -> str:
        params = {"action": "connectUser", "login": self.login, "password": self.password}
        async with self.session.get(BASE, params=params) as resp:
            payload = await resp.json(content_type=None)
        token = isinstance(payload, dict) and payload.get("token")
        if not token or payload.get("response_code") != "ok":
            detail = payload.get("response_text") if isinstance(payload, dict) else None
            raise DebridError(f"{self.name}: login fallido ({detail or 'sin detalle'})")
        self._token = token
        return token

    async def _request(self, action: str, *, data=None, retry: bool = True) -> dict:
        if not self._token:
            await self._connect()
        params = {"action": action, "token": self._token}
        method = "POST" if data is not None else "GET"
        async with self.session.request(method, BASE, params=params, data=data) as resp:
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise DebridError(f"{self.name}: respuesta inesperada de la API")
        if payload.get("response_code") != "ok":
            detail = payload.get("response_text") or payload.get("response_code") or "error"
            # el token de login caduca al volver a iniciar sesión: reintenta una vez
            if "token" in str(detail).lower() and self.login and retry:
                await self._connect()
                return await self._request(action, data=data, retry=False)
            raise DebridError(f"{self.name}: {detail}")
        return payload

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        payload = await self._request("getLink", data={"link": link})
        url = payload.get("debridLink")
        if not url:
            raise DebridError(f"{self.name}: el enlace no devolvió ninguna descarga")
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "archivo"
        return UnrestrictedLink(
            url=url,
            filename=filename,
            host=urlparse(link).hostname or self.slug,
            size=None,
        )

    async def add_magnet(self, magnet: str) -> str:
        payload = await self._request("uploadTorrent", data={"magnet": magnet})
        return str((payload.get("newTorrent") or {})["hash"])

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field("file", raw, filename=filename, content_type="application/x-bittorrent")
        payload = await self._request("uploadTorrent", data=form)
        return str((payload.get("newTorrent") or {})["hash"])

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        payload = await self._request("getTorrent", data={"hash": torrent_id})
        return self._to_info(payload.get("status") or {}, torrent_id)

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        payload = await self._request("getTorrent", data={"hash": torrent_id})
        ub_link = (payload.get("status") or {}).get("ub_link")
        if not ub_link:
            return []
        # el torrent termina en un enlace de hoster que hay que desbloquear
        try:
            return [await self.unrestrict(ub_link)]
        except DebridError:
            filename = unquote(urlparse(ub_link).path.rsplit("/", 1)[-1]) or "archivo"
            return [UnrestrictedLink(url=ub_link, filename=filename, host=self.slug)]

    async def list_torrents(self) -> list[TorrentInfo]:
        payload = await self._request("getTorrents")
        torrents = payload.get("torrents") or []
        return [
            self._to_info(t, str(t.get("hash") or t.get("id") or t.get("name", "?")))
            for t in torrents[:20]
        ]

    async def delete_torrent(self, torrent_id: str) -> None:
        raise DebridError(f"{self.name}: la API no permite borrar torrents")

    def _to_info(self, torrent: dict, torrent_id: str) -> TorrentInfo:
        raw_status = str(torrent.get("status") or "").lower()
        progress = float(torrent.get("progress") or 0)
        if raw_status in _READY or progress >= 100:
            status = "ready"
        elif raw_status in _ERROR:
            status = "error"
        elif raw_status in _QUEUED:
            status = "queued"
        else:
            status = "downloading"
        return TorrentInfo(
            id=torrent_id,
            name=torrent.get("name") or "torrent",
            status=status,
            progress=100.0 if status == "ready" else progress,
            detail=raw_status,
        )
