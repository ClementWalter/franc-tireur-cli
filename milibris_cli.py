#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.1",
#     "curl_cffi>=0.7",
#     "beautifulsoup4>=4.12",
#     "lxml>=5.0",
#     "markdownify>=0.13",
#     "pycryptodome>=3.20",
#     "Pillow>=10.0",
#     "img2pdf>=0.5",
# ]
# ///
"""miLibris web-kiosk CLI — read any miLibris-powered digital kiosk from the shell.

miLibris hosts the "liseuse" (e-paper reader) of dozens of French titles behind
per-publisher hosts (digital.franc-tireur.fr, …). Every one of them runs the
same two pieces:

  * a server-rendered *kiosk* (catalogue, issue pages, library, search) whose
    only credential is an Express session cookie named `<name>WebKioskSessionKey`;
  * the shared *HTML5 reader*, which pulls a `material.json` manifest plus page
    images and per-article JSON from https://content.milibris.com/access/html5-reader/
    under a short-lived ticket JWT minted by the kiosk.

So this CLI is host-agnostic: it discovers kiosks by scanning the local Chromium
cookie stores for that cookie name, then speaks the two protocols above. It is
also importable — franc_tireur_cli builds on `Kiosk` / `Issue` for its `dump`.

The reader ships the manifest either as plain JSON or as CryptoJS-AES ciphertext
keyed on the request's own session id; both are handled transparently.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json as jsonlib
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

import click
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Hash import MD5, SHA256
from Crypto.Protocol.KDF import PBKDF2
from curl_cffi import requests
from markdownify import markdownify

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
log = logging.getLogger("milibris")

# The reader's content CDN. Every asset path from material.json is relative to
# `{CONTENT_ENDPOINT}{ticket}/`.
CONTENT_ENDPOINT = "https://content.milibris.com/access/html5-reader/"
# CloudFront in front of the kiosks drops non-browser TLS handshakes.
TLS_IMPERSONATE = "chrome131"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CONFIG_PATH = Path.home() / ".config" / "milibris-cli" / "config.json"

# Chromium cookie stores this CLI knows how to open, in preference order:
# (label, cookie sqlite path, keychain service holding the AES key)
COOKIE_SOURCES = [
    ("chrome", "~/Library/Application Support/Google/Chrome/Default/Cookies", "Chrome Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Profile 1/Cookies", "Chrome Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Profile 2/Cookies", "Chrome Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Profile 3/Cookies", "Chrome Safe Storage"),
    ("arc", "~/Library/Application Support/Arc/User Data/Default/Cookies", "Arc Safe Storage"),
    ("brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies", "Brave Safe Storage"),
    ("edge", "~/Library/Application Support/Microsoft Edge/Default/Cookies", "Microsoft Edge Safe Storage"),
]

# The kiosk marks its Express session with this suffix, prefixed by the kiosk
# name (`franctireurWebKioskSessionKey`). Matching on the suffix is what makes
# kiosk discovery work without knowing the publisher up front.
KIOSK_COOKIE_SUFFIX = "WebKioskSessionKey"

# Kiosk routes that are never a title slug.
RESERVED_PATHS = {"reader", "library", "search", "auth", "consent", "css", "js",
                  "img", "catalog", "share", "account"}


# --------------------------------------------------------------------------- #
# Browser cookie extraction
# --------------------------------------------------------------------------- #
def _keychain_key(service: str) -> bytes | None:
    """Derive the AES key Chromium uses on macOS: PBKDF2(keychain secret)."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return PBKDF2(
        proc.stdout.strip().encode(), b"saltysalt", 16, count=1003,
        prf=lambda p, s: hmac.new(p, s, hashlib.sha1).digest(),
    )


def _decrypt_cookie(encrypted: bytes, key: bytes) -> str | None:
    """Decrypt a Chromium `v10`/`v11` AES-CBC cookie value."""
    if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
        return None
    try:
        dec = AES.new(key, AES.MODE_CBC, iv=b" " * 16).decrypt(encrypted[3:])
        dec = dec[: -dec[-1]]  # strip PKCS7 padding
        try:
            return dec.decode()
        except UnicodeDecodeError:
            # Chrome >= M118 prefixes a 32-byte sha256(domain); skip it.
            return dec[32:].decode(errors="replace")
    except (ValueError, IndexError):
        return None


