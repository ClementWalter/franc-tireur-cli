#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0", "click>=8.1", "curl_cffi>=0.7", "beautifulsoup4>=4.12",
#                 "lxml>=5.0", "markdownify>=0.13", "markdown>=3.5", "pycryptodome>=3.20"]
# ///
"""Unit tests for franc_tireur_cli's scraping and rendering layers. No network."""

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from franc_tireur_cli import (  # noqa: E402
    _HTML_CSS,
    article_to_markdown,
    build_article_html,
    html_to_body_markdown,
    jwt_payload,
    parse_article,
    parse_listing,
    parse_numeros,
)

NUMEROS_HTML = """
<article class="list-publications__publication">
  <img data-src="https://images.franc-tireur.fr/cover.png?w=360">
  <header class="list-publications__details">
    <span class="list-publications__issue">N°250</span>
    <time class="list-publications__date">Mercredi 26 août 2026</time>
    <a class="list-publications__heading" href="https://www.franc-tireur.fr/lemprise-LFI">
      L'emprise
    </a>
  </header>
  <p class="list-publications__description">La gauche soumise aux Insoumis.</p>
</article>
"""


@pytest.fixture
def numero_card():
    return parse_numeros(BeautifulSoup(NUMEROS_HTML, "lxml"))[0]


def test_parse_numeros_reads_slug(numero_card):
    assert numero_card["slug"] == "lemprise-LFI"


def test_parse_numeros_strips_the_number_prefix(numero_card):
    assert numero_card["number"] == "250"


def test_parse_numeros_reads_the_date(numero_card):
    assert numero_card["date"] == "Mercredi 26 août 2026"


def test_parse_numeros_reads_the_summary(numero_card):
    assert numero_card["summary"] == "La gauche soumise aux Insoumis."


LISTING_HTML = """
<article class="listing-articles__article">
  <div class="listing-articles__content">
    <span class="listing-articles__format">Le dossier de la semaine</span>
    <h3 class="listing-articles__title">
      <a href="https://www.franc-tireur.fr/le-pire-de-lfi-fait-sa-rentree">
        Le pire de LFI fait sa rentrée</a>
    </h3>
    <address class="listing-articles__writer">
      <a class="listing-articles__writer-link" href="/yann-barte">Yann Barte</a>
    </address>
  </div>
</article>
"""


@pytest.fixture
def listing_row():
    return parse_listing(BeautifulSoup(LISTING_HTML, "lxml"))[0]


def test_parse_listing_reads_slug(listing_row):
    assert listing_row["slug"] == "le-pire-de-lfi-fait-sa-rentree"


def test_parse_listing_reads_format(listing_row):
    assert listing_row["format"] == "Le dossier de la semaine"


def test_parse_listing_reads_authors(listing_row):
    assert listing_row["authors"] == ["Yann Barte"]


FREE_ARTICLE_HTML = """
<div class="breadcrumb">
  <a class="breadcrumb__link" href="https://www.franc-tireur.fr">Accueil</a>
  <a class="breadcrumb__link" href="https://www.franc-tireur.fr/tous-les-numeros">Numéros</a>
  <a class="breadcrumb__link" href="https://www.franc-tireur.fr/lemprise-LFI">N° 250 L'emprise</a>
</div>
<article class="article js-article">
  <header class="article__header">
    <span class="article__format">Le dossier de la semaine</span>
    <h1 class="article__title">Le pire de LFI fait sa rentrée</h1>
    <address class="article__date"><time datetime="2026-08-26T08:45:04+02:00">26/08</time></address>
    <div class="article__writers">
      <a href="/yann-barte" class="article__writer-link">Yann Barte</a>
    </div>
    <img class="article__figure" data-src="https://images.franc-tireur.fr/dossier.png">
  </header>
  <div class="article__headline">Aux universités d’été des Insoumis.</div>
  <section class="article-body js-article-body"><p>Devant une foule.</p></section>
</article>
"""


@pytest.fixture
def free_article():
    return parse_article(FREE_ARTICLE_HTML)


