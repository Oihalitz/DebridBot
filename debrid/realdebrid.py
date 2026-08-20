from __future__ import annotations

import asyncio

from .base import DebridError, DebridProvider, TorrentFile, TorrentInfo, UnrestrictedLink

BASE = "https://api.real-debrid.com/rest/1.0"

_STATUS_MAP = {
    "magnet_conversion": "queued",
    "waiting_files_selection": "select",
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


def _parse_files(data: dict) -> list[TorrentFile]:
    files: list[TorrentFile] = []
    raw_files = data.get("files") or []
    links = data.get("links") or []
    for item in raw_files:
        path = str(item.get("path") or "").lstrip("/")
        name = path.rsplit("/", 1)[-1] or path or "archivo"
        files.append(
            TorrentFile(
                id=str(item["id"]),
                name=name,
                path=path,
                size=int(item["bytes"]) if item.get("bytes") is not None else None,
                selected=bool(item.get("selected")),
            )
        )
    # links[] va en el mismo orden que los archivos seleccionados, si las
    # longitudes coinciden; si no, torrent_file_link desambigua por nombre
    selected = [f for f in files if f.selected]
    if len(selected) == len(links):
        for torrent_file, link in zip(selected, links):
            torrent_file.url = link
    return files


class RealDebrid(DebridProvider):
    name = "Real-Debrid"
    slug = "realdebrid"
    supports_file_selection = True

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
        return str(data["id"])

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        data = await self._request("PUT", "/torrents/addTorrent", data=raw)
        return str(data["id"])

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        data = await self._request("GET", f"/torrents/info/{torrent_id}")
        return self._to_info(data)

    async def torrent_files(self, torrent_id: str) -> list[TorrentFile]:
        data = await self._request("GET", f"/torrents/info/{torrent_id}")
        return _parse_files(data)

    async def select_torrent_files(self, torrent_id: str, file_ids: list[str]) -> None:
        if not file_ids:
            raise DebridError(f"{self.name}: no hay archivos seleccionados")
        payload = {"files": ",".join(file_ids)}
        last: DebridError | None = None
        for attempt in range(6):
            try:
                await self._request(
                    "POST", f"/torrents/selectFiles/{torrent_id}", data=payload
                )
                return
            except DebridError as exc:
                last = exc
                msg = str(exc).lower()
                if "already" in msg or "selected" in msg:
                    return
                # el magnet a veces sigue convirtiéndose un momento
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last or DebridError(f"{self.name}: no se pudieron seleccionar los archivos")

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        data = await self._request("GET", f"/torrents/info/{torrent_id}")
        return [await self.unrestrict(link) for link in data.get("links") or []]

    async def torrent_file_link(self, torrent_id: str, file: TorrentFile) -> UnrestrictedLink:
        hoster = file.url
        if not hoster:
            files = await self.torrent_files(torrent_id)
            match = next((item for item in files if item.id == file.id), None)
            hoster = match.url if match else None
        if hoster:
            return await self.unrestrict(hoster)

        data = await self._request("GET", f"/torrents/info/{torrent_id}")
        links = data.get("links") or []
        if not links:
            raise DebridError(
                f"{self.name}: aún no hay enlaces (¿el torrent ha terminado?)"
            )
        if len(links) == 1:
            return await self.unrestrict(links[0])
        last_err: DebridError | None = None
        for link in links:
            try:
                unrestricted = await self.unrestrict(link)
            except DebridError as exc:
                last_err = exc
                continue
            if unrestricted.filename in (file.name, file.path) or file.name.endswith(
                unrestricted.filename
            ):
                return unrestricted
        if last_err:
            raise last_err
        raise DebridError(f"{self.name}: no hay enlace para {file.name}")

    async def list_torrents(self) -> list[TorrentInfo]:
        data = await self._request("GET", "/torrents", params={"limit": "20"}) or []
        return [self._to_info(item) for item in data]

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._request("DELETE", f"/torrents/delete/{torrent_id}")

    def _to_info(self, data: dict) -> TorrentInfo:
        raw = data.get("status", "")
        return TorrentInfo(
            id=str(data["id"]),
            name=data.get("filename") or "torrent",
            status=_STATUS_MAP.get(raw, "downloading"),
            progress=float(data.get("progress") or 0),
            detail=raw,
            files=_parse_files(data),
            needs_file_selection=raw == "waiting_files_selection",
        )