def browser_cookies(name_like: str, host_like: str = "%") -> list[dict]:
    """Return every local Chromium cookie matching a name/host SQL LIKE pattern."""
    found = []
    for label, path, service in COOKIE_SOURCES:
        store = Path(path).expanduser()
        if not store.exists():
            continue
        key = _keychain_key(service)
        if not key:
            continue
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "Cookies"
            try:
                shutil.copy2(store, tmp)  # copy: the live DB is locked by the browser
                con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
                rows = con.execute(
                    "SELECT host_key, name, encrypted_value FROM cookies "
                    "WHERE name LIKE ? AND host_key LIKE ?",
                    (name_like, host_like),
                ).fetchall()
                con.close()
            except (OSError, sqlite3.Error):
                continue
        for host, cname, enc in rows:
            value = _decrypt_cookie(bytes(enc), key)
            if value:
                found.append({"browser": label, "host": host.lstrip("."),
                              "name": cname, "value": value})
    return found


def discover_kiosks() -> list[dict]:
    """Return the miLibris kiosks the local browsers hold a session for."""
    seen, kiosks = set(), []
    for row in browser_cookies(f"%{KIOSK_COOKIE_SUFFIX}"):
        if row["host"] in seen:
            continue
        seen.add(row["host"])
        kiosks.append(row)
    return kiosks


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return jsonlib.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(jsonlib.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # holds a session cookie


def resolve_host(host: str | None) -> str:
    """Pick the kiosk host: explicit flag, then $MILIBRIS_HOST, then config,
    then the only kiosk the browsers know about."""
    if host:
        return urlparse(host).netloc or host
    env = os.environ.get("MILIBRIS_HOST")
    if env:
        return urlparse(env).netloc or env
    cfg = load_config()
    if cfg.get("host"):
        return cfg["host"]
    kiosks = discover_kiosks()
    if len(kiosks) == 1:
        return kiosks[0]["host"]
    if not kiosks:
        raise click.ClickException(
            "No miLibris kiosk session found in Chrome/Arc/Brave/Edge. Log into "
            "your kiosk (e.g. https://digital.franc-tireur.fr) in the browser, "
            "then re-run. Pass --host to name it explicitly."
        )
    names = ", ".join(k["host"] for k in kiosks)
    raise click.ClickException(f"Several kiosks available ({names}) — pass --host.")


# --------------------------------------------------------------------------- #
# Manifest decryption
# --------------------------------------------------------------------------- #
def _material_salt() -> str:
    """Rebuild the PBKDF2 salt the reader hides as base64 of '+'-joined codes."""
    obfuscated = (
        "OTcrNTErNTUrNTIrNTMrNDgrNTQrNTArMTE5KzY5KzU3KzEwOSsxMDMrNTcrMTEyKzExNys"
        "1MCs3NSs4MysxMDkrMTEyKzUzKzEwOCsxMDQrNTArMTAxKzU0Kzk4Kzk3KzUzKzU0KzEwMQ=="
    )
    chars = [chr(int(n)) for n in base64.b64decode(obfuscated).decode().split("+")]
    return "".join(chars[8: len(chars) - 8])


MATERIAL_SALT = _material_salt()


def _evp_bytes_to_key(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey(MD5) — the KDF behind CryptoJS.AES.decrypt(ct, pass)."""
    out, block = b"", b""
    while len(out) < 48:
        block = MD5.new(block + passphrase + salt).digest()
        out += block
    return out[:32], out[32:48]


def decrypt_material(ciphertext_b64: str, session_id: str, ticket: str,
                     day: str | None = None) -> dict:
    """Decrypt an encrypted material.json body.

    The passphrase is `<session id>_<UTC day>_<ticket>` stretched through
    PBKDF2-SHA256, hex-encoded, then fed to CryptoJS's OpenSSL-compatible
    AES envelope — so the manifest is only readable by the request that asked
    for it, on the day it asked.
    """
    day = day or f"{dt.date.today():%Y-%m-%d}"
    password = f"{session_id}_{day}_{ticket}".encode()
    passphrase = PBKDF2(password, MATERIAL_SALT.encode(), 32, count=1000,
                        hmac_hash_module=SHA256).hex().encode()
    raw = base64.b64decode(ciphertext_b64)
    if raw[:8] != b"Salted__":
        raise ValueError("material is neither JSON nor a salted AES envelope")
    key, iv = _evp_bytes_to_key(passphrase, raw[8:16])
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:])
    return jsonlib.loads(plain[: -plain[-1]])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(text: str, limit: int = 80) -> str:
    """ASCII, lowercase, dash-joined — safe as a file name."""
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", norm.lower())).strip("-")[:limit] or "sans-titre"


def html_to_markdown(fragment: str) -> str:
    """Inline HTML (entities, <em>, <strong>) to Markdown, on one line."""
    md = markdownify(fragment or "", strip=["a"])
    return re.sub(r"\s+", " ", md).strip()


FR_MONTHS = {m: i for i, m in enumerate(
    ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
     "septembre", "octobre", "novembre", "décembre"], start=1)}


def parse_fr_date(label: str) -> str | None:
    """ISO date out of a French legend like `N°250 - mercredi 26 août 2026`."""
    found = re.search(r"(\d{1,2})\s+([a-zéûôA-Z]+)\s+(\d{4})", label or "")
    if not found or found.group(2).lower() not in FR_MONTHS:
        return None
    day, month, year = int(found.group(1)), FR_MONTHS[found.group(2).lower()], int(found.group(3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def kiosk_config(html: str) -> dict:
    """The string fields of the page's `window.mlKiosk.config` object."""
    block = re.search(r"window\.mlKiosk\.config\s*=\s*\{(.*?)\n\s*\};", html, re.DOTALL)
    if not block:
        return {}
    return {k: v for k, v in re.findall(r"(\w+):\s*'([^']*)'", block.group(1))}


MID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_ref(ref: str | None) -> dict:
    """Classify an issue ref as {host?, mid?, path?, slug?}.

    Accepts a reader URL, a kiosk issue URL, a bare issue mid, an issue slug
    (`n250-2026`), a `title/version/slug` path, or `latest`/None.
    """
    if not ref or ref == "latest":
        return {}
    if "://" in ref:
        url = urlparse(ref)
        parts = [p for p in url.path.split("/") if p]
        if parts and parts[0] == "reader" and len(parts) > 1:
            return {"host": url.netloc, "mid": parts[1]}
        return {"host": url.netloc, "path": "/".join(parts)}
    if MID_RE.match(ref):
        return {"mid": ref}
    if "/" in ref:
        return {"path": ref.strip("/")}
    return {"slug": ref}


def tile_names(hd: dict) -> list[tuple[int, int, str]]:
    """Yield (column, row, relative path) for an HD page's tileset.

    A 1x1 tileset is stored as the image itself rather than under a directory.
    """
    cols, rows = hd["tile_col_count"], hd["tile_row_count"]
    if cols == 1 and rows == 1:
        return [(0, 0, hd["path"])]
    return [(c, r, f"{hd['path']}/tile0{c}x0{r}.jpeg")
            for c in range(cols) for r in range(rows)]


def _opens_with(article: dict, abstract: str) -> bool:
    """Whether the body's first paragraph already carries the abstract's text."""
    for section in article.get("content", {}).get("sections", []):
        for item in section.get("items", []):
            if item.get("type") == "text" and item.get("content"):
                return html_to_markdown(item["content"]).startswith(abstract[:120])
    return False


def article_to_markdown(article: dict, meta: dict | None = None,
                        asset_url=None) -> str:
    """Render one reader article JSON as Markdown with YAML frontmatter."""
    meta = meta or {}
    front = {
        "title": article.get("title", ""),
        "surtitle": article.get("surtitle") or None,
        "subtitle": article.get("subtitle") or None,
        "publication": meta.get("title"),
        "issue": meta.get("issue_number"),
        "date": meta.get("publication_date"),
        "page": article.get("start_page"),
        "rubrics": article.get("rubrics") or None,
        "authors": article.get("authors") or None,
        "words": article.get("word_count"),
        "reading_time": article.get("reading_time"),
    }
    lines = ["---"]
    for key, value in front.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(jsonlib.dumps(v, ensure_ascii=False) for v in value)}]")
        else:
            lines.append(f"{key}: {jsonlib.dumps(value, ensure_ascii=False)}")
    lines += ["---", ""]

    if article.get("surtitle"):
        lines += [f"*{html_to_markdown(article['surtitle'])}*", ""]
    lines += [f"# {html_to_markdown(article.get('title', ''))}", ""]
    if article.get("subtitle"):
        lines += [f"## {html_to_markdown(article['subtitle'])}", ""]
    if article.get("authors"):
        lines += ["*" + " — ".join(article["authors"]) + "*", ""]
    abstract = html_to_markdown(article.get("abstract") or "")
    if abstract and not _opens_with(article, abstract):
        lines += [f"**{abstract}**", ""]

    def render(items: list) -> None:
        nonlocal lines
        for item in items:
            kind = item.get("type")
            if kind == "section":
                render(item.get("items", []))
            elif kind == "image":
                url = item.get("url") or (item.get("mid") and f"../resources/{item['mid']}.jpg")
                src = asset_url(url) if (asset_url and url) else (url or "")
                caption = html_to_markdown(item.get("caption") or "")
                lines.append(f"![{caption}]({src})")
                credit = item.get("credit")
                if caption or credit:
                    lines.append(f"*{' — '.join(x for x in (caption, credit) if x)}*")
                lines.append("")
            elif kind == "text":
                text = html_to_markdown(item.get("content", ""))
                if not text:
                    continue
                css = item.get("class")
                if css == "heading":
                    lines += [f"## {text}", ""]
                elif css == "intertitle":
                    lines += [f"### {text}", ""]
                elif css == "quote":
                    lines += [f"> {text}", ""]
                else:
                    lines += [text, ""]

    for section in article.get("content", {}).get("sections", []):
        render(section.get("items", []))

    for note in article.get("notes") or []:
        lines += [f"[^]: {html_to_markdown(note if isinstance(note, str) else jsonlib.dumps(note))}"]

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Kiosk + issue clients
# --------------------------------------------------------------------------- #
class Kiosk:
    """One miLibris kiosk host, authenticated with the browser session cookie."""

    def __init__(self, host: str, session_key: str | None = None):
        self.host = host
        self.base = f"https://{host}"
        self.cookie_name, self.session_key = self._session(host, session_key)

    @staticmethod
    def _session(host: str, override: str | None) -> tuple[str, str]:
        name = re.sub(r"[^a-z0-9]", "", host.split(".")[0]) + KIOSK_COOKIE_SUFFIX
        if override:
            return name, override
        rows = browser_cookies(f"%{KIOSK_COOKIE_SUFFIX}", f"%{host}")
        if not rows:
            raise click.ClickException(
                f"No kiosk session cookie for {host}. Open https://{host} in "
                "Chrome/Arc/Brave/Edge, log in, then re-run."
            )
        return rows[0]["name"], unquote(rows[0]["value"])

    def request(self, method: str, path: str, **kw) -> requests.Response:
        url = path if "://" in path else f"{self.base}/{path.lstrip('/')}"
        resp = requests.request(
            method, url,
            cookies={self.cookie_name: self.session_key},
            headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"},
            impersonate=TLS_IMPERSONATE, timeout=60, **kw,
        )
        if resp.status_code >= 400:
            raise click.ClickException(f"{method} {url} → HTTP {resp.status_code}")
        return resp

    def soup(self, path: str, **kw) -> BeautifulSoup:
        return BeautifulSoup(self.request("GET", path, **kw).text, "lxml")

    # -- catalogue ---------------------------------------------------------- #
    def authenticated(self) -> bool:
        """Whether the kiosk still recognises the stored session."""
        html = self.request("GET", "/").text
        return "authenticated: true" in html

    def titles(self) -> list[dict]:
        """The catalogue's titles, newest issue first.

        The kiosk names its own current title in `window.mlKiosk.config`, which
        is authoritative; the catalogue links only fill in the other titles a
        multi-title kiosk carries.
        """
        html = self.request("GET", "/").text
        rows, seen = [], set()
        current = kiosk_config(html)
        if current.get("currentTitleSlug"):
            seen.add(current["currentTitleSlug"])
            rows.append({"slug": current["currentTitleSlug"],
                         "version": current.get("currentVersionSlug", ""),
                         "latest_issue": current.get("currentIssueSlug", "")})
        for link in BeautifulSoup(html, "lxml").select('a[href^="/"]'):
            parts = [p for p in link["href"].split("?")[0].split("/") if p]
            if len(parts) != 3 or parts[0] in RESERVED_PATHS or parts[0] in seen:
                continue
            seen.add(parts[0])
            rows.append({"slug": parts[0], "version": parts[1], "latest_issue": parts[2]})
        return rows

    def issues(self, title: str | None = None, limit: int = 30) -> list[dict]:
        """Issues of a title, newest first, following the kiosk's date paging."""
        title = title or (self.titles() or [{}])[0].get("slug")
        if not title:
            raise click.ClickException("No title found in this kiosk's catalogue.")
        html = self.request("GET", f"/{title}").text
        page = BeautifulSoup(html, "lxml")
        featured = parse_featured_issue(page, kiosk_config(html))
        rows = [featured] if featured else []
        while True:
            batch = parse_issue_cards(page)
            if not batch:
                break
            rows.extend(batch)
            if len(rows) >= limit:
                break
            slugs = rows[-1]["path"].split("/")
            page = BeautifulSoup(
                self.request("POST", "/" + "/".join(slugs),
                             params={"date": rows[-1]["date"]}).text, "lxml")
        return rows[:limit]

    def search(self, query: str, issue_mid: str | None = None, page: int = 0,
               order: str = "pubDate") -> dict:
        """Full-text search across the kiosk's articles."""
        data = {"query": query, "page": page, "order": order, "phrase": "true"}
        if issue_mid:
            data["issue"] = issue_mid
        return self.request("POST", "/search/article", data=data).json()

    # -- issues ------------------------------------------------------------- #
    def resolve(self, ref: str | None) -> Issue:
        """Turn any issue ref into a live Issue (defaults to the latest one)."""
        parsed = parse_ref(ref)
        if parsed.get("mid"):
            return Issue(self, parsed["mid"])
        if parsed.get("path"):
            path = parsed["path"]
        elif parsed.get("slug"):
            title = self.titles()[0]
            path = f"{title['slug']}/{title['version']}/{parsed['slug']}"
        else:
            title = self.titles()[0]
            path = f"{title['slug']}/{title['version']}/{title['latest_issue']}"
        html = self.request("GET", "/" + path).text
        wanted = path.rsplit("/", 1)[-1]
        served = kiosk_config(html).get("currentIssueSlug")
        # An unknown slug quietly falls back to the title page, which would hand
        # back the latest issue instead — refuse rather than answer the wrong one.
        if served and wanted and served != wanted:
            raise click.ClickException(
                f"No issue '{wanted}' on {self.host} (the kiosk served "
                f"'{served}' instead). Run `milibris issues` to list them."
            )
        link = BeautifulSoup(html, "lxml").select_one('a[href^="/reader/"]')
        if not link:
            raise click.ClickException(f"No reader link on /{path} — is the issue available?")
        return Issue(self, link["href"].split("?")[0].rsplit("/", 1)[-1])


