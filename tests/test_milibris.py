#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0", "click>=8.1", "curl_cffi>=0.7", "beautifulsoup4>=4.12",
#                 "lxml>=5.0", "markdownify>=0.13", "pycryptodome>=3.20"]
# ///
"""Unit tests for milibris_cli's pure layers: ref parsing, the manifest key
derivation, catalogue scraping, and article→Markdown rendering. No network."""

import base64
import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2

sys.path.insert(0, str(Path(__file__).parent.parent))
from milibris_cli import (  # noqa: E402
    MATERIAL_SALT,
    _evp_bytes_to_key,
    article_to_markdown,
    decrypt_material,
    kiosk_config,
    parse_featured_issue,
    parse_fr_date,
    parse_issue_cards,
    parse_ref,
    slugify,
    tile_names,
)

MID = "6c7feff0-ca75-43a5-9a36-2f761f3a5544"


# -- parse_ref --------------------------------------------------------------- #
def test_parse_ref_none_is_latest():
    assert parse_ref(None) == {}


def test_parse_ref_latest_keyword():
    assert parse_ref("latest") == {}


def test_parse_ref_bare_mid():
    assert parse_ref(MID) == {"mid": MID}


def test_parse_ref_reader_url():
    assert parse_ref(f"https://digital.franc-tireur.fr/reader/{MID}?origin=%2Fx") == {
        "host": "digital.franc-tireur.fr", "mid": MID}


def test_parse_ref_kiosk_url():
    assert parse_ref("https://digital.franc-tireur.fr/franc-tireur/franc-tireur/n250-2026") == {
        "host": "digital.franc-tireur.fr", "path": "franc-tireur/franc-tireur/n250-2026"}


def test_parse_ref_path():
    assert parse_ref("franc-tireur/franc-tireur/n250-2026") == {
        "path": "franc-tireur/franc-tireur/n250-2026"}


def test_parse_ref_slug():
    assert parse_ref("n250-2026") == {"slug": "n250-2026"}


# -- slugify ----------------------------------------------------------------- #
@pytest.mark.parametrize(("raw", "expected"), [
    ("La gauche soumise", "la-gauche-soumise"),
    ("L’œil de Sire", "lil-de-sire"),
    ("“Vers un risque de banqueroute”", "vers-un-risque-de-banqueroute"),
    ("", "sans-titre"),
])
def test_slugify(raw, expected):
    assert slugify(raw) == expected


# -- tiles ------------------------------------------------------------------- #
def test_tile_names_grid_is_column_major():
    spec = {"path": "pages/jpeg/tileset/0001.abc", "tile_col_count": 2, "tile_row_count": 3}
    assert tile_names(spec)[:4] == [
        (0, 0, "pages/jpeg/tileset/0001.abc/tile00x00.jpeg"),
        (0, 1, "pages/jpeg/tileset/0001.abc/tile00x01.jpeg"),
        (0, 2, "pages/jpeg/tileset/0001.abc/tile00x02.jpeg"),
        (1, 0, "pages/jpeg/tileset/0001.abc/tile01x00.jpeg"),
    ]


def test_tile_names_single_tile_is_the_path_itself():
    spec = {"path": "pages/jpeg/tileset/0001.abc", "tile_col_count": 1, "tile_row_count": 1}
    assert tile_names(spec) == [(0, 0, "pages/jpeg/tileset/0001.abc")]


# -- manifest crypto --------------------------------------------------------- #
def test_material_salt_is_the_readers_literal():
    assert MATERIAL_SALT == "wE9mg9pu2KSmp5lh"


def test_evp_bytes_to_key_matches_openssl_lengths():
    key, iv = _evp_bytes_to_key(b"passphrase", b"12345678")
    assert (len(key), len(iv)) == (32, 16)


def test_decrypt_material_round_trip():
    session_id, ticket, day = "0011223344556677", "tok", "2026-08-26"
    payload = {"metadata": {"issue_number": "250"}}
    passphrase = PBKDF2(f"{session_id}_{day}_{ticket}".encode(), MATERIAL_SALT.encode(),
                        32, count=1000, hmac_hash_module=SHA256).hex().encode()
    salt = b"SALTSALT"
    key, iv = _evp_bytes_to_key(passphrase, salt)
    plain = json.dumps(payload).encode()
    pad = 16 - len(plain) % 16
    blob = base64.b64encode(
        b"Salted__" + salt + AES.new(key, AES.MODE_CBC, iv).encrypt(plain + bytes([pad]) * pad)
    ).decode()
    assert decrypt_material(blob, session_id, ticket, day) == payload


def test_decrypt_material_rejects_unsalted_input():
    with pytest.raises(ValueError):
        decrypt_material(base64.b64encode(b"nope" * 8).decode(), "sid", "tok", "2026-08-26")


# -- catalogue scraping ------------------------------------------------------ #
CARD_HTML = """
<div class="issue_container" data-date="2026-08-19">
  <a class="has_right" href="/franc-tireur/franc-tireur/n249-2026" title="Disponible"></a>
  <a href="/franc-tireur/franc-tireur/n249-2026">
    <img data-src="//static.milibris.com/thumbnail/issue/211f968f-583b-44eb-9864-96f7345a88be/front/catalog-cover.png">
  </a>
  <h3 class="issue_legend">N°249 - 19 août 2026</h3>
</div>
"""


