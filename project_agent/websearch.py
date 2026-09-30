from __future__ import annotations

import urllib.parse
from html.parser import HTMLParser

import httpx


def unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query:
        return urllib.parse.unquote(query["uddg"][0])
    return href


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._href: str | None = None
        self._mode: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = {key: value for key, value in attrs}
        classes = set((attr.get("class") or "").split())
        if tag == "a" and "result-link" in classes:
            self._href = attr.get("href") or ""
            self._mode = "title"
            self._buf = []
        elif tag == "td" and "result-snippet" in classes:
            self._mode = "snippet"
            self._buf = []

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a":
            title = " ".join("".join(self._buf).split())
            url = unwrap_ddg(self._href or "")
            if title and url.startswith("http"):
                self.results.append({"title": title, "url": url, "snippet": ""})
            self._href = None
            self._mode = None
            self._buf = []
        elif self._mode == "snippet" and tag == "td":
            snippet = " ".join("".join(self._buf).split())
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet
            self._mode = None
            self._buf = []

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)


def parse_ddg(html: str) -> list[dict]:
    parser = _ResultParser()
    parser.feed(html)
    return parser.results


def web_search(query: str, limit: int = 5) -> list[dict]:
    query = " ".join((query or "").split())
    if not query:
        raise ValueError("пустой запрос")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ProjectAgent/1.0)"}
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        response = client.get("https://lite.duckduckgo.com/lite/", params={"q": query}, headers=headers)
        response.raise_for_status()
    return parse_ddg(response.text)[:limit]