class Issue:
    """One issue: mints reader tickets and serves the decrypted manifest."""

    TICKET_RE = re.compile(r'ticket:\s*"([^"]+)"')

    def __init__(self, kiosk: Kiosk, mid: str):
        self.kiosk = kiosk
        self.mid = mid
        self._ticket: str | None = None
        self._material: dict | None = None

    @property
    def ticket(self) -> str:
        """A reader ticket, minted from the reader page (they live ~30 min)."""
        if self._ticket is None:
            html = self.kiosk.request("GET", f"/reader/{self.mid}").text
            found = self.TICKET_RE.search(html)
            if not found:
                raise click.ClickException(
                    f"No reader ticket for issue {self.mid} — the account may not "
                    "have access to it."
                )
            self._ticket = found.group(1)
        return self._ticket

    def renew(self) -> str:
        self._ticket = None
        return self.ticket

    def asset_url(self, path: str) -> str:
        """Absolute content URL for a manifest-relative asset path."""
        return f"{CONTENT_ENDPOINT}{self.ticket}/{path.lstrip('./').lstrip('/')}"

    def fetch(self, path: str, binary: bool = False):
        """GET one reader asset, re-minting the ticket once if it has expired."""
        for attempt in (1, 2):
            resp = requests.get(self.asset_url(path), headers={"User-Agent": UA},
                                impersonate=TLS_IMPERSONATE, timeout=90)
            if resp.status_code in (401, 403) and attempt == 1:
                self.renew()
                continue
            if resp.status_code >= 400:
                raise click.ClickException(f"{path} → HTTP {resp.status_code}")
            return resp.content if binary else resp.text
        raise click.ClickException(f"{path}: ticket refused twice")

    @property
    def material(self) -> dict:
        """The issue manifest: metadata, pages, articles, summary."""
        if self._material is None:
            session_id = os.urandom(8).hex()
            resp = requests.get(
                f"{CONTENT_ENDPOINT}{self.ticket}/material.json",
                headers={"User-Agent": UA, "X-Session-Id": session_id},
                impersonate=TLS_IMPERSONATE, timeout=90,
            )
            if resp.status_code >= 400:
                raise click.ClickException(f"material.json → HTTP {resp.status_code}")
            body = resp.text.strip()
            self._material = (jsonlib.loads(body) if body.startswith("{")
                              else decrypt_material(body, session_id, self.ticket))
        return self._material

    @property
    def metadata(self) -> dict:
        return self.material.get("metadata", {})

    def toc(self) -> list[dict]:
        """Articles in reading order, with their rubric from the summary."""
        rubric_of = {aid: block["rubric"]
                     for block in self.material.get("summary", [])
                     for aid in block.get("articles", [])}
        rows = []
        for aid, art in self.material.get("articles", {}).items():
            rows.append({
                "id": aid,
                "title": art.get("title", ""),
                "page": art.get("page"),
                "rubric": rubric_of.get(aid) or (art.get("rubrics") or [None])[0],
                "words": art.get("wordsCount"),
                "reading_time": art.get("readingTime"),
                "advertisement": art.get("isAdvertisement", False),
                "abstract": art.get("abstract", ""),
                "url": art.get("url"),
            })
        return sorted(rows, key=lambda r: (r["page"] or 0, r.get("id")))

    def article(self, article_id: str) -> dict:
        entry = self.material.get("articles", {}).get(article_id)
        if not entry:
            raise click.ClickException(f"No article {article_id} in issue {self.mid}")
        return jsonlib.loads(self.fetch(entry["url"]))

    def page_image(self, page: dict, hd: bool = False) -> bytes:
        """One page as JPEG bytes — the LD render, or HD tiles stitched together."""
        if not hd or not page.get("hd"):
            return self.fetch(page["ld"], binary=True)
        from io import BytesIO

        from PIL import Image

        spec = page["hd"]
        canvas = Image.new("RGB", (spec["width"], spec["height"]), "white")
        for col, row, path in tile_names(spec):
            tile = Image.open(BytesIO(self.fetch(path, binary=True)))
            canvas.paste(tile, (col * spec["tile_width"], row * spec["tile_height"]))
        buf = BytesIO()
        canvas.save(buf, "JPEG", quality=92)
        return buf.getvalue()


