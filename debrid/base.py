from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import aiohttp


class DebridError(Exception):
    """Error devuelto por la API de un servicio debrid."""


@dataclass
class UnrestrictedLink:
    url: str
    filename: str
    host: str
    size: int | None = None


@dataclass
class TorrentInfo:
    id: str
    name: str
    status: str  # queued | downloading | ready | error
    progress: float  # 0-100
    detail: str = ""  # estado textual tal cual lo reporta el servicio


class DebridProvider(ABC):
    name: str
    slug: str
    supports_restart: bool = False

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
