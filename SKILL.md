---
name: franc-tireur-cli
description:
  "Read Franc-Tireur (French weekly) from the terminal with the bundled `ft`
  command. Use for site articles as Markdown/HTML/PDF with the paywall opened,
  an issue's table of contents, site search, and the printed edition's pages
  (JPEG/PDF) and per-article text via the liseuse. Authenticates by reading the
  user's browser session — no login to run. Every read supports --json. For a
  kiosk other than Franc-Tireur's, use the milibris-cli skill instead."
allowed-tools:
  - Bash
  - Read
---

# Franc-Tireur CLI

Terminal access to Franc-Tireur as the logged-in subscriber.

## How to invoke

Invoke it as **`ft`** — on `$PATH` via a symlink in `~/.local/bin` onto this
repo's `bin/ft`, so a `git pull` or an uncommitted edit takes effect at once. It
is a PEP 723 `uv` script; deps resolve on first run. If `ft` is missing from
`$PATH`, run `<skill-dir>/bin/ft` directly or link it:

```bash
ln -sfn <skill-dir>/bin/ft ~/.local/bin/ft
```

The liseuse commands (`toc`, `page`, `dump`) import the **milibris-cli** module,
found via `$MILIBRIS_CLI`, a `milibris-cli` checkout beside this repo, or
`~/.claude/skills/milibris-cli/`. The site commands need none of that. For any
*other* publisher's kiosk, use the **milibris-cli** skill directly.

## Two back-ends — pick the right one

Franc-Tireur publishes the same journalism twice. Which one to read depends on
what the user asked for:

| The user wants | Back-end | Command |
|---|---|---|
| "read this article" / a URL or slug | website | `ft read <slug>` |
| "what's in this week's issue?" | website TOC (has slugs) | `ft numero latest` |
| "search Franc-Tireur for X" | website | `ft search "X"` |
| "the paper" / pages / PDF / archive | liseuse (miLibris) | `ft dump`, `ft page` |
| "TOC of the printed issue" | liseuse | `ft toc` |
| full-text search *inside* the paper | liseuse | `milibris search "X"` (milibris-cli skill) |

The website is the right default for reading: its slugs are human-readable and
match the URLs the user sees. The liseuse is the right one for the physical
object — page images, a PDF, the print rubrics.

**Identifiers differ between them and are not interchangeable.** Website
articles are slugs (`le-pire-de-lfi-fait-sa-rentree`); liseuse articles are
UUIDs from `ft toc`. Website issues are slugs (`lemprise-LFI`); liseuse issues
are `nNNN-YYYY` slugs or a mid UUID.

## Authentication

Nothing to paste and nothing to run: `ft` decrypts the user's Chromium
cookie store (Chrome/Arc/Brave/Edge) with the macOS keychain key.

- Site paywall needs `cmiuser` + `lauser_token` on `.franc-tireur.fr`.
- Liseuse needs `franctireurWebKioskSessionKey` on `digital.franc-tireur.fr`.

`cmiuser` is a ~24 h JWT. **On a `401`, do not retry** — tell the user to reload
https://www.franc-tireur.fr in their browser (that silently mints a new token),
then re-run. Start with `ft whoami` when anything looks off.

## Output conventions

- **`--json` everywhere.** Every read command emits structured JSON — use it to
  chain commands or extract fields; never parse the human output.
- **Markdown by default**, with YAML frontmatter (title, publication, issue,
  date, page, rubrics, authors, words, reading time).
- **Stable paths.** `dump` writes to `./dump/<title>/<date>/` unless `-o`.
- **No silent fallbacks.** An unknown issue slug is an error, not "the latest
  one". Failures exit non-zero with a one-line reason — surface it.

## Commands

### Website

```bash
ft whoami                                  # subscriber id, cookies, liseuse status
ft numeros -n 10                           # issues, newest first, with slugs
ft numero latest                           # cover + TOC of the current issue
ft numero lemprise-LFI --json              # {number, date, title, summary, articles[]}
ft read le-pire-de-lfi-fait-sa-rentree     # Markdown to stdout, paywall opened
ft read <slug> -o out/article.md           # parents auto-created
ft read <slug> --html -o phone.html        # responsive standalone page
ft read <slug> --pdf                       # reader-style A4 → ./<slug>.pdf
ft read <slug> --json                      # parsed fields incl. body_html
ft search "melenchon" -n 20 --json
```

`read` takes a slug or a full franc-tireur.fr URL. An `-o` path ending in
`.html`/`.pdf` implies `--html`/`--pdf`.

### Printed edition (liseuse)

```bash
ft toc                                     # latest printed issue, article UUIDs
ft toc n249-2026 --rubric dossier --json
ft page 1 -o cover.jpg                     # LD render (~700px wide)
ft page 1 --hd -o cover.jpg                # full resolution, tiles stitched
ft dump                                    # pages + PDF + per-article Markdown
ft dump n248-2026 --hd -o /tmp/ft248
ft dump --page 5 --no-articles             # just one page
```

## Common patterns for agents

**Read every article of this week's issue:**
```bash
ft numero latest --json | jq -r '.articles[].slug' \
  | xargs -I{} ft read {} -o "out/{}.md"
```

**Find this week's articles about a topic, then read them:**
```bash
ft numero latest --json \
  | jq -r '.articles[] | select(.title|test("LFI";"i")) | .slug' \
  | xargs -I{} ft read {}
```

**Archive the printed paper at full resolution:**
```bash
ft dump --hd            # → ./dump/franc-tireur/<date>/ (JPEGs + PDF + Markdown)
```

**Map a website article to its page in the paper:**
```bash
ft toc --json | jq -r '.[] | "\(.page)\t\(.title)"'
```

## Don'ts

- **Don't** invent slugs — run `ft numeros` / `ft numero latest` first.
- **Don't** retry on `401`: the user must reload franc-tireur.fr in the browser.
- **Don't** parse the human output; use `--json`.
- **Don't** call `www.franc-tireur.fr` or the kiosk with plain `curl` — both sit
  behind CloudFront and reject non-browser TLS handshakes. `ft` impersonates
  Chrome.
- **Don't** mix identifiers: website slugs and liseuse UUIDs name different
  objects.
- **Don't** reimplement kiosk work here — searching the printed archive or
  reading another publisher's liseuse is the **milibris-cli** skill's job.
- **Don't** reach for `--hd` by default — it downloads one tile per grid cell per
  page. Use it when the user wants print quality or readable text in the image.