def parse_featured_issue(soup: BeautifulSoup, config: dict) -> dict | None:
    """The issue promoted at the top of a title page, absent from the grid."""
    link = soup.select_one('#left_column a[href^="/reader/"]')
    if not link:
        return None
    label = soup.select_one("#right_column h2")
    label = label.get_text(strip=True) if label else ""
    number = re.match(r"N°\s*(\S+)", label)
    slug = config.get("currentIssueSlug", "")
    title, version = config.get("currentTitleSlug", ""), config.get("currentVersionSlug", "")
    return {
        "path": "/".join(x for x in (title, version, slug) if x),
        "slug": slug,
        "mid": link["href"].split("?")[0].rsplit("/", 1)[-1],
        "date": parse_fr_date(label),
        "label": label,
        "number": number.group(1) if number else None,
        "available": bool(soup.select_one("#left_column .has_right")),
    }


def parse_issue_cards(soup: BeautifulSoup) -> list[dict]:
    """Extract issue rows from a kiosk catalogue page or paging fragment."""
    rows = []
    for card in soup.select(".issue_container"):
        if not card.get("data-date"):
            continue
        link = next((a for a in card.select('a[href^="/"]')
                     if len([s for s in a["href"].split("?")[0].split("/") if s]) == 3), None)
        if not link:
            continue
        path = link["href"].split("?")[0].strip("/")
        legend = card.select_one(".issue_legend")
        img = card.select_one("img[data-src]")
        mid = None
        if img:
            found = re.search(r"/issue/([0-9a-f-]{36})/", img["data-src"])
            mid = found.group(1) if found else None
        label = legend.get_text(strip=True) if legend else ""
        number = re.match(r"N°\s*(\S+)", label)
        rows.append({
            "path": path,
            "slug": path.rsplit("/", 1)[-1],
            "mid": mid,
            "date": card.get("data-date"),
            "label": label,
            "number": number.group(1) if number else None,
            "available": bool(card.select_one(".has_right")),
        })
    return rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def emit(data, as_json: bool, render=None) -> None:
    """Print JSON when asked, otherwise the human rendering."""
    if as_json:
        click.echo(jsonlib.dumps(data, ensure_ascii=False, indent=2))
    else:
        render(data)


