"""Extracción de enlaces de carpetas de filecrypt.cc.

Rutas:
1. aiohttp + CNL2 / redirects (carpetas sin captcha).
2. Chrome + uBlock Origin + Filecrypt Guard cuando hay captcha PoW
   (el mismo tipo de "I am a human" de filecrypt), bloqueando popups
   de anuncios que te sacan del contenedor.

El captcha se resuelve en un navegador real (Playwright). Las extensiones
requieren Chrome/Chromium en modo con ventana (no headless clásico).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import time
import zipfile
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

import aiohttp
from Crypto.Cipher import AES
from lxml import html
from yarl import URL

log = logging.getLogger("filecrypt")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FILECRYPT_HOSTS = ("filecrypt.cc", "filecrypt.co", "filecrypt.to")

CAPTCHA_MARKERS = (
    "cutcaptcha",
    "keycaptcha",
    "solvemedia",
    "g-recaptcha",
    "hcaptcha",
    "captcha.php",
    "circle.php",
    "/captcha/",
    "pow-captcha",
    "pow_captcha",
    "security check",
    "sicherheitsüberprüfung",
)

LINK_ID_RE = re.compile(r"/Link/([0-9A-Za-z]+)\.html")
OPEN_LINK_RE = re.compile(r"""openLink\(['"]([0-9A-Za-z]+)['"]""")
JK_KEY_RE = re.compile(r"return\s*['\"]([0-9a-fA-F]{16,64})['\"]")
REDIRECT_RES = (
    re.compile(
        r"""(?:top|window|document)\.location(?:\.href)?\s*=\s*['"]([^'"]+)""", re.I
    ),
    re.compile(
        r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+url=([^"'>\s]+)""", re.I
    ),
    re.compile(
        r"""["'](https?://[^"']+)["']\s*,\s*this\)\s*;?\s*["']?\s*class=["']download""",
        re.I,
    ),
)

PASSWORD_FIELD_NAMES = ("password", "pssw", "password__")

ROOT = Path(__file__).resolve().parent
EXT_DIR = ROOT / "extensions"
UBLOCK_DIR = EXT_DIR / "ublock"
GUARD_DIR = EXT_DIR / "fc-guard"
BROWSER_PROFILE = ROOT / ".browser-profile"
# Cookies de sesión tras pasar el captcha (PHPSESSID, etc.)
COOKIE_FILE = ROOT / ".filecrypt-cookies.json"

# uBlock Origin Lite (MV3). El uBlock clásico es MV2 y Chrome/Chromium
# moderno ya no lo carga — por eso no lo veías en la barra.
UBLOCK_ZIP_URL = (
    "https://github.com/uBlockOrigin/uBOL-home/releases/download/"
    "2026.723.1724/uBOLite_2026.723.1724.chromium.zip"
)
UBLOCK_MIN_MANIFEST = 3

CAPTCHA_TIMEOUT_S = 180
POPUP_KILL_INTERVAL_MS = 500

# Dominios de ads/popunders frecuentes en filecrypt (uBlock tarda en cargar listas)
AD_HOST_MARKERS = (
    "adsterra",
    "opera.com/lp",
    "predictivdisplay",
    "zoologyfibre",
    "meritvolleyball",
    "doubleclick.net",
    "googlesyndication",
    "popads",
    "popcash",
    "exoclick",
    "propellerads",
    "juicyads",
    "trafficjunky",
    "tsyndicate",
    "adnxs.com",
    "onclicka",
    "onclickgenius",
    "ad-delivery",
    "rampidads",
    "pl02",
)

class FilecryptError(Exception):
    """Fallo genérico procesando una carpeta de filecrypt."""


class CaptchaRequired(FilecryptError):
    """La carpeta exige captcha y no se pudo resolver en el navegador."""


class PasswordRequired(FilecryptError):
    """La carpeta pide contraseña (o la proporcionada es incorrecta)."""


def is_filecrypt(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(netloc == host or netloc.endswith(f".{host}") for host in FILECRYPT_HOSTS)


def _is_filecrypt_related(url: str) -> bool:
    if not url:
        return True
    u = url.lower()
    if u.startswith(("about:", "chrome:", "chrome-extension:", "devtools:", "data:")):
        return True
    keep = (
        "filecrypt.",
        "cutcaptcha.",
        "captcha.filecrypt",
        "pow.filecrypt",
        "static.filecrypt",
    )
    return any(k in u for k in keep)


# ---------------------------------------------------------------- aiohttp helpers


async def _fetch(
    session: aiohttp.ClientSession, url: str, data: dict | None = None
) -> tuple[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": url,
        "Accept-Language": "en-US,en;q=0.9",
    }
    # lang cookie sin pisar la jar
    try:
        has_lang = any(getattr(c, "key", None) == "lang" for c in session.cookie_jar)
    except Exception:
        has_lang = False
    if not has_lang:
        session.cookie_jar.update_cookies(
            {"lang": "en"}, response_url=URL("https://www.filecrypt.cc/")
        )
    method = session.post if data else session.get
    kwargs = {"data": data} if data else {}
    async with method(url, headers=headers, **kwargs) as resp:
        resp.raise_for_status()
        text = await resp.text()
        # persiste cualquier Set-Cookie nuevo
        jar_cookies = cookies_from_aiohttp_jar(session)
        if jar_cookies:
            save_cookies(jar_cookies)
        return text, str(resp.url)


def _has_captcha(page: str) -> bool:
    """True solo si la página es la de bloqueo (no la carpeta abierta)."""
    if _page_looks_unlocked(page):
        return False
    lowered = page.lower()
    # señales fuertes del challenge actual
    if "pow-captcha" in lowered or "pow_captcha" in lowered:
        return True
    if "security check" in lowered or "sicherheitsüberprüfung" in lowered:
        return True
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def _needs_password(tree) -> bool:
    return bool(
        tree.xpath(
            '//input[@type="password" or @name="password" or @name="pssw" '
            'or @name="password__"]'
        )
    )


def _password_payload(password: str, page: str) -> dict:
    """Detecta el name real del input de password (JD usa pssw / password__)."""
    tree = html.fromstring(page)
    for name in PASSWORD_FIELD_NAMES:
        if tree.xpath(f'//input[@name="{name}"]'):
            return {name: password}
    return {"password": password}


def decrypt_cnl(crypted: str, jk: str) -> list[str]:
    """Descifra el payload CNL2: AES-128-CBC con la clave usada también como IV."""
    match = JK_KEY_RE.search(jk) or re.fullmatch(r"[0-9a-fA-F]{16,64}", jk.strip())
    if not match:
        raise FilecryptError("no se pudo leer la clave CNL")
    key = bytes.fromhex(match.group(1) if match.re is JK_KEY_RE else jk.strip())
    payload = base64.b64decode(crypted)
    plain = AES.new(key, AES.MODE_CBC, key).decrypt(payload)
    text = plain.decode("utf-8", errors="ignore").replace("\x00", "")
    return [line.strip() for line in text.splitlines() if line.strip().startswith("http")]


def _cnl_from_page(tree, page: str) -> list[str]:
    crypted = tree.xpath('//input[@name="crypted"]/@value')
    jk = tree.xpath('//input[@name="jk"]/@value')
    if not (crypted and jk):
        # textarea / hidden genéricos
        crypted = tree.xpath('//*[@name="crypted"]/@value | //*[@name="crypted"]/text()')
        jk = tree.xpath('//*[@name="jk"]/@value | //*[@name="jk"]/text()')
    if not (crypted and jk):
        # CNLPOP style (JDownloader): form con literales 'jk','crypted',...
        for m in re.finditer(r"CNLPOP|cnlform|Click.?n.?Load", page, re.I):
            chunk = page[m.start() : m.start() + 2000]
            infos = re.findall(r"""['"]([A-Za-z0-9+/=_-]{8,})['"]""", chunk)
            # busca un hex 16-64 chars (jk) seguido de un base64 largo (crypted)
            for i, token in enumerate(infos):
                if re.fullmatch(r"[0-9a-fA-F]{16,64}", token) and i + 1 < len(infos):
                    nxt = infos[i + 1]
                    if len(nxt) > 20:
                        try:
                            links = decrypt_cnl(nxt, token)
                            if links:
                                return links
                        except Exception:
                            pass
        c_match = re.search(
            r"""crypted['"]?\s*[:=]\s*['"]([A-Za-z0-9+/=]+)['"]""", page
        )
        j_match = re.search(
            r"""jk['"]?\s*[:=]\s*['"]?(function[^;]+?|[0-9a-fA-F]{16,64})['"]?[;,]""",
            page,
        )
        if not (c_match and j_match):
            # function f(){ return 'HEX';}
            j_match = re.search(
                r"""function\s*\w*\s*\(\s*\)\s*\{\s*return\s*['"]([0-9a-fA-F]{16,64})['"]""",
                page,
            )
            if c_match and j_match:
                try:
                    return decrypt_cnl(c_match.group(1), j_match.group(1))
                except Exception:
                    return []
            return []
        crypted, jk = [c_match.group(1)], [j_match.group(1)]
    try:
        return decrypt_cnl(crypted[0], jk[0])
    except Exception:
        return []


async def _cnl_from_endpoint(
    session: aiohttp.ClientSession, base_url: str, page: str
) -> list[str]:
    match = re.search(
        r"""_CNL/([0-9A-Za-z]+)\.html|getlink\.php\?id=([0-9A-Za-z]+)""", page
    )
    if not match:
        return []
    container_id = match.group(1) or match.group(2)
    endpoint = urljoin(base_url, "/_CNL/getlink.php")
    try:
        body, _ = await _fetch(
            session, f"{endpoint}?id={container_id}", data={"id": container_id}
        )
    except Exception:
        return []
    c_match = re.search(
        r"""["']?crypted["']?\s*[:=]\s*["']([A-Za-z0-9+/=]+)["']""", body
    )
    j_match = re.search(r"""["']?jk["']?\s*[:=]\s*["']([^"']+)["']""", body)
    if not (c_match and j_match):
        return []
    try:
        return decrypt_cnl(c_match.group(1), j_match.group(1))
    except Exception:
        return []


async def _resolve_single_link(
    session: aiohttp.ClientSession, base_url: str, link_id: str
) -> str | None:
    url = urljoin(base_url, f"/Link/{link_id}.html")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": base_url,
        "Cookie": "BetterJsPopCount=1; lang=en",
    }
    try:
        async with session.get(url, headers=headers, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                target = resp.headers.get("Location")
                if target and not is_filecrypt(target):
                    return target
                if target and is_filecrypt(target):
                    m = LINK_ID_RE.search(target)
                    if m:
                        return await _resolve_single_link(session, base_url, m.group(1))
            body = await resp.text()
    except Exception:
        return None

    # Intermediate Action=Go
    go = re.search(
        r"""(["'])(https?://[^/]+/index\.php\?Action=[Gg]o[^"']+)\1""", body
    )
    if go:
        try:
            async with session.get(
                go.group(2), headers=headers, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    target = resp.headers.get("Location")
                    if target and not is_filecrypt(target):
                        return target
                body = await resp.text()
        except Exception:
            pass

    for pattern in REDIRECT_RES:
        found = pattern.search(body)
        if found and not is_filecrypt(found.group(1)):
            return found.group(1).strip()
    return None


async def _links_from_page(
    session: aiohttp.ClientSession, base_url: str, page: str
) -> list[str]:
    tree = html.fromstring(page)

    links = _cnl_from_page(tree, page) or await _cnl_from_endpoint(session, base_url, page)
    if links:
        return _dedupe(links)

    link_ids = _dedupe(OPEN_LINK_RE.findall(page) + LINK_ID_RE.findall(page))
    resolved = []
    for link_id in link_ids[:50]:
        target = await _resolve_single_link(session, base_url, link_id)
        if target:
            resolved.append(target)
    return _dedupe(resolved)


def _mirror_urls(base_url: str, page: str) -> list[str]:
    found = re.findall(
        r"""["']([^"']*Container/[A-Z0-9]+\.html\?mirror=\d+[^"']*)["']""", page, re.I
    )
    found += re.findall(r"""["'](/[^"']*mirror=\d+[^"']*)["']""", page)
    return _dedupe(urljoin(base_url, path) for path in found)


def _dedupe(items) -> list[str]:
    seen, result = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _page_looks_unlocked(page: str) -> bool:
    """Detecta la carpeta ya abierta (Click'n Load / DLC / mirrors), no el captcha."""
    low = page.lower()
    still_pow = "pow-captcha" in low or 'id="pow-captcha"' in low
    if still_pow and "security check" in low:
        return False
    if 'name="crypted"' in page or "name='crypted'" in page:
        return True
    if "click'n load" in low or "clickn load" in low or "cnlform" in low or "cnlpop" in low:
        return True
    if "downloaddlc" in low or "/dlc/" in low or re.search(r"\bdlc\b", low):
        # botón dlc de la UI desbloqueada
        if "security check" not in low:
            return True
    if OPEN_LINK_RE.search(page) and "security check" not in low and not still_pow:
        return True
    if "mirror=" in page and "security check" not in low and not still_pow:
        return True
    # UI con hoster online (rapidgator, etc.) sin captcha
    if not still_pow and "security check" not in low:
        if re.search(r"rapidgator|uploaded\.|nitroflare|katfile|ddownload", low):
            if "online" in low or "click" in low:
                return True
    return False


# ---------------------------------------------------------------- cookies de sesión


def load_saved_cookies() -> list[dict]:
    if not COOKIE_FILE.is_file():
        return []
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        log.warning("No se pudieron leer cookies de %s", COOKIE_FILE)
    return []


def save_cookies(cookies: list[dict]) -> None:
    """Guarda cookies al estilo Playwright (name/value/domain/path/...)."""
    if not cookies:
        return
    # merge por (name, domain, path)
    by_key: dict[tuple, dict] = {}
    for c in load_saved_cookies():
        key = (c.get("name"), c.get("domain"), c.get("path") or "/")
        by_key[key] = c
    for c in cookies:
        if not c.get("name"):
            continue
        key = (c.get("name"), c.get("domain"), c.get("path") or "/")
        by_key[key] = {
            "name": c["name"],
            "value": c.get("value", ""),
            "domain": c.get("domain") or ".filecrypt.cc",
            "path": c.get("path") or "/",
            "expires": c.get("expires", -1),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": c.get("sameSite") or "Lax",
        }
    COOKIE_FILE.write_text(
        json.dumps(list(by_key.values()), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.debug("Cookies filecrypt guardadas (%d) en %s", len(by_key), COOKIE_FILE)


def apply_cookies_to_session(session: aiohttp.ClientSession) -> int:
    """Inyecta cookies guardadas en la jar de aiohttp. Devuelve cuántas aplicó."""
    cookies = load_saved_cookies()
    n = 0
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        domain = (c.get("domain") or ".filecrypt.cc").lstrip(".")
        path = c.get("path") or "/"
        # url “de respuesta” para que el jar asocie dominio/path
        response_url = URL.build(scheme="https", host=domain.lstrip("."), path=path or "/")
        morsel = SimpleCookie()
        morsel[name] = value
        morsel[name]["domain"] = domain
        morsel[name]["path"] = path
        try:
            session.cookie_jar.update_cookies(morsel, response_url=response_url)
            n += 1
        except Exception:
            # fallback sin domain estricto
            try:
                session.cookie_jar.update_cookies({name: value}, response_url=URL("https://www.filecrypt.cc/"))
                n += 1
            except Exception:
                pass
    if n:
        log.debug("Aplicadas %d cookies filecrypt guardadas", n)
    return n


def cookies_from_aiohttp_jar(session: aiohttp.ClientSession) -> list[dict]:
    out = []
    try:
        for cookie in session.cookie_jar:
            out.append(
                {
                    "name": cookie.key,
                    "value": cookie.value,
                    "domain": cookie.get("domain") or ".filecrypt.cc",
                    "path": cookie.get("path") or "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
            )
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- extensions / browser


def _read_manifest_version(ext_dir: Path) -> int | None:
    manifest = ext_dir / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        import json

        data = json.loads(manifest.read_text(encoding="utf-8"))
        return int(data.get("manifest_version") or 0)
    except Exception:
        return None


def ensure_ublock(*, force: bool = False) -> Path:
    """Asegura uBlock Origin Lite (MV3) en extensions/ublock."""
    mv = _read_manifest_version(UBLOCK_DIR)
    if not force and mv is not None and mv >= UBLOCK_MIN_MANIFEST:
        return UBLOCK_DIR

    if UBLOCK_DIR.exists():
        log.info(
            "Reemplazando extensión antigua (manifest v%s) por uBlock Origin Lite MV3",
            mv,
        )
        shutil.rmtree(UBLOCK_DIR)

    EXT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = EXT_DIR / "ublock.zip"
    log.info("Descargando uBlock Origin Lite (MV3)...")
    with urlopen(UBLOCK_ZIP_URL, timeout=120) as resp:  # noqa: S310 - URL fija de GitHub
        zip_path.write_bytes(resp.read())

    tmp = EXT_DIR / "ublock_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp)
    zip_path.unlink(missing_ok=True)

    candidates = list(tmp.rglob("manifest.json"))
    if not candidates:
        raise FilecryptError("no se pudo descomprimir uBlock Origin Lite")
    src = candidates[0].parent
    # si el zip no trae subcarpeta, src == tmp
    if src.resolve() == tmp.resolve():
        UBLOCK_DIR.mkdir(parents=True, exist_ok=True)
        for item in tmp.iterdir():
            shutil.move(str(item), str(UBLOCK_DIR / item.name))
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        src.rename(UBLOCK_DIR)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    if _read_manifest_version(UBLOCK_DIR) != 3:
        raise FilecryptError("instalación de uBlock Lite incompleta (no es MV3)")
    log.info("uBlock Origin Lite listo en %s", UBLOCK_DIR)
    return UBLOCK_DIR


def _extension_paths() -> list[str]:
    paths = [str(ensure_ublock().resolve())]
    if (GUARD_DIR / "manifest.json").is_file():
        paths.append(str(GUARD_DIR.resolve()))
    return paths


async def _verify_extensions_loaded(context) -> list[str]:
    """Comprueba por CDP qué extensiones hay cargadas (service workers / backgrounds)."""
    found: list[str] = []
    try:
        # Los service workers de MV3 aparecen en context.service_workers
        for sw in getattr(context, "service_workers", []) or []:
            u = getattr(sw, "url", "") or ""
            if u.startswith("chrome-extension://"):
                found.append(u)
                log.debug("Extensión SW: %s", u)
        # A veces hay páginas de background
        for pg in list(context.pages):
            u = pg.url or ""
            if u.startswith("chrome-extension://"):
                found.append(u)
                log.debug("Extensión page: %s", u)
        # Intento extra: targets vía CDP
        try:
            client = await context.new_cdp_session(context.pages[0])
            targets = await client.send("Target.getTargets")
            for t in targets.get("targetInfos", []):
                u = t.get("url") or ""
                if u.startswith("chrome-extension://"):
                    found.append(u)
                    log.debug("Extensión target (%s): %s", t.get("type"), u)
        except Exception as exc:
            log.debug("CDP targets: %s", exc)
    except Exception:
        log.exception("No se pudieron listar extensiones")
    return _dedupe(found)


async def _strip_ad_overlays(page) -> None:
    """Quita capas de anuncios que interceptan el click del captcha."""
    try:
        await page.evaluate(
            """() => {
            const kill = (el) => { try { el.remove(); } catch (e) {} };
            for (const sel of [
                '#y0hyvr9', '#lkina', 'a#lkina',
                'iframe[src*="ad"]', 'iframe[id*="google_ads"]',
                '[class*="adsbox"]', '[id*="overlay"]'
            ]) {
                document.querySelectorAll(sel).forEach(kill);
            }
            for (const el of document.querySelectorAll('body *')) {
                const s = getComputedStyle(el);
                if (s.position !== 'fixed' && s.position !== 'absolute') continue;
                const z = parseInt(s.zIndex || '0', 10);
                if (z < 100) continue;
                const r = el.getBoundingClientRect();
                if (r.width * r.height < 20000) continue;
                // no borrar el captcha ni su ventana
                if (el.closest && (el.closest('#pow-captcha') || el.closest('#cform') || el.closest('.pow-captcha')))
                    continue;
                if (el.id === 'pow-captcha' || el.classList?.contains('pow-captcha')) continue;
                kill(el);
            }
        }"""
        )
    except Exception:
        pass


async def _click_pow_captcha(page) -> None:
    await _strip_ad_overlays(page)
    box = page.locator(".pow-captcha__box, #pow-captcha .pow-captcha__box").first
    await box.scroll_into_view_if_needed()
    await page.wait_for_timeout(200)
    bb = await box.bounding_box()
    if bb:
        await page.mouse.move(
            bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, steps=12
        )
        await page.wait_for_timeout(150)
        # force evita overlays residuales
        await box.click(force=True, timeout=10000)
    else:
        await box.click(force=True, timeout=10000)


async def _browser_unlock_folder(
    url: str, password: str | None = None
) -> tuple[str, str]:
    """Abre la carpeta en Chrome con uBlock, resuelve captcha PoW y devuelve (html, url)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise CaptchaRequired(
            "hace falta playwright para el captcha de filecrypt "
            "(pip install playwright && playwright install chromium)"
        ) from exc

    ext_paths = _extension_paths()
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    ext_arg = ",".join(ext_paths)

    async with async_playwright() as p:
        # IMPORTANTE: el Google Chrome del sistema (channel=chrome) a partir de
        # ~Chrome 137 IGNORA --load-extension. Hay que usar el Chromium de
        # Playwright (o Chrome for Testing) para que uBlock cargue de verdad.
        # Además Playwright mete --no-sandbox por defecto → lo ignoramos.
        launch_args = [
            f"--disable-extensions-except={ext_arg}",
            f"--load-extension={ext_arg}",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        ignore_defaults = [
            "--enable-automation",
            "--no-sandbox",
            "--disable-extensions",  # si no, anula las extensiones
            "--enable-unsafe-swiftshader",
        ]
        log.debug("Cargando extensiones: %s", ext_paths)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE),
            headless=False,  # las extensiones no cargan en headless clásico
            args=launch_args,
            ignore_default_args=ignore_defaults,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=USER_AGENT,
            timezone_id="Europe/Madrid",
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        # Espera a que arranquen los service workers de las extensiones MV3
        await asyncio.sleep(2.5)
        loaded = await _verify_extensions_loaded(context)
        if not loaded:
            log.warning(
                "No se detectó ninguna extensión cargada. "
                "¿Ejecutaste 'playwright install chromium'? "
                "Sigo con bloqueo de ads por código, pero sin icono de uBlock."
            )
        else:
            log.debug("Extensiones activas: %d", len(loaded))

        # Cierra pestañas de anuncios que se cuelen pese a uBlock
        async def on_page(page):
            def _maybe_close(p=page):
                async def runner():
                    try:
                        u = p.url
                        if u and not _is_filecrypt_related(u) and u != "about:blank":
                            log.debug("Cerrando pestaña de anuncio: %s", u[:120])
                            await p.close()
                    except Exception:
                        pass

                asyncio.create_task(runner())

            page.on("framenavigated", lambda _f: _maybe_close())

        context.on("page", on_page)

        page = context.pages[0] if context.pages else await context.new_page()

        # uBlock + bloqueo extra: ads y redirects fuera de filecrypt
        async def route_handler(route):
            req = route.request
            u = req.url.lower()
            if any(m in u for m in AD_HOST_MARKERS) and not _is_filecrypt_related(req.url):
                await route.abort()
                return
            if req.resource_type == "document" and req.frame == page.main_frame:
                if req.url.startswith("http") and not _is_filecrypt_related(req.url):
                    log.debug("Bloqueado redirect a %s", req.url[:120])
                    await route.abort()
                    return
            await route.continue_()

        await page.route("**/*", route_handler)

        # Da tiempo a que uBlock cargue filtros en el perfil persistente
        await page.wait_for_timeout(2000)

        log.info("Abriendo %s con uBlock...", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)

        # Password si el formulario está visible
        if password:
            for name in PASSWORD_FIELD_NAMES:
                loc = page.locator(f'input[name="{name}"]')
                if await loc.count():
                    await loc.fill(password)
                    break

        await _strip_ad_overlays(page)

        # Click captcha PoW si existe
        pow_box = page.locator(".pow-captcha__box, #pow-captcha .pow-captcha__box")
        if await pow_box.count():
            log.info("Captcha PoW detectado — haciendo click (uBlock activo)")
            try:
                await _click_pow_captcha(page)
            except Exception:
                log.exception("No se pudo clickar el captcha")
        # Espera a que desaparezca el captcha / aparezcan enlaces
        deadline = time.monotonic() + CAPTCHA_TIMEOUT_S
        final_html = None
        while time.monotonic() < deadline:
            # mata pestañas extra de anuncios
            for p in list(context.pages):
                try:
                    if p is page:
                        continue
                    u = p.url
                    if u and not _is_filecrypt_related(u):
                        await p.close()
                except Exception:
                    pass

            try:
                # si nos echaron, volver
                if page.url.startswith("http") and not _is_filecrypt_related(page.url):
                    log.warning("Salimos de filecrypt → volviendo")
                    await page.goto(url, wait_until="domcontentloaded")
                    if await pow_box.count():
                        await pow_box.first.click(force=True)

                content = await page.content()
                state = await page.evaluate(
                    """() => {
                        const el = document.getElementById('pow-captcha');
                        return el ? el.getAttribute('data-state') : null;
                    }"""
                )

                # rellena password en el form si sigue visible tras captcha
                if password and state == "done":
                    for name in PASSWORD_FIELD_NAMES:
                        loc = page.locator(f'input[name="{name}"]')
                        if await loc.count():
                            await loc.fill(password)

                if _page_looks_unlocked(content) or (
                    state is None
                    and "pow-captcha" not in content
                    and "Security Check" not in content
                    and "Sicherheits" not in content
                ):
                    # espera un pelín a CNL/render
                    await page.wait_for_timeout(1500)
                    final_html = await page.content()
                    if _has_captcha(final_html) and not (
                        'name="crypted"' in final_html or OPEN_LINK_RE.search(final_html)
                    ):
                        final_html = None
                    else:
                        break

                if state == "fail":
                    log.warning("Captcha falló, reintentando click")
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)
                    await _strip_ad_overlays(page)
                    if await page.locator(".pow-captcha__box").count():
                        await _click_pow_captcha(page)

            except Exception as exc:
                log.debug("poll: %s", exc)

            await page.wait_for_timeout(POPUP_KILL_INTERVAL_MS)

        final_url = page.url
        if not final_html:
            try:
                final_html = await page.content()
            except Exception:
                final_html = ""

        # Asegura CNL en el HTML (a veces va en forms dinámicos)
        try:
            cnl_bits = await page.evaluate(
                """() => {
                const out = {crypted: '', jk: '', htmlExtra: ''};
                const c = document.querySelector('input[name=crypted], textarea[name=crypted]');
                const j = document.querySelector('input[name=jk], textarea[name=jk]');
                if (c) out.crypted = c.value || '';
                if (j) out.jk = j.value || '';
                // formularios CNLPOP / cnlform
                for (const f of document.forms) {
                    const t = f.outerHTML || '';
                    if (/cnl|crypted|jk/i.test(t)) out.htmlExtra += t + '\\n';
                }
                return out;
            }"""
            )
            if cnl_bits:
                if cnl_bits.get("crypted") and cnl_bits.get("jk"):
                    # embebe inputs por si el content() no los trajo bien
                    final_html += (
                        f'\n<input name="crypted" value="{cnl_bits["crypted"]}"/>'
                        f'\n<input name="jk" value="{cnl_bits["jk"]}"/>\n'
                    )
                if cnl_bits.get("htmlExtra"):
                    final_html += "\n" + cnl_bits["htmlExtra"]
        except Exception:
            log.debug("No se pudo leer CNL del DOM", exc_info=True)

        # Guardar cookies de la sesión desbloqueada (lo importante)
        try:
            browser_cookies = await context.cookies()
            fc_cookies = [
                c
                for c in browser_cookies
                if "filecrypt" in (c.get("domain") or "")
                or c.get("name")
                in ("PHPSESSID", "lang", "lang_v2", "BetterJsPopCount")
            ]
            if fc_cookies:
                save_cookies(fc_cookies)
            else:
                save_cookies(browser_cookies)
            log.debug(
                "Cookies del navegador guardadas (%d filecrypt / %d total) → %s",
                len(fc_cookies),
                len(browser_cookies),
                COOKIE_FILE,
            )
        except Exception:
            log.exception("No se pudieron guardar cookies del navegador")

        await context.close()

    if not final_html or (
        not _page_looks_unlocked(final_html)
        and _has_captcha(final_html)
        and not ('name="crypted"' in final_html or OPEN_LINK_RE.search(final_html))
    ):
        raise CaptchaRequired(
            "no se pudo pasar el captcha de filecrypt (incluso con navegador + uBlock). "
            "Ábrela en Chrome con adblock y pega los enlaces."
        )
    return final_html, final_url


async def _links_from_html_string(
    session: aiohttp.ClientSession, base_url: str, page: str
) -> list[str]:
    links = await _links_from_page(session, base_url, page)
    if links:
        return links
    # DLC id → no lo desciframos localmente; al menos reportar
    dlc = re.findall(r"DownloadDLC\(['\"]([^'\"]+)['\"]", page)
    dlc += re.findall(r"/DLC/([A-Za-z0-9]+)\.dlc", page)
    if dlc:
        log.warning("La carpeta solo expone DLC (%s); sin decoder DLC local", dlc[0])
    return links


# ---------------------------------------------------------------- public API


async def get_folder_links(
    session: aiohttp.ClientSession, url: str, password: str | None = None
) -> list[str]:
    # 1) reutilizar cookies de un captcha ya pasado
    apply_cookies_to_session(session)

    page, final_url = await _fetch(session, url)
    tree = html.fromstring(page)

    if _needs_password(tree) and not _page_looks_unlocked(page) and not _has_captcha(page):
        if not password:
            raise PasswordRequired("la carpeta está protegida con contraseña")
        page, final_url = await _fetch(
            session, final_url, data=_password_payload(password, page)
        )
        tree = html.fromstring(page)
        if _needs_password(tree) and not _page_looks_unlocked(page) and not _has_captcha(page):
            raise PasswordRequired("contraseña incorrecta")

    if _has_captcha(page) and not _page_looks_unlocked(page):
        log.info("Captcha detectado → abriendo navegador")
        page, final_url = await _browser_unlock_folder(url, password)
        # reinyecta cookies recien guardadas para seguir con aiohttp
        apply_cookies_to_session(session)
        tree = html.fromstring(page)
        if _needs_password(tree):
            if not password:
                raise PasswordRequired("la carpeta está protegida con contraseña")
            page, final_url = await _fetch(
                session, final_url, data=_password_payload(password, page)
            )

    links = await _links_from_html_string(session, final_url, page)
    if not links:
        for mirror in _mirror_urls(final_url, page):
            try:
                mirror_page, mirror_url = await _fetch(session, mirror)
            except Exception:
                continue
            if _has_captcha(mirror_page) and not _page_looks_unlocked(mirror_page):
                try:
                    mirror_page, mirror_url = await _browser_unlock_folder(
                        mirror, password
                    )
                    apply_cookies_to_session(session)
                except CaptchaRequired:
                    continue
            links = await _links_from_html_string(session, mirror_url, mirror_page)
            if links:
                break

    if not links:
        raise FilecryptError("no se pudo extraer ningún enlace de la carpeta")
    return links
