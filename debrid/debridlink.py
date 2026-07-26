from __future__ import annotations

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://debrid-link.com/api/v2"

# status numérico del seedbox: 0 paused, 1 queued, 2 verification,
# 4 downloading, 8 seeding, 100 finished
_STATUS_MAP = {
    0: ("queued", "pausado"),
    1: ("queued", "en cola"),
    2: ("queued", "verificando"),
    4: ("downloading", "descargando"),
    8: ("ready", "seedeando"),
    100: ("ready", "completado"),
}


class DebridLink(DebridProvider):
    name = "Debrid-Link"
    slug = "debridlink"

    async def _request(self, method: str, path: str, *, params: dict | None = None, data=None):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with self.session.request(
            method, f"{BASE}{path}", params=params, data=data, headers=headers
        ) as resp:
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise DebridError(f"{self.name}: respuesta inesperada de la API")
        if not payload.get("success"):
            detail = payload.get("error_description") or payload.get("error") or "error desconocido"
            raise DebridError(f"{self.name}: {detail}")
        return payload.get("value")

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        value = await self._request("POST", "/downloader/add", data={"url": link})
        # un enlace de carpeta puede devolver una lista de links en vez de uno
        if isinstance(value, list):
            if not value:
                raise DebridError(f"{self.name}: el enlace no devolvió ninguna descarga")
            value = value[0]
        return UnrestrictedLink(
            url=value["downloadUrl"],
            filename=value.get("name") or "archivo",
            host=value.get("host") or self.slug,
            size=value.get("size") or None,
        )

    async def add_magnet(self, magnet: str) -> str:
        value = await self._request("POST", "/seedbox/add", data={"url": magnet})
        return str(value["id"])

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field("file", raw, filename=filename, content_type="application/x-bittorrent")
        value = await self._request("POST", "/seedbox/add", data=form)
        return str(value["id"])

    async def _find_torrent(self, torrent_id: str) -> dict:
        value = await self._request("GET", "/seedbox/list", params={"ids": torrent_id})
        for torrent in value or []:
            if str(torrent.get("id")) == str(torrent_id):
                return torrent
        raise DebridError(f"{self.name}: torrent no encontrado")

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        return self._to_info(await self._find_torrent(torrent_id))

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        torrent = await self._find_torrent(torrent_id)
        return [
            UnrestrictedLink(
                url=file["downloadUrl"],
                filename=file.get("name") or "archivo",
                host=self.slug,
                size=file.get("size") or None,
            )
            for file in torrent.get("files") or []
            if file.get("downloadUrl") and float(file.get("downloadPercent") or 0) >= 100
        ]

    async def list_torrents(self) -> list[TorrentInfo]:
        value = await self._request("GET", "/seedbox/list", params={"perPage": 100})
        return [self._to_info(torrent) for torrent in value or []]

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._request("DELETE", f"/seedbox/{torrent_id}/remove")

    def _to_info(self, torrent: dict) -> TorrentInfo:
        progress = float(torrent.get("downloadPercent") or 0)
        status, detail = _STATUS_MAP.get(torrent.get("status"), ("downloading", ""))
        if progress >= 100:
            status = "ready"
        if torrent.get("error"):
            status, detail = "error", str(torrent["error"])
        return TorrentInfo(
            id=str(torrent["id"]),
            name=torrent.get("name") or "torrent",
            status=status,
            progress=100.0 if status == "ready" else progress,
            detail=detail,
        )
