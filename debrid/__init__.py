import aiohttp

from config import Config

from .alldebrid import AllDebrid
from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink
from .premiumize import Premiumize
from .realdebrid import RealDebrid
from .torbox import TorBox

__all__ = [
    "AllDebrid",
    "DebridError",
    "DebridProvider",
    "Premiumize",
    "RealDebrid",
    "TorBox",
    "TorrentInfo",
    "UnrestrictedLink",
    "build_providers",
]


def build_providers(cfg: Config, session: aiohttp.ClientSession) -> dict[str, DebridProvider]:
    providers: dict[str, DebridProvider] = {}
    if cfg.realdebrid_key:
        providers[RealDebrid.slug] = RealDebrid(cfg.realdebrid_key, session)
    if cfg.alldebrid_key:
        providers[AllDebrid.slug] = AllDebrid(cfg.alldebrid_key, session)
    if cfg.torbox_key:
        providers[TorBox.slug] = TorBox(cfg.torbox_key, session)
    if cfg.premiumize_key:
        providers[Premiumize.slug] = Premiumize(cfg.premiumize_key, session)
    return providers
