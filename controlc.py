"""Extracción de enlaces de pastes de controlc.com sin renderizar JavaScript."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import aiohttp
from lxml import html

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

# Los pastes suelen listar varios mirrors del mismo archivo: se queda con el
# primer host de esta lista que aparezca; si ninguno aparece, devuelve todos.
PASTE_HOST_PRIORITY = ["rapidgator.net", "katfile.com"]


async def _fetch(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        return await resp.text()


def _visible_text(element) -> str:
    for hidden in element.xpath(
        './/*[contains(@style, "display:none") or contains(@style, "display: none")]'
    ):
        hidden.drop_tree()  # a diferencia de remove(), conserva el texto posterior al nodo
    return element.text_content()


def extract_links(text: str) -> list[str]:
    links: list[str] = []
    for match in URL_RE.findall(text):
        link = match.rstrip(").,;")
        if link not in links:
            links.append(link)
    for host in PASTE_HOST_PRIORITY:
        preferred = [link for link in links if host in link]
        if preferred:
            return preferred
    return links


async def get_paste_links(session: aiohttp.ClientSession, url: str) -> list[str]:
    page = await _fetch(session, url)
    tree = html.fromstring(page)

    iframe_src = tree.xpath('//*[@id="pasteFrame"]/@src')
    if iframe_src:
        page = await _fetch(session, urljoin(url, iframe_src[0]))
        tree = html.fromstring(page)

    paste = tree.xpath('//*[@id="thepaste"]')
    if paste:
        text = _visible_text(paste[0])
    else:
        body = tree.body if tree.body is not None else tree
        text = _visible_text(body)
    return extract_links(text)