def test_parse_article_reads_title(free_article):
    assert free_article["title"] == "Le pire de LFI fait sa rentrée"


def test_parse_article_reads_iso_date(free_article):
    assert free_article["date"] == "2026-08-26T08:45:04+02:00"


def test_parse_article_reads_authors(free_article):
    assert free_article["authors"] == ["Yann Barte"]


def test_parse_article_reads_the_issue_from_the_breadcrumb(free_article):
    assert free_article["issue_slug"] == "lemprise-LFI"


def test_parse_article_reads_the_lead_image(free_article):
    assert free_article["image"] == "https://images.franc-tireur.fr/dossier.png"


def test_parse_article_marks_a_free_article_as_not_premium(free_article):
    assert free_article["premium"] is False


def test_parse_article_keeps_the_inline_body_when_not_paywalled(free_article):
    assert "Devant une foule." in free_article["body_html"]


def test_parse_article_calls_the_paywall_for_an_encrypted_body(monkeypatch):
    import franc_tireur_cli

    monkeypatch.setattr(franc_tireur_cli, "decrypt_body",
                        lambda blob, jar: f"<p>opened {blob}</p>")
    html = '<article class="article"><section class="js-article-body" data-content-src="BLOB">'
    article = parse_article(html, jar={"cmiuser": "x", "lauser_token": "y"})
    assert article["body_html"] == "<p>opened BLOB</p>"


def test_html_to_body_markdown_unescapes_entities():
    assert html_to_body_markdown("<p>Assoiff&#233;s de revanche.</p>") == "Assoiffés de revanche."


def test_html_to_body_markdown_collapses_blank_runs():
    assert html_to_body_markdown("<p>Un.</p><p>Deux.</p>") == "Un.\n\nDeux."


def test_article_to_markdown_carries_the_url(free_article):
    md = article_to_markdown(free_article, "https://www.franc-tireur.fr/le-pire")
    assert 'url: "https://www.franc-tireur.fr/le-pire"' in md


def test_article_to_markdown_renders_the_headline_in_bold(free_article):
    assert "**Aux universités d’été des Insoumis.**" in article_to_markdown(free_article)


def test_build_article_html_sets_a_viewport_when_asked(free_article):
    assert "width=device-width" in build_article_html(free_article, _HTML_CSS, viewport=True)


def test_build_article_html_omits_the_viewport_for_print(free_article):
    assert "width=device-width" not in build_article_html(free_article, _HTML_CSS, viewport=False)


def test_build_article_html_escapes_the_title():
    article = {"title": "A <script> title", "format": "", "authors": [], "date": None,
               "headline": "", "issue": "", "image": None, "body_html": ""}
    assert "<title>A &lt;script&gt; title</title>" in build_article_html(
        article, _HTML_CSS, viewport=True)


def test_jwt_payload_decodes_an_unpadded_body():
    # {"user":{"id":23968}} — base64url, padding stripped, as cookies store it.
    token = "aGVhZGVy.eyJ1c2VyIjp7ImlkIjoyMzk2OH19.c2ln"
    assert jwt_payload(token) == {"user": {"id": 23968}}


def test_load_milibris_imports_the_first_existing_candidate(monkeypatch, tmp_path):
    import franc_tireur_cli

    stub = tmp_path / "milibris_cli.py"
    stub.write_text("MARKER = 'from the stub'\n")
    monkeypatch.setattr(franc_tireur_cli, "MILIBRIS_CANDIDATES",
                        (tmp_path / "absent.py", stub))
    assert franc_tireur_cli._load_milibris().MARKER == "from the stub"


def test_load_milibris_exits_when_no_checkout_is_found(monkeypatch, tmp_path):
    import franc_tireur_cli

    monkeypatch.setattr(franc_tireur_cli, "MILIBRIS_CANDIDATES",
                        (None, tmp_path / "absent.py"))
    with pytest.raises(SystemExit, match="milibris-cli"):
        franc_tireur_cli._load_milibris()
