from __future__ import annotations

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://www.premiumize.me/api"

_STATUS_MAP = {
    "waiting": "queued",
    "queued": "queued",
    "running": "downloading",
    "finished": "ready",
    "seeding": "ready",
    "error": "error",
    "timeout": "error",
    "deleted": "error",
    "banned": "error",
}


class Premiumize(DebridProvider):
    name = "Premiumize"
    slug = "premiumize"
    supports_restart = True

    async def _request(self, method: str, path: str, *, params: dict | None = None, data=None):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with self.session.request(
            method, f"{BASE}{path}", params=params, data=data, headers=headers
        ) as resp:
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise DebridError(f"{self.name}: respuesta inesperada de la API")
        if payload.get("status") == "error":
            raise DebridError(f"{self.name}: {payload.get('message', 'error desconocido')}")
        return payload

    async def unrestrict(self, link: str) -> UnrestrictedLink:
        data = await self._request("POST", "/transfer/directdl", data={"src": link})
        content = data.get("content") or []

        # Para un enlace de hoster simple la API responde con los datos arriba;
        # los contenedores/torrents cacheados los devuelven dentro de content[]
        if data.get("location"):
            return UnrestrictedLink(
                url=data["location"],
                filename=data.get("filename") or "archivo",
                host=self.slug,
                size=data.get("filesize") or None,
            )
        if content:
            first = content[0]
            return UnrestrictedLink(
                url=first["link"],
                filename=(first.get("path") or "archivo").split("/")[-1],
                host=self.slug,
                size=first.get("size") or None,
            )
        raise DebridError(f"{self.name}: el enlace no devolvió ninguna descarga")

    async def add_magnet(self, magnet: str) -> str:
        data = await self._request("POST", "/transfer/create", data={"src": magnet})
        return str(data["id"])

    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        form = aiohttp.FormData()
        form.add_field("src", raw, filename=filename, content_type="application/x-bittorrent")
        data = await self._request("POST", "/transfer/create", data=form)
        return str(data["id"])

    async def _find_transfer(self, torrent_id: str) -> dict:
        # Premiumize no expone consulta por id: hay que filtrar la lista completa
        data = await self._request("GET", "/transfer/list")
        for transfer in data.get("transfers") or []:
            if str(transfer.get("id")) == str(torrent_id):
                return transfer
        raise DebridError(f"{self.name}: transferencia no encontrada")

    async def torrent_info(self, torrent_id: str) -> TorrentInfo:
        return self._to_info(await self._find_transfer(torrent_id))

    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        transfer = await self._find_transfer(torrent_id)

        if transfer.get("file_id"):
            data = await self._request(
                "GET", "/item/details", params={"id": str(transfer["file_id"])}
            )
            if data.get("link"):
                return [
                    UnrestrictedLink(
                        url=data["link"],
                        filename=data.get("name") or "archivo",
                        host=self.slug,
                        size=data.get("size") or None,
                    )
                ]
            return []

        if transfer.get("folder_id"):
            return await self._folder_links(str(transfer["folder_id"]))

        return []

    async def _folder_links(self, folder_id: str) -> list[UnrestrictedLink]:
        data = await self._request("GET", "/folder/list", params={"id": folder_id})
        links: list[UnrestrictedLink] = []
        for item in data.get("content") or []:
            if item.get("type") == "folder":
                links.extend(await self._folder_links(str(item["id"])))
            elif item.get("link"):
                links.append(
                    UnrestrictedLink(
                        url=item["link"],
                        filename=item.get("name") or "archivo",
                        host=self.slug,
                        size=item.get("size") or None,
                    )
                )
        return links

    async def list_torrents(self) -> list[TorrentInfo]:
        data = await self._request("GET", "/transfer/list")
        transfers = data.get("transfers") or []
        return [self._to_info(transfer) for transfer in transfers[:20]]

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._request("POST", "/transfer/delete", data={"id": torrent_id})

    async def restart_torrent(self, torrent_id: str) -> None:
        await self._request("POST", "/transfer/retry", data={"id": torrent_id})

    def _to_info(self, transfer: dict) -> TorrentInfo:
        status = _STATUS_MAP.get(transfer.get("status", ""), "downloading")
        # progress llega como fracción 0-1 y falta mientras está en cola
        raw_progress = transfer.get("progress")
        progress = 100.0 if status == "ready" else float(raw_progress or 0) * 100
        return TorrentInfo(
            id=str(transfer["id"]),
            name=transfer.get("name") or "transferencia",
            status=status,
            progress=progress,
            detail=transfer.get("message") or transfer.get("status", ""),
        )
