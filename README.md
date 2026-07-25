# DebridBot

**Async** Telegram bot for multi-debrid services: send it a hoster link, a magnet or a `.torrent` file and choose between getting the **direct link** or having the bot **upload the file** to Telegram.

## Features

- ⚡ Fully asynchronous (kurigram/pyrogram + aiohttp).
- 🧩 Multi-service: **Real-Debrid**, **AllDebrid**, **TorBox** and **Premiumize** (use one or several, switch with `/service`).
- 🔗 **Option 1 — Link**: unlocks the link and gives you the premium direct download.
- 📤 **Option 2 — File**: downloads the file and uploads it to Telegram with a progress bar (2 GB limit).
- 🧲 **Torrents**: accepts magnets and `.torrent` files, shows live progress and offers each file with the same two options when finished.
- 🛠 **Torrent manager** (`/torrents`): browse your torrents, check progress, get links, restart (AllDebrid, Premiumize) or delete them with inline buttons.
- 📋 **controlc.com pastes**: extracts the links from the paste (prioritizes the hosts in `PASTE_HOST_PRIORITY` in `controlc.py`) and unlocks them all.
- 🔐 **filecrypt.cc folders**: extracts links via CNL2 (Click'n'Load, same idea as JDownloader). Password: send it after the URL. Captcha folders open **Chrome with uBlock Origin** (+ a small popup guard) so ads don't kick you out of the container.
- 🪞 Mirror rewriting (e.g. `turb.to` → `turbobit.net`) — edit `MIRRORS` in `main.py`.
- 🔒 Optional user whitelist (`ALLOWED_USER_IDS`).

## Commands

| Command | Description |
|---|---|
| `/service` | Choose the active debrid service |
| `/torrents` | Manage your torrents: progress, links, restart, delete |
| `/help` | Help |

## Setup

1. Copy the config file and fill it in:

   ```bash
   cp .env.example .env
   ```

   - `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`: [my.telegram.org/apps](https://my.telegram.org/apps)
   - `TELEGRAM_BOT_TOKEN`: [@BotFather](https://t.me/BotFather)
   - At least one API key from: [Real-Debrid](https://real-debrid.com/apitoken), [AllDebrid](https://alldebrid.com/apikeys), [TorBox](https://torbox.app/settings), [Premiumize](https://www.premiumize.me/account)

2. Install and run:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium   # o usa Google Chrome del sistema
   python main.py
   ```

   Filecrypt con captcha abre el **Chromium de Playwright** (no el Chrome del
   sistema: ese ignora `--load-extension`) con **uBlock Origin Lite (MV3)**
   y **Filecrypt Guard**. El uBlock clásico (MV2) ya no carga en Chrome moderno.
   En servidores sin pantalla no podrá resolver captchas.

### Docker

```bash
docker build -t debrid-bot .
docker run --env-file .env debrid-bot
```

## Project layout

```
main.py            # Telegram bot (handlers, progress, upload)
config.py          # Configuration via environment variables / .env
controlc.py        # controlc.com paste link extraction
filecrypt.py       # filecrypt.cc (CNL2 + Chrome/uBlock for captcha)
extensions/
  ublock/          # uBlock Origin (auto-downloaded if missing)
  fc-guard/        # closes ad popups
debrid/
  base.py          # Common provider interface
  realdebrid.py    # Real-Debrid
  alldebrid.py     # AllDebrid (API v4/v4.1)
  torbox.py        # TorBox (API v1)
  premiumize.py    # Premiumize
```

## Credits

Original base by [Oihalitz](https://github.com/Oihalitz/DebridBot) (formerly RealDebridTelegram), uploader by [StarMade✨](https://github.com/StarMadeThis) inspired by [Anasty17](https://github.com/anasty17).