host_option = click.option("--host", help="Kiosk host, e.g. digital.franc-tireur.fr. "
                                          "Defaults to $MILIBRIS_HOST, the config, "
                                          "or the only kiosk you are logged into.")
json_option = click.option("--json", "as_json", is_flag=True, help="Emit JSON.")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option("1.0.0", prog_name="milibris")
def cli() -> None:
    """Read any miLibris digital kiosk (liseuse) from the terminal.

    \b
    Examples:
      milibris kiosks                       # kiosks you are logged into
      milibris titles                       # catalogue of the default kiosk
      milibris issues -n 10                 # recent issues, newest first
      milibris toc                          # TOC of the latest issue
      milibris toc n249-2026 --json         # TOC of one issue, as JSON
      milibris read <article-id>            # one article as Markdown
      milibris dump --hd -o ./out           # pages + PDF + per-article Markdown
      milibris search "melenchon" --json
    """


@cli.command()
@json_option
def kiosks(as_json: bool) -> None:
    """List the miLibris kiosks your local browsers hold a session for."""
    rows = discover_kiosks()
    if not rows:
        raise click.ClickException(
            "No kiosk session found. Log into a miLibris kiosk in Chrome/Arc/"
            "Brave/Edge (e.g. https://digital.franc-tireur.fr) and re-run."
        )
    emit([{k: v for k, v in r.items() if k != "value"} for r in rows], as_json,
         lambda data: [click.echo(f"{r['host']}\t{r['browser']}\t{r['name']}")
                       for r in data])


