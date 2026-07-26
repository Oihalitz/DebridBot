from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://api.torbox.app/v1/api"

_ERROR_STATES = {"failed", "error", "missingFiles"}
_QUEUED_STATES = {"queued", "metaDL", "checking", "checkingResumeData", "paused"}


class TorBox(DebridProvider):
    name = "TorBox"
    slug = "torbox"

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, data=None, json=None
    ):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with self.session.request(
            method, f"{BASE}{path}", params=params, data=data, json=json, headers=headers
        ) as resp:
            payload = await resp.json(content_type=None)
            if not payload.get("success"):
                detail = payload.get("detail") or payload.get("error") or f"HTTP {resp.status}"
                raise DebridError(f"{self.name}: {detail}")
            return payload.get("data")

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        data = await self._request("POST", "/webdl/createwebdownload", data={"link": link})
        web_id = data.get("webdownload_id") or data.get("id")
        if web_id is None:
            raise DebridError(f"{self.name}: no se pudo crear la descarga web")

        host = urlparse(link).netloc
        for _ in range(60):
            item = await self._request(
                "GET", "/webdl/mylist", params={"id": str(web_id), "bypass_cache": "true"}
            )
            if isinstance(item, list):
                item = item[0] if item else None
            if not item:
                raise DebridError(f"{self.name}: la descarga desapareció de la lista")
            state = item.get("download_state") or ""
            if state in _ERROR_STATES:
                raise DebridError(f"{self.name}: error del hoster ({state})")
            if item.get("download_present"):
                files = item.get("files") or []
                if not files:
                    raise DebridError(f"{self.name}: la descarga no contiene archivos")
                file = files[0]
                url = await self._request(
                    "GET",
                    "/webdl/requestdl",
                    params={
                        "token": self.api_key,
                        "web_id": str(web_id),
                        "file_id": str(file["id"]),
                    },
                )
                return UnrestrictedLink(
                    url=url,
                    filename=file.get("short_name") or item.get("name") or "archivo",
                    host=host,
                    size=file.get("size") or None,
                )
            await asyncio.sleep(2)
        raise DebridError(f"{self.name}: tiempo de espera agotado generando el enlace")

    async def add_magnet(self, magnet: str) -> str:
        form = aiohttp.FormData()
        form.add_field("magnet", magnet)
        data = await self._request("POST", "/torrents/createtorrent", data=form)
        return str(data.get("torrent_id") or data.get("id"))

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field("file", raw, filename=filename, content_type="application/x-bittorrent")
        data = await self._request("POST", "/torrents/createtorrent", data=form)
        return str(data.get("torrent_id") or data.get("id"))

    async def _get_torrent(self, torrent_id: str) -> dict:
        item = await self._request(
            "GET", "/torrents/mylist", params={"id": torrent_id, "bypass_cache": "true"}
        )
        if isinstance(item, list):
            item = item[0] if item else None
        if not item:
            raise DebridError(f"{self.name}: torrent no encontrado")
        return item

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        return self._to_info(await self._get_torrent(torrent_id))

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        item = await self._get_torrent(torrent_id)
        links = []
        for file in item.get("files") or []:
            url = await self._request(
                "GET",
                "/torrents/requestdl",
                params={
                    "token": self.api_key,
                    "torrent_id": str(torrent_id),
                    "file_id": str(file["id"]),
                },
            )
            links.append(
                UnrestrictedLink(
                    url=url,
                    filename=file.get("short_name") or file.get("name") or "archivo",
                    host="torbox",
                    size=file.get("size") or None,
                )
            )
        return links

    async def list_torrents(self) -> list[TorrentInfo]:
        data = await self._request("GET", "/torrents/mylist", params={"bypass_cache": "true"})
        items = data if isinstance(data, list) else [data] if data else []
        return [self._to_info(item) for item in items[:100]]

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._request(
            "POST",
            "/torrents/controltorrent",
            json={"torrent_id": torrent_id, "operation": "delete"},
        )

    def _to_info(self, item: dict) -> TorrentInfo:
        state = item.get("download_state") or ""
        if item.get("download_present"):
            status = "ready"
        elif state in _ERROR_STATES:
            status = "error"
        elif state in _QUEUED_STATES:
            status = "queued"
        else:
            status = "downloading"
        return TorrentInfo(
            id=str(item.get("id")),
            name=item.get("name") or "torrent",
            status=status,
            progress=float(item.get("progress") or 0) * 100,
            detail=state,
        )
