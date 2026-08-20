from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import aiohttp


class DebridError(Exception):
    """Error devuelto por la API de un servicio debrid."""


@dataclass
class UnrestrictedLink:
    url: str
    filename: str
    host: str
    size: int | None = None
    # "ytdlp" = hay que bajar con yt-dlp (HLS/DASH, etc.), no con un GET simple
    via: str | None = None
    # selector de formato yt-dlp (p.ej. "bv*[height=720]+ba/b") si via=ytdlp
    format_selector: str | None = None


@dataclass
class TorrentFile:
    """Un archivo dentro de un torrent."""

    id: str
    name: str
    path: str = ""
    size: int | None = None
    selected: bool = False
    # Enlace del hoster o directo si el servicio ya lo expone (aún puede
    # hacer falta unrestrict; cada provider lo resuelve en torrent_file_link).
    url: str | None = None


@dataclass
class TorrentInfo:
    id: str
    name: str
    status: str  # queued | downloading | ready | error | select
    progress: float  # 0-100
    detail: str = ""  # estado textual tal cual lo reporta el servicio
    files: list[TorrentFile] = field(default_factory=list)
    # True si el servicio espera a que elijamos archivos (Real-Debrid)
    needs_file_selection: bool = False


class DebridProvider(ABC):
    name: str
    slug: str
    supports_restart: bool = False
    supports_delete: bool = True
    # Real-Debrid (y similares): hay que elegir archivos antes de descargar
    supports_file_selection: bool = False

    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session

    @abstractmethod
    async def unrestrict(self, link: str) -> UnrestrictedLink:
        """Convierte un enlace de hoster en un enlace directo premium."""

    @abstractmethod
    async def add_magnet(self, magnet: str) -> str:
        """Añade un magnet y devuelve el id del torrent en el servicio."""

    @abstractmethod
    async def add_torrent_file(self, raw: bytes, filename: str) -> str:
        """Añade un archivo .torrent y devuelve el id del torrent."""

    @abstractmethod
    async def torrent_info(self, torrent_id: str) -> TorrentInfo: ...

    @abstractmethod
    async def torrent_links(self, torrent_id: str) -> list[UnrestrictedLink]:
        """Enlaces directos de un torrent ya completado."""

    @abstractmethod
    async def list_torrents(self) -> list[TorrentInfo]: ...

    @abstractmethod
    async def delete_torrent(self, torrent_id: str) -> None: ...

    async def restart_torrent(self, torrent_id: str) -> None:
        raise DebridError(f"{self.name}: no soporta reiniciar torrents")

    async def torrent_files(self, torrent_id: str) -> list[TorrentFile]:
        """Archivos del torrent. Por defecto los que vengan en torrent_info."""
        info = await self.torrent_info(torrent_id)
        return list(info.files)

    async def select_torrent_files(self, torrent_id: str, file_ids: list[str]) -> None:
        raise DebridError(f"{self.name}: no permite elegir archivos del torrent")

    async def torrent_file_link(self, torrent_id: str, file: TorrentFile) -> UnrestrictedLink:
        """Enlace directo de un archivo concreto ya descargado."""
        if file.url:
            return UnrestrictedLink(
                url=file.url,
                filename=file.name,
                host=self.slug,
                size=file.size,
            )
        links = await self.torrent_links(torrent_id)
        for link in links:
            if link.filename == file.name or file.name.endswith(link.filename):
                return link
        if len(links) == 1:
            return links[0]
        raise DebridError(f"{self.name}: no hay enlace para {file.name}")
