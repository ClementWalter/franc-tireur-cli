#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "click==8.5.0",
#     "curl_cffi==0.17.0",
#     "beautifulsoup4==4.15.0",
#     "lxml==6.1.3",
#     "markdownify==1.2.3",
#     "markdown==3.11.2",
#     "pycryptodome==4.2.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Tests for the terminal-free path: the missing-session error and where files
land without -o. No network."""

import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))
import franc_tireur_cli as ft  # noqa: E402

ARTICLE_HTML = """
<article class="article">
  <h1 class="article__title">Le pire de LFI</h1>
  <div class="js-article-body"><p>Texte libre.</p></div>
</article>
"""


@pytest.fixture
def no_cookies(monkeypatch):
    monkeypatch.setattr(ft, "browser_cookies", lambda *a, **k: [])


def test_missing_cookies_names_the_site(no_cookies):
    with pytest.raises(click.ClickException) as exc:
        ft.require_cookies()
    assert "https://www.franc-tireur.fr" in exc.value.message


def test_missing_cookies_names_the_keychain_prompt(no_cookies):
    with pytest.raises(click.ClickException) as exc:
        ft.require_cookies()
    assert "Always Allow" in exc.value.message


def test_downloads_folder_is_under_home():
    assert ft.DOWNLOADS == Path.home() / "Downloads" / "franc-tireur"


def test_liseuse_files_land_beside_the_site_files():
    assert ft.milibris_cli.DOWNLOADS == ft.DOWNLOADS.parent


@pytest.fixture
def stub_site(monkeypatch, tmp_path):
    monkeypatch.setattr(ft, "get", lambda url, **kw: SimpleNamespace(text=ARTICLE_HTML))
    monkeypatch.setattr(ft, "DOWNLOADS", tmp_path)
    return tmp_path


def test_read_html_defaults_to_downloads(stub_site):
    result = CliRunner().invoke(ft.cli, ["read", "le-pire-de-lfi", "--html"])
    assert result.output.strip() == str(stub_site / "le-pire-de-lfi.html")


def test_read_pdf_defaults_to_downloads(stub_site, monkeypatch):
    monkeypatch.setattr(ft, "html_to_pdf", lambda document, target, base_url=None: target.write_text("%PDF-"))
    result = CliRunner().invoke(ft.cli, ["read", "le-pire-de-lfi", "--pdf"])
    assert result.output.strip() == str(stub_site / "le-pire-de-lfi.pdf")


def test_read_markdown_still_prints_to_stdout(stub_site):
    result = CliRunner().invoke(ft.cli, ["read", "le-pire-de-lfi"])
    assert "Texte libre." in result.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
