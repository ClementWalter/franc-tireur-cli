# franc-tireur-cli

Read **Franc-Tireur** — the weekly's website *and* its liseuse — from the
terminal, as the logged-in subscriber. Ships two commands:

| Command | Scope |
|---|---|
| `ft` | Franc-Tireur: site articles (paywall included) + the printed issue |
| `milibris` | Any miLibris digital kiosk, host-agnostic |

## Why two CLIs

Franc-Tireur serves the same journalism through two independent back-ends:

* **www.franc-tireur.fr** — a server-rendered site. Paywalled articles ship
  their body as an AES blob in `data-content-src`; only
  `ws.profile.franc-tireur.fr/content/decrypt` opens it, and only for a request
  carrying *both* the subscriber's `cmiuser` JWT and the `lauser_token`
  shared-user cookie.
* **digital.franc-tireur.fr** — the **miLibris** liseuse: the printed paper as
  page images, a PDF and per-article JSON.

The second half is not Franc-Tireur-specific. miLibris hosts the liseuse of many
titles behind per-publisher hosts, all running the same kiosk app (an Express
session cookie named `<name>WebKioskSessionKey`) and the same HTML5 reader
(a short-lived ticket JWT unlocking `content.milibris.com`). So that layer lives
in `milibris_cli.py` as a standalone, host-agnostic CLI, and `ft` builds on it.

## Install

Both scripts are [PEP 723](https://peps.python.org/pep-0723/) `uv` scripts —
dependencies resolve on first run, nothing to install. Put them on `$PATH`:

```bash
ln -sfn "$PWD/bin/ft"       ~/.local/bin/ft
ln -sfn "$PWD/bin/milibris" ~/.local/bin/milibris
```

The symlinks point at this checkout, so a `git pull` or an uncommitted edit
takes effect immediately.

## Authentication

There is nothing to paste. Both CLIs read the session straight out of a locally
logged-in Chromium browser (Chrome, Arc, Brave, Edge) by decrypting its cookie
store with the browser's macOS keychain key:

* `cmiuser` + `lauser_token` on `.franc-tireur.fr` — the site paywall.
* `<name>WebKioskSessionKey` on the kiosk host — the liseuse.

`cmiuser` is a ~24 h JWT. When it expires the paywall answers `401`; reload
www.franc-tireur.fr in the browser to mint a new one, then re-run.

## `ft`

```bash
ft whoami                                   # which subscriber, is the liseuse live
ft numeros -n 10                            # recent issues
ft numero latest                            # this week's cover + TOC
ft numero lemprise-LFI --json
ft read le-pire-de-lfi-fait-sa-rentree      # Markdown to stdout, paywall opened
ft read <slug> -o article.md
ft read <slug> --html -o phone.html         # responsive standalone page
ft read <slug> --pdf                        # reader-style A4
ft search "melenchon" --json
```

The printed paper (via the liseuse):

```bash
ft toc                                      # TOC of the latest printed issue
ft toc n249-2026 --rubric dossier
ft page 1 -o cover.jpg                      # one page as JPEG
ft page 1 --hd -o cover.jpg                 # full-resolution (tiles stitched)
ft dump --hd                                # pages + PDF + per-article Markdown
```

## `milibris`

```bash
milibris kiosks                             # kiosks your browsers are logged into
milibris titles
milibris issues -n 20
milibris issue n249-2026 --json
milibris toc --json
milibris read <article-id> --issue n249-2026
milibris dump --hd -o ./out
milibris search "ukraine" --issue n250-2026 --json
milibris set-host digital.example.fr        # default kiosk, if you use several
```

The kiosk is picked from `--host`, then `$MILIBRIS_HOST`, then
`~/.config/milibris-cli/config.json`, then the only kiosk you are logged into.

`dump` writes to `./dump/<title>/<date>/`:

```
pages/page-NNN.jpg        LD renders, or HD tilesets stitched with --hd
<title>-<date>.pdf        the pages assembled
pNNN-<slug>.md            one Markdown file per article, frontmatter included
resources/<mid>.jpg       article images, downloaded so the files outlive the ticket
```

## Output conventions

* `--json` on every read command; human output is plain, tab/space-aligned text
  (no ANSI), so it pipes.
* Markdown carries YAML frontmatter (title, publication, issue, date, page,
  rubrics, authors, words, reading time).
* Failures exit non-zero with a one-line reason. Nothing falls back silently: an
  unknown issue slug is an error, not the latest issue.

## Tests

```bash
uv run --with pytest --with click --with curl_cffi --with beautifulsoup4 \
  --with lxml --with markdownify --with markdown --with pycryptodome \
  -m pytest tests -q
```

They cover the pure layers only — ref parsing, the manifest key derivation,
catalogue/article scraping and Markdown rendering. No network.
