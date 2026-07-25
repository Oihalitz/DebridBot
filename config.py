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
    allowed_users: frozenset[int]
    download_dir: str


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

    return Config(
        api_id=int(_env("TELEGRAM_API_ID")),
        api_hash=_env("TELEGRAM_API_HASH"),
        bot_token=_env("TELEGRAM_BOT_TOKEN"),
        realdebrid_key=_env("REALDEBRID_API_KEY"),
        alldebrid_key=_env("ALLDEBRID_API_KEY"),
        torbox_key=_env("TORBOX_API_KEY"),
        premiumize_key=_env("PREMIUMIZE_API_KEY"),
        allowed_users=allowed,
        download_dir=_env("DOWNLOAD_DIR") or "downloads",
    )