@cli.command()
@host_option
@json_option
def whoami(host: str, as_json: bool) -> None:
    """Check that the stored kiosk session is still valid."""
    kiosk = Kiosk(resolve_host(host))
    status = {"host": kiosk.host, "cookie": kiosk.cookie_name,
              "authenticated": kiosk.authenticated()}
    emit(status, as_json, lambda d: click.echo(
        f"{d['host']}: {'authenticated' if d['authenticated'] else 'logged out'}"))
    if not status["authenticated"]:
        raise SystemExit(1)


@cli.command()
@host_option
@json_option
def titles(host: str, as_json: bool) -> None:
    """List the titles carried by a kiosk."""
    rows = Kiosk(resolve_host(host)).titles()
    emit(rows, as_json, lambda data: [
        click.echo(f"{r['slug']}/{r['version']}\tlatest: {r['latest_issue']}")
        for r in data])


@cli.command()
@click.argument("title", required=False)
@click.option("-n", "--limit", default=30, show_default=True, help="How many issues.")
@host_option
@json_option
def issues(title: str, limit: int, host: str, as_json: bool) -> None:
    """List a title's issues, newest first."""
    rows = Kiosk(resolve_host(host)).issues(title, limit)
    emit(rows, as_json, lambda data: [
        click.echo(f"{'✓' if r['available'] else '·'} {r['slug']:<14} "
                   f"{r['date'] or '':<12} {r['label']}")
        for r in data])


