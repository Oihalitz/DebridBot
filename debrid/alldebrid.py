from __future__ import annotations

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://api.alldebrid.com/v4"
BASE_41 = "https://api.alldebrid.com/v4.1"
AGENT = "DebridBot"


class AllDebrid(DebridProvider):
    name = "AllDebrid"
    slug = "alldebrid"
    supports_restart = True

    async def _request(self, method: str, path: str, *, params: dict | None = None, data=None):
        params = {"agent": AGENT, **(params or {})}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = path if path.startswith("http") else f"{BASE}{path}"
        async with self.session.request(
            method, url, params=params, data=data, headers=headers
        ) as resp:
            payload = await resp.json(content_type=None)
        if payload.get("status") != "success":
            error = payload.get("error") or {}
            raise DebridError(f"{self.name}: {error.get('message', 'error desconocido')}")
        return payload["data"]

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        data = await self._request("GET", "/link/unlock", params={"link": link})
        return UnrestrictedLink(
            url=data["link"],
            filename=data["filename"],
            host=data.get("host", ""),
            size=data.get("filesize") or None,
        )

    async def add_magnet(self, magnet: str) -> str:
        data = await self._request("POST", "/magnet/upload", data={"magnets[]": magnet})
        info = data["magnets"][0]
        if info.get("error"):
            raise DebridError(f"{self.name}: {info['error'].get('message', 'magnet rechazado')}")
        return str(info["id"])

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field("files[0]", raw, filename=filename, content_type="application/x-bittorrent")
        data = await self._request("POST", "/magnet/upload/file", data=form)
        info = data["files"][0]
        if info.get("error"):
            raise DebridError(f"{self.name}: {info['error'].get('message', 'torrent rechazado')}")
        return str(info["id"])

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        data = await self._request(
            "POST", f"{BASE_41}/magnet/status", data={"id": torrent_id}
        )
        magnet = data["magnets"]
        if isinstance(magnet, list):
            if not magnet:
                raise DebridError(f"{self.name}: torrent no encontrado")
            magnet = magnet[0]
        return self._to_info(magnet)

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        data = await self._request("GET", "/magnet/files", params={"id[]": torrent_id})
        magnets = data.get("magnets") or []
        if not magnets:
            return []
        flat: list[dict] = []

        def walk(entries: list[dict]):
            for entry in entries:
                if entry.get("e"):
                    walk(entry["e"])
                elif entry.get("l"):
                    flat.append(entry)

        walk(magnets[0].get("files") or [])
        return [await self.unrestrict(entry["l"]) for entry in flat]

    async def list_torrents(self) -> list[TorrentInfo]:
        data = await self._request("POST", f"{BASE_41}/magnet/status")
        magnets = data.get("magnets") or []
        if isinstance(magnets, dict):
            magnets = [magnets]
        return [self._to_info(m) for m in magnets[:100]]

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._request("POST", "/magnet/delete", data={"id": torrent_id})

    async def restart_torrent(self, torrent_id: str) -> None:
        await self._request("POST", "/magnet/restart", data={"id": torrent_id})

    def _to_info(self, magnet: dict) -> TorrentInfo:
        code = magnet.get("statusCode", 0)
        if code == 4:
            status = "ready"
        elif code >= 5:
            status = "error"
        elif code == 0:
            status = "queued"
        else:
            status = "downloading"
        size = magnet.get("size") or 0
        downloaded = magnet.get("downloaded") or 0
        progress = 100.0 if status == "ready" else (downloaded / size * 100 if size else 0.0)
        return TorrentInfo(
            id=str(magnet["id"]),
            name=magnet.get("filename") or "torrent",
            status=status,
            progress=progress,
            detail=magnet.get("status", ""),
        )
