# DebridBot

**Async** Telegram bot for multi-debrid services: send it a hoster link, a magnet or a `.torrent` file and choose between getting the **direct link** or having the bot **upload the file** to Telegram.

## Features

- ⚡ Fully asynchronous (kurigram/pyrogram + aiohttp).
- 🧩 Multi-service: **Real-Debrid**, **AllDebrid**, **TorBox**, **Premiumize**, **Debrid-Link**, **Mega-Debrid**, **Deepbrid** and **High-Way** (use one or several, switch with `/service`).
- 🔗 **Option 1 — Link**: unlocks the link and gives you the premium direct download.
- 📤 **Option 2 — File**: downloads the file and uploads it to Telegram with a progress bar (2 GB limit).
- 🧲 **Torrents**: accepts magnets and `.torrent` files, shows live progress and offers each file with the same two options when finished.
- 🛠 **Torrent manager** (`/torrents`): browse your torrents, check progress, get links, restart (AllDebrid, Premiumize) or delete them with inline buttons.
- 📋 **controlc.com pastes**: extracts the links from the paste (prioritizes the hosts in `PASTE_HOST_PRIORITY` in `controlc.py`) and unlocks them all.
- 🔐 **filecrypt.cc folders**: extracts links via CNL2 (Click'n'Load, same idea as JDownloader). Password: send it after the URL. Captcha folders open **Chrome with uBlock Origin** (+ a small popup guard) so ads don't kick you out of the container.
- 🪞 Mirror rewriting (e.g. `turb.to` → `turbobit.net`) — edit `MIRRORS` in `main.py`.
- 🔒 Optional user whitelist (`ALLOWED_USER_IDS`).
- 🧦 Optional proxy for debrid traffic only (`DEBRID_PROXY`): SOCKS5/SOCKS4/HTTP. Useful on VPSes whose datacenter IPs are blocked by AllDebrid & co. — API calls **and** file downloads go through the proxy, Telegram stays direct.
- 🎯 Per-hoster routing (`HOST_RULES`): pin a hoster to a specific service, e.g. `rapidgator:torbox` unlocks every rapidgator link with TorBox regardless of the active service.
- 🛟 Automatic failover (`FAILOVER`, on by default): if the chosen service can't unlock a link, the bot silently tries your other configured services and answers with the first one that works.
- 🔁 Optional link relay (`LINK_PROXY`): instead of the raw debrid URL you get a URL served by the bot itself (`http://bot_ip:8845/dl/…`), and the bot streams the file from the debrid. The debrid only ever sees one IP (the server's), which avoids "downloads from multiple IPs" bans when the bot runs on a VPS. Supports resume/Range. Remember to open the port.
- 🎬 Optional **yt-dlp** fallback (`YTDLP=true`): if debrid and direct download fail, try [yt-dlp](https://github.com/yt-dlp/yt-dlp) (YouTube, Vimeo, and many other sites). Shows a **quality picker** (best / 1080p / 720p / audio…) then Link or File. Not installed by default — `pip install yt-dlp` (ffmpeg recommended for merging video+audio). By default (`YTDLP_REENCODE_H264=true`) file downloads are re-encoded to **H.264 + AAC** so they play in QuickTime/macOS (skipped if already compatible).
- 💧 **DripFiles** button (`DRIPFILES=true` by default): after unlocking, upload the file to [DripFiles](https://dripfiles.com) via their free public API (no key, ~2 GB, links expire in ~2 days) and get a share URL. Optional description via `DRIPFILES_MESSAGE` (`{filename}`, `{host}`, `{size}` → API field `message`).

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
   - At least one API key from: [Real-Debrid](https://real-debrid.com/apitoken), [AllDebrid](https://alldebrid.com/apikeys), [TorBox](https://torbox.app/settings), [Premiumize](https://www.premiumize.me/account), [Debrid-Link](https://debrid-link.com/webapp/apikey), [Mega-Debrid](https://www.mega-debrid.eu/index.php?page=moncompte) (app API key **or** `MEGADEBRID_LOGIN`/`MEGADEBRID_PASSWORD`), [Deepbrid](https://www.deepbrid.com/account), [High-Way](https://high-way.me) (`HIGHWAY_LOGIN`/`HIGHWAY_PASSWORD`)

2. Install and run:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium   # o usa Google Chrome del sistema
   # Opcional: YouTube/Vimeo/etc. → pip install yt-dlp  y YTDLP=true en .env
   python main.py
   ```

   Filecrypt con captcha abre el **Chromium de Playwright** (no el Chrome del
   sistema: ese ignora `--load-extension`) con **uBlock Origin Lite (MV3)**
   y **Filecrypt Guard**. El uBlock clásico (MV2) ya no carga en Chrome moderno.
   En servidores sin pantalla no podrá resolver captchas.

### Docker

```bash
docker build -t debrid-bot .
docker run -d --env-file .env -p 8845:8845 -v debrid_downloads:/app/downloads debrid-bot
```

- `-p 8845:8845` is only needed if you enable `LINK_PROXY`.
- Add `--build-arg INSTALL_BROWSER=true` to bundle Playwright's Chromium (~700 MB extra) for filecrypt. Note that captcha-protected folders still need a display, so on a headless VPS they won't work either way — CNL2 folders (no captcha) work fine without the browser.
- Add `--build-arg INSTALL_YTDLP=true` to install `yt-dlp` + `ffmpeg` in the image, then set `YTDLP=true` in `.env`.
- The `.env` file is passed at runtime and never baked into the image.

## Project layout

```
main.py            # Telegram bot (handlers, progress, upload)
config.py          # Configuration via environment variables / .env
controlc.py        # controlc.com paste link extraction
filecrypt.py       # filecrypt.cc (CNL2 + Chrome/uBlock for captcha)
ytdlp.py           # optional yt-dlp fallback (YouTube, Vimeo, …)
dripfiles.py       # DripFiles free API client (upload + share link)
extensions/
  ublock/          # uBlock Origin (auto-downloaded if missing)
  fc-guard/        # closes ad popups
debrid/
  base.py          # Common provider interface
  realdebrid.py    # Real-Debrid
  alldebrid.py     # AllDebrid (API v4/v4.1)
  torbox.py        # TorBox (API v1)
  premiumize.py    # Premiumize
  debridlink.py    # Debrid-Link (API v2)
  megadebrid.py    # Mega-Debrid
  deepbrid.py      # Deepbrid
  highway.py       # High-Way (link debrid + torrent list; adding torrents is web-only)
```

## Credits

Original base by [Oihalitz](https://github.com/Oihalitz/DebridBot) (formerly RealDebridTelegram), uploader by [StarMade✨](https://github.com/StarMadeThis) inspired by [Anasty17](https://github.com/anasty17).