@cli.command()
@click.argument("issue", required=False)
@host_option
@json_option
def issue(issue: str, host: str, as_json: bool) -> None:
    """Show one issue's metadata (defaults to the latest)."""
    target = Kiosk(resolve_host(host)).resolve(issue)
    material = target.material
    info = dict(target.metadata, mid=target.mid,
                pages=len(material.get("pages", [])),
                articles=len(material.get("articles", {})))
    emit(info, as_json, lambda d: [
        click.echo(f"{d.get('title')} N°{d.get('issue_number')} — "
                   f"{d.get('publication_localized_date')}"),
        click.echo(f"{d['pages']} pages, {d['articles']} articles — mid {d['mid']}")])


@cli.command()
@click.argument("issue", required=False)
@click.option("--rubric", help="Keep only articles in this rubric (case-insensitive).")
@host_option
@json_option
def toc(issue: str, rubric: str, host: str, as_json: bool) -> None:
    """Print an issue's table of contents."""
    target = Kiosk(resolve_host(host)).resolve(issue)
    rows = target.toc()
    if rubric:
        rows = [r for r in rows if rubric.lower() in (r["rubric"] or "").lower()]
    emit(rows, as_json, lambda data: [
        click.echo(f"{r['id']}  p.{r['page']!s:<3} {(r['rubric'] or ''):<22} {r['title']}")
        for r in data])


@cli.command()
@click.argument("article_id")
@click.option("--issue", "issue_ref", help="Issue the article belongs to (default: latest).")
@click.option("-o", "--out", type=click.Path(), help="Write to this file instead of stdout.")
@host_option
@json_option
def read(article_id: str, issue_ref: str, out: str, host: str, as_json: bool) -> None:
    """Render one article as Markdown (article ids come from `toc`)."""
    target = Kiosk(resolve_host(host)).resolve(issue_ref)
    article = target.article(article_id)
    if as_json:
        click.echo(jsonlib.dumps(article, ensure_ascii=False, indent=2))
        return
    text = article_to_markdown(article, target.metadata, target.asset_url)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        log.info("wrote %s", path)
    else:
        click.echo(text)


