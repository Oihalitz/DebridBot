import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    realdebrid_key: str | None
    alldebrid_key: str | None
    torbox_key: str | None
    premiumize_key: str | None
    debridlink_key: str | None
    deepbrid_key: str | None
    megadebrid_key: str | None
    megadebrid_login: str | None
    megadebrid_password: str | None
    highway_login: str | None
    highway_password: str | None
    allowed_users: frozenset[int]
    download_dir: str
    debrid_proxy: str | None
    link_proxy: bool
    link_proxy_port: int
    link_proxy_url: str | None
    host_rules: tuple[tuple[str, str], ...]
    failover: bool


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def load_config() -> Config:
    missing = [
        name
        for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN")
        if not _env(name)
    ]
    if missing:
        raise SystemExit(f"Faltan variables de entorno: {', '.join(missing)} (revisa .env.example)")

    raw_users = _env("ALLOWED_USER_IDS") or ""
    allowed = frozenset(int(uid) for uid in raw_users.replace(" ", "").split(",") if uid)

    # HOST_RULES=rapidgator:torbox, 1fichier.com:alldebrid
    rules = []
    for part in (_env("HOST_RULES") or "").split(","):
        host, sep, slug = part.strip().lower().partition(":")
        if not sep:
            if part.strip():
                raise SystemExit(f"HOST_RULES: entrada inválida '{part.strip()}' (formato host:servicio)")
            continue
        rules.append((host.strip(), slug.strip()))

    proxy = _env("DEBRID_PROXY")
    if proxy and not proxy.startswith(("socks5://", "socks5h://", "socks4://", "http://")):
        raise SystemExit(
            "DEBRID_PROXY debe empezar por socks5://, socks5h://, socks4:// o http:// "
            f"(recibido: {proxy.split('://')[0]}://...)"
        )

    return Config(
        api_id=int(_env("TELEGRAM_API_ID")),
        api_hash=_env("TELEGRAM_API_HASH"),
        bot_token=_env("TELEGRAM_BOT_TOKEN"),
        realdebrid_key=_env("REALDEBRID_API_KEY"),
        alldebrid_key=_env("ALLDEBRID_API_KEY"),
        torbox_key=_env("TORBOX_API_KEY"),
        premiumize_key=_env("PREMIUMIZE_API_KEY"),
        debridlink_key=_env("DEBRIDLINK_API_KEY"),
        deepbrid_key=_env("DEEPBRID_API_KEY"),
        megadebrid_key=_env("MEGADEBRID_API_KEY"),
        megadebrid_login=_env("MEGADEBRID_LOGIN"),
        megadebrid_password=_env("MEGADEBRID_PASSWORD"),
        highway_login=_env("HIGHWAY_LOGIN"),
        highway_password=_env("HIGHWAY_PASSWORD"),
        allowed_users=allowed,
        download_dir=_env("DOWNLOAD_DIR") or "downloads",
        debrid_proxy=proxy,
        link_proxy=(_env("LINK_PROXY") or "").lower() in ("1", "true", "yes", "si", "sí"),
        link_proxy_port=int(_env("LINK_PROXY_PORT") or 8845),
        link_proxy_url=_env("LINK_PROXY_URL"),
        host_rules=tuple(rules),
        failover=(_env("FAILOVER") or "true").lower() in ("1", "true", "yes", "si", "sí"),
    )
