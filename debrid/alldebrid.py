from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink

BASE = "https://api.alldebrid.com/v4"
BASE_41 = "https://api.alldebrid.com/v4.1"
AGENT = "DebridBot"

log = logging.getLogger("bot.alldebrid")

# polling delayed links: cada 5s, hasta ~5 min
_DELAYED_POLL_SEC = 5
_DELAYED_MAX_TRIES = 60


@dataclass
class AdStreamOption:
    id: str
    label: str
    size: int | None = None
    quality: str | int | None = None
    ext: str | None = None


@dataclass
class AdStreamProbe:
    """Resultado de /link/unlock cuando hay varias calidades de stream."""

    unlock_id: str
    filename: str
    host: str
    streams: list[AdStreamOption] = field(default_factory=list)
    source_url: str = ""
    host_domain: str = ""


class NeedsStreamChoice(Exception):
    """AllDebrid devolvió calidades de stream: hay que elegir una y llamar a streaming."""

    def __init__(self, probe: AdStreamProbe):
        self.probe = probe
        super().__init__(
            f"AllDebrid: elige calidad ({len(probe.streams)} opciones) para {probe.filename}"
        )


def _fmt_size(n: int | None) -> str | None:
    if not n or n <= 0:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _stream_label(stream: dict) -> str:
    quality = stream.get("quality")
    ext = (stream.get("ext") or "").lower()
    name = (stream.get("name") or "").strip()
    size_txt = _fmt_size(stream.get("filesize") or None)
    abr = stream.get("abr")

    parts: list[str] = []
    if isinstance(quality, (int, float)) and quality > 0:
        parts.append(f"{int(quality)}p")
    elif isinstance(quality, str) and quality.strip():
        q = quality.strip()
        if q.isdigit():
            parts.append(f"{q}p")
        else:
            parts.append(q.upper() if len(q) <= 6 else q)
    elif name:
        parts.append(name[:24])
    else:
        parts.append(stream.get("id") or "stream")

    if abr and not any(str(abr) in p for p in parts):
        try:
            parts.append(f"{int(abr)}kbps")
        except (TypeError, ValueError):
            pass
    if ext and ext not in ("", "mp4"):
        parts.append(ext)
    if size_txt:
        parts.append(f"~{size_txt}")
    return " · ".join(parts)


def _stream_rank(stream: dict) -> tuple:
    """Mayor = mejor calidad de vídeo (o audio)."""
    quality = stream.get("quality")
    ext = (stream.get("ext") or "").lower()
    size = stream.get("filesize") or 0
    q_num = 0
    if isinstance(quality, (int, float)):
        q_num = int(quality)
    elif isinstance(quality, str) and quality.isdigit():
        q_num = int(quality)
    # preferir vídeo mp4 sobre solo audio
    is_video = 1 if ext in ("mp4", "webm", "mkv", "m4v") or q_num >= 144 else 0
    if ext in ("mp3", "m4a", "opus") or (
        isinstance(quality, str) and quality.lower() in ("mp3", "audio")
    ):
        is_video = 0
    return (is_video, q_num, size)


