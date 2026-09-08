#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.1",
#     "curl_cffi>=0.7",
#     "beautifulsoup4>=4.12",
#     "lxml>=5.0",
#     "markdownify>=0.13",
#     "markdown>=3.5",
#     "pycryptodome>=3.20",
#     "Pillow>=10.0",
#     "img2pdf>=0.5",
# ]
# ///
"""Franc-Tireur CLI — read the weekly (site + liseuse) from the terminal.

Franc-Tireur publishes the same journalism through two back-ends, and this CLI
speaks both:

  * **www.franc-tireur.fr** — a server-rendered site whose paywalled articles
    ship their body as an AES blob in `data-content-src`. Only
    `ws.profile.franc-tireur.fr/content/decrypt` can open it, and only for a
    request carrying *both* the subscriber's `cmiuser` JWT and the `lauser_token`
    shared-user cookie. Both are read straight out of a locally logged-in
    Chromium browser, so there is nothing to paste and no login to re-implement.
  * **digital.franc-tireur.fr** — the miLibris liseuse, i.e. the actual printed
    paper: page images, a PDF, and per-article text. That half is handled by the
    host-agnostic `milibris_cli` module sitting next to this file.

Site commands (`numeros`, `numero`, `read`, `search`) hit the first back-end;
paper commands (`toc`, `dump`, `pages`) delegate to the second.
"""

from __future__ import annotations

import html as htmllib
import importlib.util
import json as jsonlib
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import click
from bs4 import BeautifulSoup
from curl_cffi import requests

# The liseuse half lives in its own repo (milibris-cli) because miLibris powers
# many titles. Look for it where a local checkout plausibly sits, newest hint
# first, so a `git pull` in either repo keeps both halves in step.
MILIBRIS_CANDIDATES = (
    Path(os.environ["MILIBRIS_CLI"]).expanduser() if os.environ.get("MILIBRIS_CLI") else None,
    Path(__file__).resolve().parent.parent / "milibris-cli" / "milibris_cli.py",
    Path.home() / ".claude" / "skills" / "milibris-cli" / "milibris_cli.py",
    Path(__file__).resolve().parent / "milibris_cli.py",
)


