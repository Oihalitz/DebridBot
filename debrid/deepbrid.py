from __future__ import annotations

import re

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://www.deepbrid.com/api/v1"

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _parse_size(text) -> int | None:
    # la API devuelve tamaños humanos tipo "1.50 GB"
    match = re.match(r"([\d.]+)\s*([KMGT]?B)", str(text or ""), re.I)
    if not match:
        return None
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])


class Deepbrid(DebridProvider):
    name = "Deepbrid"
    slug = "deepbrid"
    supports_delete = False  # la API no expone borrado de torrents

    async def _request(self, method: str, path: str, *, params: dict | None = None, data=None):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with self.session.request(
            method, f"{BASE}{path}", params=params, data=data, headers=headers
        ) as resp:
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise DebridError(f"{self.name}: respuesta inesperada de la API")
        # las respuestas correctas llevan "error": 0; el listado de torrents no lleva error
        if payload.get("error"):
            raise DebridError(f"{self.name}: {payload.get('message', 'error desconocido')}")
        return payload

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        payload = await self._request("POST", "/generate/link", data={"link": link})
        if not payload.get("link"):
            raise DebridError(f"{self.name}: el enlace no devolvió ninguna descarga")
        return UnrestrictedLink(
            url=payload["link"],
            filename=payload.get("filename") or "archivo",
            host=payload.get("hoster") or self.slug,
            size=_parse_size(payload.get("size")),
        )

    async def add_magnet(self, magnet: str) -> str:
        payload = await self._request("POST", "/torrents/add", data={"magnet": magnet})
        return self._new_torrent_id(payload)

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field(
            "torrent_file", raw, filename=filename, content_type="application/x-bittorrent"
        )
        payload = await self._request("POST", "/torrents/add", data=form)
        return self._new_torrent_id(payload)

    def _new_torrent_id(self, payload: dict) -> str:
        torrent_id = payload.get("id") or payload.get("torrent_id") or (payload.get("data") or {}).get("id")
        if not torrent_id:
            raise DebridError(f"{self.name}: la API no devolvió el id del torrent")
        return str(torrent_id)

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        payload = await self._request("GET", "/torrents/info", params={"id": torrent_id})
        return self._to_info(payload)

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        payload = await self._request("GET", "/torrents/info", params={"id": torrent_id})
        filename = payload.get("filename") or "archivo"
        links = payload.get("links") or []
        return [
            UnrestrictedLink(
                url=url,
                filename=filename if len(links) == 1 else f"{filename} ({i})",
                host=self.slug,
                size=None,
            )
            for i, url in enumerate(links, 1)
        ]

    async def list_torrents(self) -> list[TorrentInfo]:
        payload = await self._request("GET", "/torrents/info")
        # sin id la respuesta es {"1": {...}, "2": {...}} con claves numéricas
        entries = [v for v in payload.values() if isinstance(v, dict) and v.get("id")]
        return [self._to_info(entry) for entry in entries[:100]]

    async def delete_torrent(self, torrent_id: str) -> None:
        raise DebridError(f"{self.name}: la API no permite borrar torrents")

    def _to_info(self, torrent: dict) -> TorrentInfo:
        progress = float(torrent.get("progress") or 0)
        if torrent.get("error"):
            status = "error"
        elif progress >= 100:
            status = "ready"
        else:
            status = "downloading"
        detail = f"{torrent.get('seeders', 0)} seeds · {torrent.get('speed', '')}".strip(" ·")
        return TorrentInfo(
            id=str(torrent.get("id", "")),
            name=torrent.get("filename") or "torrent",
            status=status,
            progress=100.0 if status == "ready" else progress,
            detail="" if status == "ready" else detail,
        )