@pytest.fixture
def card():
    return parse_issue_cards(BeautifulSoup(CARD_HTML, "lxml"))[0]


def test_parse_issue_cards_reads_slug(card):
    assert card["slug"] == "n249-2026"


def test_parse_issue_cards_reads_mid(card):
    assert card["mid"] == "211f968f-583b-44eb-9864-96f7345a88be"


def test_parse_issue_cards_reads_date(card):
    assert card["date"] == "2026-08-19"


def test_parse_issue_cards_reads_number(card):
    assert card["number"] == "249"


def test_parse_issue_cards_flags_availability(card):
    assert card["available"] is True


def test_parse_issue_cards_skips_the_featured_block():
    html = f'<div class="issue_container"><a href="/reader/{MID}"></a></div>' 
    assert parse_issue_cards(BeautifulSoup(html, "lxml")) == []


FEATURED_HTML = f"""
<div id="left_column" class="issue_container">
  <a class="has_right" href="/reader/{MID}?origin=%2Fx"></a>
</div>
<div id="right_column"><h1>Franc-Tireur</h1><h2>N°250 - mercredi 26 août 2026</h2></div>
"""


def test_parse_featured_issue_reads_mid_and_date():
    row = parse_featured_issue(BeautifulSoup(FEATURED_HTML, "lxml"),
                               {"currentTitleSlug": "franc-tireur",
                                "currentVersionSlug": "franc-tireur",
                                "currentIssueSlug": "n250-2026"})
    assert (row["mid"], row["date"], row["path"]) == (
        MID, "2026-08-26", "franc-tireur/franc-tireur/n250-2026")


def test_parse_featured_issue_absent_without_reader_link():
    assert parse_featured_issue(BeautifulSoup("<div></div>", "lxml"), {}) is None


@pytest.mark.parametrize(("label", "expected"), [
    ("N°250 - mercredi 26 août 2026", "2026-08-26"),
    ("N°247 - 5 août 2026", "2026-08-05"),
    ("N°1 - 1 février 2021", "2021-02-01"),
    ("pas une date", None),
])
def test_parse_fr_date(label, expected):
    assert parse_fr_date(label) == expected


def test_kiosk_config_reads_string_fields():
    html = """<script>
    window.mlKiosk.config = {
        env: "production",
        currentTitleSlug: 'franc-tireur',
        currentIssueSlug: 'n250-2026',
    };
    </script>"""
    assert kiosk_config(html) == {"currentTitleSlug": "franc-tireur",
                                 "currentIssueSlug": "n250-2026"}


def test_kiosk_config_empty_without_block():
    assert kiosk_config("<html></html>") == {}


# -- article rendering ------------------------------------------------------- #
ARTICLE = {
    "title": "La gauche soumise",
    "surtitle": "",
    "subtitle": "",
    "authors": ["PAR Caroline Fourest"],
    "abstract": "Après un été brûlant, c’est la rentrée.",
    "rubrics": ["FRANCE TIREUR"],
    "start_page": 3,
    "word_count": 548,
    "reading_time": 3,
    "notes": [],
    "content": {"sections": [{"type": "section", "items": [
        {"type": "image", "url": "../resources/abc.jpg", "caption": None, "credit": "DR"},
        {"type": "text", "class": "paragraph", "content": "Un <em>mot</em> souligné."},
        {"type": "text", "class": "intertitle", "content": "MANGER LES RICHES"},
        {"type": "text", "class": "heading", "content": "Le retour"},
        {"type": "text", "class": "quote", "content": "La victoire est possible."},
        {"type": "section", "items": [
            {"type": "text", "class": "paragraph", "content": "Encadr&#233;."}]},
    ]}]},
}
META = {"title": "Franc-Tireur", "issue_number": "250", "publication_date": "2026-08-26"}


@pytest.fixture
def rendered():
    return article_to_markdown(ARTICLE, META, lambda p: f"local/{p.rsplit('/', 1)[-1]}")


def test_markdown_frontmatter_carries_the_issue(rendered):
    assert 'issue: "250"' in rendered


def test_markdown_frontmatter_carries_the_page(rendered):
    assert "page: 3" in rendered


def test_markdown_renders_the_title_as_h1(rendered):
    assert "\n# La gauche soumise\n" in rendered


def test_markdown_renders_the_byline(rendered):
    assert "*PAR Caroline Fourest*" in rendered


def test_markdown_rewrites_image_paths_through_the_resolver(rendered):
    assert "![](local/abc.jpg)" in rendered


def test_markdown_keeps_the_image_credit(rendered):
    assert "*DR*" in rendered


def test_markdown_converts_inline_emphasis(rendered):
    assert "Un *mot* souligné." in rendered


def test_markdown_renders_intertitle_as_h3(rendered):
    assert "### MANGER LES RICHES" in rendered


def test_markdown_renders_heading_as_h2(rendered):
    assert "## Le retour" in rendered


def test_markdown_renders_quote_as_blockquote(rendered):
    assert "> La victoire est possible." in rendered


def test_markdown_flattens_nested_sections(rendered):
    assert "Encadré." in rendered


def test_markdown_drops_the_abstract_when_it_opens_the_body():
    article = dict(ARTICLE, abstract="Un mot souligné.")
    assert "**Un *mot* souligné.**" not in article_to_markdown(article, META)


def test_markdown_keeps_a_distinct_abstract(rendered):
    assert "**Après un été brûlant, c’est la rentrée.**" in rendered