def _load_milibris():
    """Import the milibris-cli module from the first checkout that has it."""
    for candidate in MILIBRIS_CANDIDATES:
        if candidate and candidate.is_file():
            spec = importlib.util.spec_from_file_location("milibris_cli", candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules["milibris_cli"] = module
            spec.loader.exec_module(module)
            return module
    looked = "\n  ".join(str(c) for c in MILIBRIS_CANDIDATES if c)
    raise SystemExit(
        "franc-tireur-cli needs milibris-cli (the liseuse half) and could not "
        "find it. Clone it next to this repo:\n"
        "  git clone https://github.com/ClementWalter/milibris-cli.git\n"
        f"or point $MILIBRIS_CLI at its milibris_cli.py. Looked in:\n  {looked}"
    )


milibris_cli = _load_milibris()
TLS_IMPERSONATE = milibris_cli.TLS_IMPERSONATE
UA = milibris_cli.UA
browser_cookies = milibris_cli.browser_cookies
slugify = milibris_cli.slugify
html_to_pdf = milibris_cli.html_to_pdf

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
log = logging.getLogger("ft")

SITE = "https://www.franc-tireur.fr"
KIOSK_HOST = "digital.franc-tireur.fr"
# The paywall service that opens `data-content-src`. It answers only when the
# request carries both the subscriber JWT and the shared-user token.
DECRYPT_ENDPOINT = "https://ws.profile.franc-tireur.fr/content/decrypt"
AUTH_COOKIES = ("cmiuser", "lauser_token")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def site_cookies() -> dict:
    """The subscriber cookies for franc-tireur.fr, newest browser copy wins."""
    jar = {}
    for name in AUTH_COOKIES:
        rows = browser_cookies(name, "%franc-tireur.fr")
        if rows:
            jar[name] = rows[0]["value"]
    return jar


def require_cookies() -> dict:
    jar = site_cookies()
    missing = [n for n in AUTH_COOKIES if n not in jar]
    if missing:
        raise click.ClickException(
            f"Missing franc-tireur.fr cookie(s): {', '.join(missing)}. Open "
            "https://www.franc-tireur.fr in Chrome/Arc/Brave/Edge, sign in, "
            "then re-run — the CLI reads the session from the browser."
        )
    return jar


def jwt_payload(token: str) -> dict:
    """Decode a JWT body without verifying it — it is the browser's own token."""
    import base64

    part = token.split(".")[1]
    return jsonlib.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# --------------------------------------------------------------------------- #
# Site client
# --------------------------------------------------------------------------- #
def get(url: str, **kw) -> requests.Response:
    """GET a site page through a Chrome fingerprint (CloudFront blocks curl)."""
    resp = requests.get(
        url if "://" in url else urljoin(SITE + "/", url.lstrip("/")),
        headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"},
        impersonate=TLS_IMPERSONATE, timeout=45, **kw,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"GET {url} → HTTP {resp.status_code}")
    return resp


def decrypt_body(blob: str, jar: dict) -> str:
    """Exchange an article's encrypted body for its HTML."""
    resp = requests.post(
        DECRYPT_ENDPOINT, json={"content": blob},
        headers={"User-Agent": UA, "Origin": SITE, "Referer": SITE + "/"},
        cookies=jar, impersonate=TLS_IMPERSONATE, timeout=45,
    )
    if resp.status_code == 401:
        raise click.ClickException(
            f"Paywall refused the stored session ({resp.text.strip()}). Reload "
            "https://www.franc-tireur.fr in your browser to refresh the cmiuser "
            "token — it expires daily — then re-run."
        )
    if resp.status_code >= 400:
        raise click.ClickException(f"decrypt → HTTP {resp.status_code}")
    return resp.json().get("body", "")


def text_of(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def parse_numeros(soup: BeautifulSoup) -> list[dict]:
    """Issue cards from /tous-les-numeros."""
    rows = []
    for card in soup.select(".list-publications__publication"):
        heading = card.select_one(".list-publications__heading")
        cover = card.select_one("img[data-src], img[src^=http]")
        rows.append({
            "slug": urlparse(heading["href"]).path.strip("/") if heading else None,
            "title": text_of(heading),
            "number": text_of(card.select_one(".list-publications__issue")).lstrip("N°"),
            "date": text_of(card.select_one(".list-publications__date")),
            "summary": text_of(card.select_one(".list-publications__description")),
            "cover": cover.get("data-src") or cover.get("src") if cover else None,
        })
    return rows


def parse_listing(soup: BeautifulSoup) -> list[dict]:
    """Article rows from any `listing-articles` block (numéro TOC, search)."""
    rows = []
    for item in soup.select(".listing-articles__article"):
        link = item.select_one(".listing-articles__title a")
        if not link:
            continue
        rows.append({
            "slug": urlparse(link["href"]).path.strip("/"),
            "title": text_of(link),
            "format": text_of(item.select_one(".listing-articles__format")),
            "authors": [text_of(a) for a in item.select(".listing-articles__writer-link")],
            "headline": text_of(item.select_one(".listing-articles__headline")),
        })
    return rows


def parse_article(html: str, jar: dict | None = None) -> dict:
    """Extract one site article, decrypting the paywalled body when needed."""
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("article.article") or soup
    body = node.select_one(".js-article-body")
    blob = body.get("data-content-src") if body else None
    if blob:
        body_html = decrypt_body(blob, jar or require_cookies())
    else:
        body_html = body.decode_contents() if body else ""
    figure = node.select_one("img.article__figure")
    time_node = node.select_one(".article__date time")
    breadcrumb = soup.select(".breadcrumb__link")
    issue = breadcrumb[-1] if len(breadcrumb) > 2 else None
    return {
        "title": text_of(node.select_one(".article__title")),
        "format": text_of(node.select_one(".article__format")),
        "date": time_node.get("datetime") if time_node else None,
        "authors": [text_of(a) for a in node.select(".article__writer-link")],
        "headline": text_of(node.select_one(".article__headline")),
        "issue": text_of(issue),
        "issue_slug": urlparse(issue["href"]).path.strip("/") if issue else None,
        "image": figure.get("data-src") if figure else None,
        "premium": bool(blob),
        "body_html": body_html,
    }


def article_to_markdown(article: dict, url: str | None = None) -> str:
    """Render a site article as Markdown with YAML frontmatter."""
    front = {
        "title": article["title"],
        "format": article["format"] or None,
        "publication": "Franc-Tireur",
        "issue": article["issue"] or None,
        "date": article["date"],
        "authors": article["authors"] or None,
        "url": url,
    }
    lines = ["---"]
    for key, value in front.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(jsonlib.dumps(v, ensure_ascii=False) for v in value)}]")
        else:
            lines.append(f"{key}: {jsonlib.dumps(value, ensure_ascii=False)}")
    lines += ["---", "", f"# {article['title']}", ""]
    if article["authors"]:
        lines += ["*par " + ", ".join(article["authors"]) + "*", ""]
    if article["headline"]:
        lines += [f"**{article['headline']}**", ""]
    if article["image"]:
        lines += [f"![]({article['image']})", ""]
    lines += [html_to_body_markdown(article["body_html"]), ""]
    return "\n".join(lines)


