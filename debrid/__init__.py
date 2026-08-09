import aiohttp

from config import Config

from .alldebrid import (
    AdStreamOption,
    AdStreamProbe,
    AllDebrid,
    NeedsStreamChoice,
)
from .base import DebridError, DebridProvider, TorrentInfo, UnrestrictedLink
from .debridlink import DebridLink
from .deepbrid import Deepbrid
from .highway import Highway
from .megadebrid import MegaDebrid
from .premiumize import Premiumize
from .realdebrid import RealDebrid
from .torbox import TorBox

__all__ = [
    "AdStreamOption",
    "AdStreamProbe",
    "AllDebrid",
    "DebridError",
    "DebridLink",
    "DebridProvider",
    "Deepbrid",
    "Highway",
    "MegaDebrid",
    "NeedsStreamChoice",
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
    if cfg.debridlink_key:
        providers[DebridLink.slug] = DebridLink(cfg.debridlink_key, session)
    if cfg.deepbrid_key:
        providers[Deepbrid.slug] = Deepbrid(cfg.deepbrid_key, session)
    if cfg.megadebrid_key or (cfg.megadebrid_login and cfg.megadebrid_password):
        providers[MegaDebrid.slug] = MegaDebrid(
            cfg.megadebrid_key or "",
            session,
            login=cfg.megadebrid_login,
            password=cfg.megadebrid_password,
        )
    if cfg.highway_login and cfg.highway_password:
        providers[Highway.slug] = Highway(
            "", session, login=cfg.highway_login, password=cfg.highway_password
        )
    return providers
