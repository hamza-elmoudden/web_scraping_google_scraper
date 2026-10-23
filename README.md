# Google Scraper

Search Google with a real Chromium browser (Playwright) and collect the results as clean documents.

- **PDF links** → downloaded as `.pdf` files
- **Page links** → opened and scraped: title + main content only (header, footer, nav, sidebar, and images are stripped)
- Every document gets a **JSON metadata file**, and all metadata is also merged into one `metadata.json`

---

## Features

- Real Chromium via Playwright with a persistent profile and basic stealth patches
- Automatic cookie/consent popup handling
- Captcha detection (solve once manually with `--no-headless`, the saved profile usually passes afterwards)
- Main-content extraction: semantic tags first (`article`, `main`), then a text-density fallback
- Date extraction from meta tags, JSON-LD, `<time>`, HTTP `Last-Modified`, and PDF metadata
- Date filter (`--before-year`) to skip documents published in a given year or later
- Deduplication by URL across runs
- Language detection (from `<html lang>` or a stopword fallback)
- Document type guess: `article`, `guide`, `report`, `paper`, `news`

---

## Installation

```bash
pip install requests playwright beautifulsoup4 pypdf
playwright install chromium
```

Requires Python 3.10+ (uses `str | None` type syntax).

`pypdf` is optional. Without it, PDFs are still downloaded but title, dates, language, and word count are less accurate.

---

## Usage

```bash
python google_scraper.py "<query>" [options]
```

### Examples

```bash
# Pages + PDFs (default), 20 results
python google_scraper.py 'what is SEO before:2019' --max 20 --out data

# PDFs only
python google_scraper.py '"what is SEO" filetype:pdf before:2019' --type pdf

# Pages only, no date filter
python google_scraper.py 'what is SEO' --type page --before-year 0

# Visible browser window (to solve a captcha by hand)
python google_scraper.py 'what is SEO' --no-headless
```

### Options

| Option | Default | Description |
|---|---|---|
| `query` | required | Search query (supports Google operators like `filetype:pdf`, `before:2019`) |
| `--max` | `10` | Number of results to collect |
| `--out` | `data` | Output directory |
| `--type` | `both` | `pdf`, `page`, or `both` |
| `--before-year` | `2019` | Skip documents published in this year or later. Use `0` to disable |
| `--headless` | on | Run Chromium headless |
| `--no-headless` | off | Show the browser window (needed for captchas) |

---

## Output structure

```
data/
├── metadata.json              # combined metadata of all documents
├── errors.json                # failed URLs (only if errors occurred)
├── pdfs/
│   └── <Title>/
│       ├── <Title>.pdf
│       └── <Title>.json
└── pages/
    └── <Title>/
        ├── <Title>.txt
        └── <Title>.json
```

If a folder name already exists, `(2)`, `(3)`, … is appended.

### Metadata format

```json
{
  "url": "https://example.com/what-is-seo",
  "title": "What is SEO",
  "source": "example.com",
  "publishedAt": "2018-05-12T00:00:00+00:00",
  "lastUpdate": "2018-06-01T00:00:00+00:00",
  "language": "en",
  "wordCount": 1850,
  "filePath": "data/pages/What is SEO/What is SEO.txt",
  "contentHash": "sha256...",
  "collectedAt": "2026-10-23T12:00:00+00:00",
  "dateConfidence": 1.0,
  "documentType": "guide"
}
```

| Field | Notes |
|---|---|
| `dateConfidence` | `1.0` = date found in the page/PDF itself, `0.5` = from HTTP `Last-Modified`, `0.0` = unknown |
| `contentHash` | SHA-256 of the saved file |
| `filePath` | Relative to the parent of `--out` |

---

## How it works

1. **Search**: opens Google in Chromium, reads result pages (10 links per page), unwraps `/url?q=` redirects, filters out Google/Bing/YouTube internal links, and classifies each link as PDF or page.
2. **Dedup**: URLs already present in `metadata.json` are skipped.
3. **Process**: each URL is fetched with `requests`.
   - Pages: noise tags/classes are removed, the main node is found, and text is extracted.
   - PDFs: validated by `%PDF` header, size-limited to 100 MB, metadata read with `pypdf`.
4. **Filter**: documents with fewer than 50 words, non-HTML pages, or dates at/after `--before-year` are skipped.
5. **Save**: file + per-document JSON, then the combined `metadata.json` is rewritten.

---

## Troubleshooting

**"Google shows a captcha in headless mode"**
Run once with `--no-headless`, solve the captcha, press Enter. The profile is saved in `~/.google_scraper_profile` and later headless runs usually pass.

**"No result links found"**
Google may have changed its HTML, or blocked the session. Try `--no-headless`, or delete `~/.google_scraper_profile` and retry.

**"could not launch Chromium"**
Run `playwright install chromium`.

**Pages skipped with "no meaningful main content"**
The page is probably JavaScript-rendered. The page fetch uses `requests`, which does not execute JS.

**Many documents skipped by date**
Documents without a detectable date are kept. Documents dated `--before-year` or later are skipped. Lower or disable the filter with `--before-year 0`.

---

## Notes

- Scraping Google results may violate Google's Terms of Service. Use it responsibly, at low volume, and for personal or research purposes.
- Respect each site's `robots.txt` and copyright when using collected content.
- A polite delay (1.5 s) is applied between document downloads, and 3–6 s between Google result pages.
