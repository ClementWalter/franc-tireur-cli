# franc-tireur-cli

Read **Franc-Tireur** — the weekly's website *and* its liseuse — from the
terminal, as the logged-in subscriber. Ships one command, `ft`.

## Two back-ends

Franc-Tireur serves the same journalism through two independent back-ends:

* **www.franc-tireur.fr** — a server-rendered site. Paywalled articles ship
  their body as an AES blob in `data-content-src`; only
  `ws.profile.franc-tireur.fr/content/decrypt` opens it, and only for a request
  carrying *both* the subscriber's `cmiuser` JWT and the `lauser_token`
  shared-user cookie.
* **digital.franc-tireur.fr** — the **miLibris** liseuse: the printed paper as
  page images, a PDF and per-article JSON.

The second half is not Franc-Tireur-specific. miLibris hosts the liseuse of many
titles behind per-publisher hosts, all running the same kiosk app and the same
HTML5 reader. That layer therefore lives in its **own repo**,
[milibris-cli](https://github.com/ClementWalter/milibris-cli), and `ft` imports it.

## Install

`ft` is a [PEP 723](https://peps.python.org/pep-0723/) `uv` script — dependencies
resolve on first run, nothing to install:

```bash
git clone https://github.com/ClementWalter/franc-tireur-cli.git
ln -sfn "$PWD/franc-tireur-cli/bin/ft" ~/.local/bin/ft
```

Requires [uv](https://docs.astral.sh/uv/) and macOS (the cookie decryption uses
the system keychain).

The symlink points at this checkout, so a `git pull` or an uncommitted edit
takes effect immediately.

The liseuse commands (`toc`, `page`, `dump`) additionally need a
[milibris-cli](https://github.com/ClementWalter/milibris-cli) checkout — the
site commands work without it:

```bash
git clone https://github.com/ClementWalter/milibris-cli.git ../milibris-cli
```

It looks, in order, at `$MILIBRIS_CLI`, a `milibris-cli` directory beside this
repo, `~/.claude/skills/milibris-cli/`, then this directory.

## Authentication

There is nothing to paste. `ft` reads the session straight out of a locally
logged-in Chromium browser (Chrome, Arc, Brave, Edge) by decrypting its cookie
store with the browser's macOS keychain key:

* `cmiuser` + `lauser_token` on `.franc-tireur.fr` — the site paywall.
* `<name>WebKioskSessionKey` on the kiosk host — the liseuse.

`cmiuser` is a ~24 h JWT. When it expires the paywall answers `401`; reload
www.franc-tireur.fr in the browser to mint a new one, then re-run.

## Usage

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

`dump` writes to `./dump/franc-tireur/<date>/`:

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

25 tests over the pure layers — site scraping, Markdown/HTML rendering and the
milibris-cli locator. No network. The reader protocol itself is tested in
[milibris-cli](https://github.com/ClementWalter/milibris-cli).