@cli.command()
@click.argument("issue", required=False)
@click.option("-o", "--out", type=click.Path(), help="Output directory "
              "[default: ./dump/<title>/<date>].")
@click.option("--hd", is_flag=True, help="Stitch the HD tilesets instead of the LD renders.")
@click.option("--page", "only_page", type=int, help="Dump a single page.")
@click.option("--no-pdf", is_flag=True, help="Skip the assembled PDF.")
@click.option("--no-articles", is_flag=True, help="Skip the per-article Markdown.")
@host_option
def dump(issue: str, out: str, hd: bool, only_page: int, no_pdf: bool,
         no_articles: bool, host: str) -> None:
    """Archive a whole issue: page JPEGs, a PDF, and per-article Markdown."""
    target = Kiosk(resolve_host(host)).resolve(issue)
    material, meta = target.material, target.metadata
    slug = slugify(meta.get("title", "issue"))
    date = meta.get("publication_date", target.mid)
    outdir = Path(out) if out else Path("dump") / slug / date
    (outdir / "pages").mkdir(parents=True, exist_ok=True)

    pages = material.get("pages", [])
    if only_page:
        pages = [p for p in pages if p["number"] == only_page]
        if not pages:
            raise click.ClickException(f"Issue has no page {only_page}")

    written = []
    for page in pages:
        path = outdir / "pages" / f"page-{page['number']:03d}.jpg"
        path.write_bytes(target.page_image(page, hd=hd))
        written.append(path)
        log.info("page %s → %s", page["number"], path)

    if written and not no_pdf:
        logging.getLogger("pikepdf").setLevel(logging.WARNING)  # it greets on import
        import img2pdf
        pdf = outdir / f"{slug}-{date}.pdf"
        pdf.write_bytes(img2pdf.convert([str(p) for p in written]))
        log.info("pdf → %s", pdf)

    if not no_articles:
        resources = outdir / "resources"

        def localise(path: str) -> str:
            """Download an article asset once and return its relative path."""
            name = path.rsplit("/", 1)[-1]
            local = resources / name
            if not local.exists():
                resources.mkdir(parents=True, exist_ok=True)
                local.write_bytes(target.fetch(path, binary=True))
            return f"resources/{name}"

        for row in target.toc():
            article = target.article(row["id"])
            name = (f"p{row['page']:03d}-{slugify(row['title'])}.md" if row["page"]
                    else f"{slugify(row['title'])}.md")
            (outdir / name).write_text(article_to_markdown(article, meta, localise))
            log.info("article → %s", outdir / name)

    click.echo(str(outdir))


@cli.command()
@click.argument("query")
@click.option("--issue", "issue_ref", help="Restrict to one issue.")
@click.option("--page", default=0, show_default=True, help="Result page (0-based).")
@host_option
@json_option
def search(query: str, issue_ref: str, page: int, host: str, as_json: bool) -> None:
    """Full-text search the kiosk's articles."""
    kiosk = Kiosk(resolve_host(host))
    mid = kiosk.resolve(issue_ref).mid if issue_ref else None
    data = kiosk.search(query, issue_mid=mid, page=page)
    def show(payload: dict) -> None:
        for row in payload.get("results", []):
            click.echo(f"{row.get('mid', '')}  {row.get('publication_date', ''):<10} "
                       f"N°{row.get('number', ''):<5} p.{row.get('start_page', '?'):<3} "
                       f"{row.get('title', '')}")
        log.info("%s–%s of %s results", page * payload.get("count", 0) + 1,
                 page * payload.get("count", 0) + len(payload.get("results", [])),
                 payload.get("total", "?"))

    emit(data, as_json, show)


@cli.command()
@click.argument("issue", required=False)
@host_option
def material(issue: str, host: str) -> None:
    """Dump an issue's raw decrypted manifest as JSON."""
    target = Kiosk(resolve_host(host)).resolve(issue)
    click.echo(jsonlib.dumps(target.material, ensure_ascii=False, indent=2))


@cli.command("set-host")
@click.argument("host")
def set_host(host: str) -> None:
    """Remember a default kiosk host in ~/.config/milibris-cli/config.json."""
    cfg = load_config()
    cfg["host"] = urlparse(host).netloc or host
    save_config(cfg)
    click.echo(cfg["host"])


if __name__ == "__main__":
    cli()
