from __future__ import annotations

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://api.real-debrid.com/rest/1.0"

_STATUS_MAP = {
    "magnet_conversion": "queued",
    "waiting_files_selection": "queued",
    "queued": "queued",
    "downloading": "downloading",
    "compressing": "downloading",
    "uploading": "downloading",
    "downloaded": "ready",
    "magnet_error": "error",
    "error": "error",
    "virus": "error",
    "dead": "error",
}


class RealDebrid(DebridProvider):
    name = "Real-Debrid"
    slug = "realdebrid"

    async def _request(self, method: str, path: str, **kwargs):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with self.session.request(method, f"{BASE}{path}", headers=headers, **kwargs) as resp:
            if resp.status == 204:
                return None
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and "error" in data:
                raise DebridError(f"{self.name}: {data['error']}")
            if resp.status >= 400:
                raise DebridError(f"{self.name}: HTTP {resp.status}")
            return data

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        data = await self._request("POST", "/unrestrict/link", data={"link": link})
        return UnrestrictedLink(
            url=data["download"],
            filename=data["filename"],
            host=data["host"],
            size=data.get("filesize") or None,
        )

    async def add_magnet(self, magnet: str) -> str:
        data = await self._request("POST", "/torrents/addMagnet", data={"magnet": magnet})
        await self._select_all(data["id"])
        return str(data["id"])

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        data = await self._request("PUT", "/torrents/addTorrent", data=raw)
        await self._select_all(data["id"])
        return str(data["id"])

    async def _select_all(self, torrent_id: str):
        # Puede fallar si el magnet aún se está convirtiendo; torrent_info lo reintenta
        try:
            await self._request("POST", f"/torrents/selectFiles/{torrent_id}", data={"files": "all"})
        except DebridError:
            pass

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        data = await self._request("GET", f"/torrents/info/{torrent_id}")
        if data["status"] == "waiting_files_selection":
            await self._select_all(torrent_id)
        return self._to_info(data)

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        data = await self._request("GET", f"/torrents/info/{torrent_id}")
        return [await self.unrestrict(link) for link in data.get("links") or []]

    async def list_torrents(self) -> list[TorrentInfo]:
        data = await self._request("GET", "/torrents", params={"limit": "20"}) or []
        return [self._to_info(item) for item in data]

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._request("DELETE", f"/torrents/delete/{torrent_id}")

    def _to_info(self, data: dict) -> TorrentInfo:
        return TorrentInfo(
            id=str(data["id"]),
            name=data.get("filename") or "torrent",
            status=_STATUS_MAP.get(data.get("status", ""), "downloading"),
            progress=float(data.get("progress") or 0),
            detail=data.get("status", ""),
        )