def html_to_body_markdown(body_html: str) -> str:
    """Article body HTML to Markdown, keeping paragraph breaks."""
    from markdownify import markdownify

    md = markdownify(htmllib.unescape(body_html or ""), heading_style="ATX")
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# --------------------------------------------------------------------------- #
# HTML / PDF rendering
# --------------------------------------------------------------------------- #
# Responsive screen stylesheet — a centred reading column in relative units, so
# the saved file reflows on a phone; honours the reader's dark-mode setting.
_HTML_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: Georgia, "Times New Roman", serif; max-width: 42rem;
       margin: 0 auto; padding: 1.3rem 1.1rem 4rem; font-size: 1.15rem;
       line-height: 1.7; color: #1a1a1a; background: #fff;
       -webkit-text-size-adjust: 100%; }
h1 { font-size: 1.95rem; line-height: 1.2; margin: 0 0 0.3em; }
.meta { color: #666; font-size: 0.8rem; letter-spacing: 0.03em;
        text-transform: uppercase; margin: 0 0 1.4em;
        font-family: -apple-system, Helvetica, Arial, sans-serif; }
.lead { font-size: 1.25rem; font-style: italic; color: #333; line-height: 1.45;
        margin: 0 0 1.5em; }
h2 { font-size: 1.4rem; margin: 1.6em 0 0.4em; }
p { margin: 0 0 1em; }
img { max-width: 100%; height: auto; border-radius: 4px; margin: 1em 0; }
blockquote { border-left: 3px solid #ddd; margin: 0 0 1em; padding-left: 1em;
             color: #555; }
a { color: #0645ad; }
@media (prefers-color-scheme: dark) {
  body { background: #16161a; color: #e7e7e7; }
  .meta { color: #9aa1ad; } .lead { color: #cfcfcf; }
  blockquote { border-color: #444; color: #b5b5b5; } a { color: #7aa7ff; }
}
"""

# Fixed-page stylesheet for printing/sharing.
_PDF_CSS = """
@page { size: A4; margin: 2.2cm 2cm; }
body { font-family: Georgia, "Times New Roman", serif; font-size: 11.5pt;
       line-height: 1.55; color: #1a1a1a; }
h1 { font-size: 22pt; line-height: 1.2; margin: 0 0 0.25em; }
.meta { color: #666; font-size: 9.5pt; letter-spacing: 0.03em;
        text-transform: uppercase; margin: 0 0 1.3em;
        font-family: -apple-system, Helvetica, Arial, sans-serif; }
.lead { font-size: 13pt; font-style: italic; color: #333; margin: 0 0 1.5em; }
h2 { font-size: 15pt; margin: 1.4em 0 0.4em; }
p { margin: 0 0 0.9em; text-align: justify; }
blockquote { border-left: 3px solid #ddd; margin: 0 0 1em; padding-left: 1em;
             color: #555; }
img { max-width: 100%; height: auto; }
"""


def build_article_html(article: dict, css: str, viewport: bool) -> str:
    """A standalone reader-style document: title, byline, lead, body."""
    esc = htmllib.escape
    meta = " · ".join(x for x in (article["format"], ", ".join(article["authors"]),
                                  (article["date"] or "")[:10], article["issue"]) if x)
    head = [f"<h1>{esc(article['title'])}</h1>"]
    if meta:
        head.append(f'<p class="meta">{esc(meta)}</p>')
    if article["headline"]:
        head.append(f'<p class="lead">{esc(article["headline"])}</p>')
    if article["image"]:
        head.append(f'<img src="{esc(article["image"])}" alt="">')
    viewport_tag = ('<meta name="viewport" content="width=device-width, initial-scale=1">'
                    if viewport else "")
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        f"{viewport_tag}<title>{esc(article['title'])}</title>"
        f"<style>{css}</style></head><body>"
        + "".join(head) + htmllib.unescape(article["body_html"] or "")
        + "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Kiosk delegation
# --------------------------------------------------------------------------- #
def kiosk() -> milibris_cli.Kiosk:
    return milibris_cli.Kiosk(KIOSK_HOST)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
json_option = click.option("--json", "as_json", is_flag=True, help="Emit JSON.")


def emit(data, as_json: bool, render) -> None:
    if as_json:
        click.echo(jsonlib.dumps(data, ensure_ascii=False, indent=2))
    else:
        render(data)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option("1.0.0", prog_name="ft")
def cli() -> None:
    """Read Franc-Tireur — the site and the liseuse — from the terminal.

    \b
    Site (www.franc-tireur.fr), paywall included:
      ft numeros -n 10                 # recent issues
      ft numero latest                 # this week's TOC
      ft read le-pire-de-lfi-fait-sa-rentree
      ft read <slug> --html -o /tmp/a.html
      ft search "melenchon" --json
    \b
    Paper (digital.franc-tireur.fr, the miLibris liseuse):
      ft toc                           # TOC of the latest issue
      ft pdf                           # readable PDF, typeset from the text
      ft pdf --facsimile               # the page scans instead
      ft dump                          # pages + both PDFs + per-article Markdown
      ft page 1 -o cover.jpg
    """


@cli.command()
@json_option
def whoami(as_json: bool) -> None:
    """Show which subscriber account the browser session belongs to."""
    jar = site_cookies()
    status = {"site_cookies": sorted(jar), "kiosk_host": KIOSK_HOST}
    if "cmiuser" in jar:
        user = jwt_payload(jar["cmiuser"]).get("user", {})
        status |= {"user_id": user.get("id"), "customer_id": user.get("cid"),
                   "subscription": user.get("p_sub")}
    rows = browser_cookies("content_user_type", "%franc-tireur.fr")
    status["user_type"] = rows[0]["value"] if rows else None
    try:
        status["kiosk_authenticated"] = kiosk().authenticated()
    except click.ClickException:
        status["kiosk_authenticated"] = False
    emit(status, as_json, lambda d: [
        click.echo(f"user {d.get('user_id')} ({d.get('user_type') or 'unknown'}), "
                   f"subscription {d.get('subscription')}"),
        click.echo(f"site cookies: {', '.join(d['site_cookies']) or 'none'}"),
        click.echo(f"liseuse: {'authenticated' if d['kiosk_authenticated'] else 'logged out'}")])


@cli.command()
@click.option("-n", "--limit", default=20, show_default=True, help="How many issues.")
@json_option
def numeros(limit: int, as_json: bool) -> None:
    """List the published issues, newest first."""
    rows, page = [], 1
    while len(rows) < limit:
        # Page 1 is the bare path; `?p=1` is not a route the site serves.
        params = {"p": page} if page > 1 else None
        batch = parse_numeros(BeautifulSoup(
            get("/tous-les-numeros", params=params).text, "lxml"))
        if not batch:
            break
        rows.extend(batch)
        page += 1
    rows = rows[:limit]
    emit(rows, as_json, lambda data: [
        click.echo(f"N°{r['number']:<5} {r['date']:<26} {r['title']}  [{r['slug']}]")
        for r in data])


@cli.command()
@click.argument("issue", default="latest")
@json_option
def numero(issue: str, as_json: bool) -> None:
    """Show one issue's cover story and its table of contents.

    ISSUE is the issue slug (`lemprise-LFI`), a full URL, or `latest`.
    """
    if issue in ("latest", "last"):
        listing = parse_numeros(BeautifulSoup(get("/tous-les-numeros").text, "lxml"))
        if not listing:
            raise click.ClickException("Could not read /tous-les-numeros")
        issue = listing[0]["slug"]
    soup = BeautifulSoup(get(f"/{issue.strip('/')}").text, "lxml")
    info = {
        "slug": issue.strip("/"),
        "number": text_of(soup.select_one(".publication-main__number")).lstrip("N°"),
        "date": text_of(soup.select_one(".publication-main__date")),
        "title": text_of(soup.select_one(".publication-main__title")),
        "summary": text_of(soup.select_one(".publication-main__summary")),
        "articles": parse_listing(soup),
    }
    emit(info, as_json, lambda d: [
        click.echo(f"N°{d['number']} — {d['date']} — {d['title']}"),
        click.echo(d["summary"]),
        click.echo(""),
        *[click.echo(f"{(a['format'] or ''):<24} {a['title']}  [{a['slug']}]")
          for a in d["articles"]]])


@cli.command()
@click.argument("ref")
@click.option("-o", "--out", type=click.Path(), help="Write here; a .html/.pdf "
              "suffix implies --html/--pdf.")
@click.option("--html", "as_html", is_flag=True, help="Responsive standalone HTML.")
@click.option("--pdf", "as_pdf", is_flag=True, help="Reader-style A4 PDF.")
@json_option
def read(ref: str, out: str, as_html: bool, as_pdf: bool, as_json: bool) -> None:
    """Render one site article (paywalled body included) as Markdown.

    REF is the article slug or its full franc-tireur.fr URL.
    """
    url = ref if "://" in ref else f"{SITE}/{ref.strip('/')}"
    article = parse_article(get(url).text)
    if out:
        as_html = as_html or out.endswith(".html")
        as_pdf = as_pdf or out.endswith(".pdf")
    if as_json:
        click.echo(jsonlib.dumps(article, ensure_ascii=False, indent=2))
        return

    stem = slugify(article["title"])
    if as_pdf:
        target = Path(out) if out else Path(f"{stem}.pdf")
        html_to_pdf(build_article_html(article, _PDF_CSS, viewport=False), target,
                    base_url=SITE)
    elif as_html:
        target = Path(out) if out else Path(f"{stem}.html")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(build_article_html(article, _HTML_CSS, viewport=True))
    else:
        text = article_to_markdown(article, url)
        if not out:
            click.echo(text)
            return
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    click.echo(str(target))


@cli.command()
@click.argument("query")
@click.option("-n", "--limit", default=20, show_default=True, help="How many results.")
@json_option
def search(query: str, limit: int, as_json: bool) -> None:
    """Search the site's articles."""
    soup = BeautifulSoup(get("/recherche", params={"s": query}).text, "lxml")
    rows = parse_listing(soup)[:limit]
    emit(rows, as_json, lambda data: [
        click.echo(f"{(r['format'] or ''):<24} {r['title']}  [{r['slug']}]")
        for r in data])


@cli.command()
@click.argument("issue", required=False)
@click.option("--rubric", help="Keep only articles in this rubric.")
@json_option
def toc(issue: str, rubric: str, as_json: bool) -> None:
    """Table of contents of the printed issue, from the liseuse."""
    rows = kiosk().resolve(issue).toc()
    if rubric:
        rows = [r for r in rows if rubric.lower() in (r["rubric"] or "").lower()]
    emit(rows, as_json, lambda data: [
        click.echo(f"{r['id']}  p.{r['page']!s:<3} {(r['rubric'] or ''):<22} {r['title']}")
        for r in data])


@cli.command()
@click.argument("issue", required=False)
@click.option("-o", "--out", type=click.Path(), help="Output directory "
              "[default: ./dump/franc-tireur/<date>].")
@click.option("--hd/--ld", default=True, show_default="--hd", help="Page renders: "
              "HD stitches the tilesets (the liseuse maximum); LD is a quarter of "
              "the pixels and is not readable in print.")
@click.option("--page", "only_page", type=int, help="Dump a single page.")
@click.option("--no-pdf", is_flag=True, help="Skip both PDFs.")
@click.option("--no-articles", is_flag=True, help="Skip the per-article Markdown "
              "(and, with it, the readable PDF).")
@click.pass_context
def dump(ctx, issue, out, hd, only_page, no_pdf, no_articles) -> None:
    """Archive a printed issue: page JPEGs, two PDFs, per-article Markdown.

    Writes a facsimile PDF (page scans, capped at the liseuse's resolution) and
    a readable PDF typeset from the article text.
    """
    ctx.invoke(milibris_cli.dump, issue=issue, out=out, hd=hd, only_page=only_page,
               no_pdf=no_pdf, no_articles=no_articles, host=KIOSK_HOST)


@cli.command()
@click.argument("issue", required=False)
@click.option("-o", "--out", type=click.Path(), help="Output PDF path.")
@click.option("--facsimile", is_flag=True, help="Assemble the page scans instead "
              "of typesetting the text.")
@click.option("--hd/--ld", default=True, show_default="--hd",
              help="Facsimile only: which page renders to assemble.")
@click.pass_context
def pdf(ctx, issue, out, facsimile, hd) -> None:
    """Build a readable PDF of a printed issue, typeset from the article text.

    The liseuse serves at most ~1400x2050 pixels per printed sheet, so the page
    scans are barely legible on a broadsheet. This typesets the text instead —
    readable, searchable and small. Pass --facsimile for the scans.
    """
    ctx.invoke(milibris_cli.pdf, issue=issue, out=out, facsimile=facsimile, hd=hd,
               host=KIOSK_HOST)


@cli.command()
@click.argument("number", type=int)
@click.argument("issue", required=False)
@click.option("-o", "--out", type=click.Path(), help="Output JPEG path.")
@click.option("--hd", is_flag=True, help="Stitch the HD tileset.")
def page(number: int, issue: str, out: str, hd: bool) -> None:
    """Save one printed page as a JPEG."""
    target = kiosk().resolve(issue)
    spec = next((p for p in target.material["pages"] if p["number"] == number), None)
    if not spec:
        raise click.ClickException(f"Issue has no page {number}")
    path = Path(out) if out else Path(f"page-{number:03d}.jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(target.page_image(spec, hd=hd))
    click.echo(str(path))


# Provider commands share the app and MCP execution policy.
from pathlib import Path as _PolicyPath
import sys as _policy_sys
_policy_sys.path.insert(0, str(_PolicyPath(__file__).resolve().parent))
from onebrain_policy import install as _install_onebrain_policy
_install_onebrain_policy(cli, 'franc-tireur')

if __name__ == "__main__":
    cli()