def _parse_streams(raw: list | None) -> list[AdStreamOption]:
    options: list[AdStreamOption] = []
    for s in raw or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        options.append(
            AdStreamOption(
                id=str(s["id"]),
                label=_stream_label(s),
                size=s.get("filesize") or None,
                quality=s.get("quality"),
                ext=(s.get("ext") or None),
            )
        )
    # ordenar de mejor a peor (misma lógica que el auto-pick)
    ranked = sorted(
        ((o, _stream_rank({"quality": o.quality, "ext": o.ext, "filesize": o.size or 0}))
         for o in options),
        key=lambda t: t[1],
        reverse=True,
    )
    return [o for o, _ in ranked]


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

    async def _wait_delayed(
        self,
        delayed_id: int | str,
        *,
        filename: str,
        host: str,
        size: int | None = None,
    ) -> UnrestrictedLink:
        """Espera a que /link/delayed devuelva el enlace de descarga."""
        last_left = None
        for attempt in range(_DELAYED_MAX_TRIES):
            data = await self._request("POST", "/link/delayed", data={"id": delayed_id})
            status = data.get("status")
            if status == 2 and data.get("link"):
                return UnrestrictedLink(
                    url=data["link"],
                    filename=filename or data.get("filename") or "download",
                    host=host,
                    size=size or data.get("filesize") or None,
                )
            if status == 3:
                raise DebridError(f"{self.name}: no se pudo generar el enlace (delayed)")
            left = data.get("time_left")
            if left is not None and left != last_left:
                log.info("AllDebrid delayed %s: ~%ss restantes", delayed_id, left)
                last_left = left
            await asyncio.sleep(_DELAYED_POLL_SEC)
        raise DebridError(
            f"{self.name}: timeout esperando el enlace delayed ({delayed_id})"
        )

    async def select_stream(
        self,
        unlock_id: str,
        stream_id: str,
        *,
        filename: str = "",
        host: str = "stream",
    ) -> UnrestrictedLink:
        """Elige una calidad de /link/unlock vía /link/streaming."""
        data = await self._request(
            "POST",
            "/link/streaming",
            data={"id": unlock_id, "stream": stream_id},
        )
        fname = data.get("filename") or filename or "download"
        size = data.get("filesize") or None
        link = (data.get("link") or "").strip()
        delayed = data.get("delayed")
        if delayed and not link:
            return await self._wait_delayed(
                delayed, filename=fname, host=host, size=size
            )
        if not link:
            raise DebridError(f"{self.name}: streaming no devolvió enlace de descarga")
        return UnrestrictedLink(
            url=link,
            filename=fname,
            host=host,
            size=size,
        )

    def best_stream_id(self, streams: list[AdStreamOption] | list[dict]) -> str:
        """Elige la mejor calidad (vídeo más alto; si no, mayor tamaño)."""
        if not streams:
            raise DebridError(f"{self.name}: no hay streams disponibles")
        if isinstance(streams[0], AdStreamOption):
            # ya ordenados en _parse_streams
            return streams[0].id
        best = max(streams, key=_stream_rank)
        return str(best["id"])

    async def unrestrict(
        self, link: str, *, auto_pick_stream: bool = False
    ) -> UnrestrictedLink:
        """Desbloquea un enlace. Si hay varias calidades de stream:

        - auto_pick_stream=True → elige la mejor (lotes / failover silencioso)
        - auto_pick_stream=False → lanza NeedsStreamChoice para menú en Telegram
        """
        data = await self._request("POST", "/link/unlock", data={"link": link})
        filename = data.get("filename") or "download"
        host = data.get("host") or data.get("hostDomain") or ""
        size = data.get("filesize") or None
        download = (data.get("link") or "").strip()
        delayed = data.get("delayed")
        raw_streams = data.get("streams") or []
        unlock_id = str(data.get("id") or "")

        if delayed and not download:
            return await self._wait_delayed(
                delayed, filename=filename, host=host, size=size
            )

        # Streaming: link vacío y lista de calidades
        if raw_streams and not download:
            options = _parse_streams(raw_streams)
            if not options:
                raise DebridError(f"{self.name}: streams vacíos para este enlace")
            if len(options) == 1 or auto_pick_stream:
                stream_id = options[0].id if len(options) == 1 else self.best_stream_id(options)
                if not unlock_id:
                    raise DebridError(f"{self.name}: falta id de unlock para streaming")
                return await self.select_stream(
                    unlock_id, stream_id, filename=filename, host=host or "stream"
                )
            raise NeedsStreamChoice(
                AdStreamProbe(
                    unlock_id=unlock_id,
                    filename=filename,
                    host=host or "stream",
                    streams=options,
                    source_url=link,
                    host_domain=data.get("hostDomain") or "",
                )
            )

        if not download:
            raise DebridError(f"{self.name}: no se obtuvo enlace de descarga")

        return UnrestrictedLink(
            url=download,
            filename=filename,
            host=host,
            size=size,
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
        return [await self.unrestrict(entry["l"], auto_pick_stream=True) for entry in flat]

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
