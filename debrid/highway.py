from __future__ import annotations

import asyncio

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://high-way.me"

# status numérico tipo Transmission según su documentación:
# 0 parado, 1-2 comprobando, 3-4 descargando, 5-6 seedeando, 7 archivado
_STATUS_MAP = {
    0: ("queued", "parado"),
    1: ("queued", "comprobando"),
    2: ("queued", "comprobando"),
    3: ("downloading", "descargando"),
    4: ("downloading", "descargando"),
    5: ("ready", "seedeando"),
    6: ("ready", "seedeando"),
    7: ("ready", "archivado"),
}

_LOGIN_HINTS = ("login", "eingeloggt", "session", "anmelden")

# espera máxima a que high-way termine de cachear un enlace en sus servidores
_CACHE_WAIT_SECONDS = 120


class Highway(DebridProvider):
    name = "High-Way"
    slug = "highway"
    supports_delete = False  # la API solo permite listar y descargar torrents

    def __init__(self, api_key: str, session: aiohttp.ClientSession, login: str, password: str):
        super().__init__(api_key, session)
        self.login = login
        self.password = password
        self._logged_in = False

    async def _connect(self) -> None:
        # el login se guarda como cookie PHPSESSID en el jar de la sesión
        async with self.session.post(
            f"{BASE}/apiV2/login", data={"user": self.login, "pass": self.password}
        ) as resp:
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict) or not payload.get("loggedin"):
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise DebridError(f"{self.name}: login fallido ({detail or 'sin detalle'})")
        self._logged_in = True

    async def _get(self, path: str, params: dict, *, retry: bool = True) -> dict:
        if not self._logged_in:
            await self._connect()
        async with self.session.get(f"{BASE}{path}", params=params) as resp:
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise DebridError(f"{self.name}: respuesta inesperada de la API")
        error = payload.get("error")
        if error and retry and any(hint in str(error).lower() for hint in _LOGIN_HINTS):
            # la sesión caducó: reloguea y reintenta una vez
            self._logged_in = False
            return await self._get(path, params, retry=False)
        if error:
            raise DebridError(f"{self.name}: {error}")
        return payload

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        payload = await self._get("/load.php", {"link": link, "json": "1"})
        download = payload.get("download")
        if not download:
            raise DebridError(f"{self.name}: el enlace no devolvió ninguna descarga")
        await self._wait_for_cache(payload)
        size = payload.get("size")
        return UnrestrictedLink(
            url=download,
            filename=payload.get("name") or "archivo",
            host=self.slug,
            size=int(size) if size and int(size) > 0 else None,
        )

    async def _wait_for_cache(self, payload: dict) -> None:
        # cacheStatus "s" = listo; "d"/"w" = aún copiando el archivo a sus servidores
        cache_url = payload.get("cache")
        if not cache_url or payload.get("cacheStatus") == "s":
            return
        waited = 0
        while waited < _CACHE_WAIT_SECONDS:
            async with self.session.get(cache_url) as resp:
                status = await resp.json(content_type=None)
            if not isinstance(status, dict) or status.get("cacheStatus") == "s":
                return
            delay = min(int(status.get("retry_in_seconds") or 5), 10)
            await asyncio.sleep(max(delay, 2))
            waited += max(delay, 2)
        raise DebridError(
            f"{self.name}: el enlace sigue preparándose en sus servidores, prueba en un rato"
        )

    async def add_magnet(self, magnet: str) -> str:
        raise DebridError(f"{self.name}: su API no permite añadir torrents (hazlo desde la web)")

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        raise DebridError(f"{self.name}: su API no permite añadir torrents (hazlo desde la web)")

    async def _torrents(self) -> list[dict]:
        payload = await self._get(
            "/torrent.php", {"action": "list", "json": "1", "order": "1", "suche": ""}
        )
        torrents = (payload.get("arguments") or {}).get("torrents")
        if torrents is None:
            # sin "arguments" lo más probable es sesión caducada: reintento tras login
            self._logged_in = False
            payload = await self._get(
                "/torrent.php", {"action": "list", "json": "1", "order": "1", "suche": ""}
            )
            torrents = (payload.get("arguments") or {}).get("torrents") or []
        return torrents

    async def _find_torrent(self, torrent_id: str) -> dict:
        for torrent in await self._torrents():
            if str(torrent.get("id")) == str(torrent_id):
                return torrent
        raise DebridError(f"{self.name}: torrent no encontrado")

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        return self._to_info(await self._find_torrent(torrent_id))

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        torrent = await self._find_torrent(torrent_id)
        if not torrent.get("link"):
            return []
        size = torrent.get("totalSize")
        return [
            UnrestrictedLink(
                url=torrent["link"],
                filename=torrent.get("name") or "torrent",
                host=self.slug,
                size=int(size) if size and int(size) > 0 else None,
            )
        ]

    async def list_torrents(self) -> list[TorrentInfo]:
        return [self._to_info(t) for t in (await self._torrents())[:100]]

    async def delete_torrent(self, torrent_id: str) -> None:
        raise DebridError(f"{self.name}: la API no permite borrar torrents")

    def _to_info(self, torrent: dict) -> TorrentInfo:
        progress = float(torrent.get("percentDone") or 0)
        status, detail = _STATUS_MAP.get(int(torrent.get("status") or 0), ("downloading", ""))
        if progress >= 100:
            status = "ready"
        return TorrentInfo(
            id=str(torrent.get("id", "")),
            name=torrent.get("name") or "torrent",
            status=status,
            progress=100.0 if status == "ready" else progress,
            detail=detail,
        )
