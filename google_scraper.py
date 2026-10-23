#!/usr/bin/env python3
"""
Search Google with real Chromium (Playwright) and scrape the results.

- PDF links  -> downloaded as .pdf files
- Page links -> opened and scraped: title + main content ONLY
                (header / footer / nav / sidebar / images stripped)

Every document gets a JSON metadata file:

    {
      "url": "...", "title": "...", "source": "...",
      "publishedAt": "...", "lastUpdate": "...",
      "language": "en", "wordCount": 0, "filePath": "...",
      "contentHash": "...", "collectedAt": "...",
      "dateConfidence": 0, "documentType": "article"
    }

Install:
    pip install requests playwright beautifulsoup4 pypdf
    playwright install chromium

Run:
    # pages + PDFs (default):
    python google_scraper.py 'what is SEO before:2019' --max 20 --out data

    # PDFs only (original behaviour):
    python google_scraper.py '"what is SEO" filetype:pdf before:2019' --type pdf

    # visible browser window (to solve captchas by hand):
    python google_scraper.py 'what is SEO' --no-headless
"""
import argparse
import hashlib
import html as html_lib
import json
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PROFILE = Path.home() / ".google_scraper_profile"
POLITE_DELAY = 1.5
MAX_PDF_BYTES = 100 * 1024 * 1024

# Search-engine internal links that are never real results
_JUNK_DOMAIN_RE = re.compile(
    r"(^|\.)((accounts|support|policies|maps|translate|webcache)\.google\.|"
    r"google\.[^/]+/(search|url|maps|preferences)|youtube\.com/(results|redirect)|"
    r"bing\.com/(search|videos|images|ck/a)|duckduckgo\.com)", re.IGNORECASE)

# A real desktop-Chrome user agent (headless UA contains "HeadlessChrome"
# and is an instant bot tell — never send it)
REAL_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def is_pdf_url(url: str) -> bool:
    return ".pdf" in urlparse(url).path.lower()


def clean_url(href: str) -> str | None:
    """Unwrap Google /url?q=... links and keep only real http(s) result URLs."""
    if not href:
        return None
    href = unquote(html_lib.unescape(href)).strip()
    if href.startswith("/url?"):
        href = parse_qs(urlparse(href).query).get("q", [""])[0]
    if not href.lower().startswith("http"):
        return None
    url = href.split("#")[0]
    if _JUNK_DOMAIN_RE.search(urlparse(url).netloc + urlparse(url).path):
        return None
    return re.sub(r"[?&](utm_[^=&]+|fbclid|gclid|ved|sa|usg)=[^&]*", "", url)


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len].strip() or "untitled"


def unique_doc_folder(parent: Path, title: str) -> Path:
    """Create (and return) a folder named after the document title.

    Layout produced:
        parent/<Title>/<Title>.txt  and  <Title>.json
    If the folder already exists, ' (2)', ' (3)', … is appended.
    """
    base = sanitize_filename(title)
    folder, n = parent / base, 2
    while folder.exists():
        folder = parent / f"{base} ({n})"
        n += 1
    folder.mkdir(parents=True)
    return folder


# ---------------------------------------------------------------------------
# 1. Google search via Playwright (real Chrome)
# ---------------------------------------------------------------------------
RESULT_SELECTORS = ["#search a:has(h3)", "#rso a:has(h3)", "a:has(h3)"]

# Remove the "I am a bot" signals Google checks in headless Chrome
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


def search_google(query: str, max_results: int, kind: str,
                  headless: bool) -> list[tuple[str, str]]:
    """Open Chrome, read result pages, return [(url, 'pdf'|'page'), ...]."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        ctx = None
        last_exc = None
        for launch_kwargs in (
                # full Chromium (new headless mode — much harder to detect)
                dict(channel="chromium"),
                dict(),                       # bundled fallback build
        ):
            try:
                ctx = p.chromium.launch_persistent_context(
                    str(PROFILE), headless=headless, locale="en-US",
                    user_agent=REAL_UA,
                    viewport={"width": 1366, "height": 768},
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-sandbox"],
                    **launch_kwargs)
                break
            except Exception as exc:
                last_exc = exc
        if ctx is None:
            print(f"[search] could not launch Chromium ({last_exc}) — "
                  f"run: playwright install chromium")
            return []
        ctx.add_init_script(_STEALTH_JS)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for start in range(0, 200, 10):
            page.goto(f"https://www.google.com/search?q={quote_plus(query)}"
                      f"&start={start}&hl=en&filter=0")
            page.wait_for_load_state("domcontentloaded")

            # cookie / consent popup
            try:
                page.get_by_role("button", name=re.compile(
                    r"accept all|reject all|i agree|accepter|tout accepter",
                    re.I)).first.click(timeout=2500)
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass

            # captcha
            if "/sorry/" in page.url or "unusual traffic" in page.content().lower():
                if headless:
                    print("[search] Google shows a captcha in headless mode.\n"
                          "         Run once with --no-headless and solve it "
                          "by hand — the saved profile then usually passes "
                          "headless too.")
                    break
                input("Solve the captcha in the browser, then press Enter... ")
                page.wait_for_load_state("domcontentloaded")

            hrefs: list[str] = []
            for selector in RESULT_SELECTORS:
                hrefs = page.eval_on_selector_all(
                    selector, "els => els.map(e => e.href)")
                if hrefs:
                    break
            if not hrefs:
                print(f"[search] page {start // 10 + 1}: no results, stopping")
                break

            new = 0
            for h in hrefs:
                url = clean_url(h)
                if not url or url in seen:
                    continue
                is_pdf = is_pdf_url(url)
                if kind == "pdf" and not is_pdf:
                    continue
                if kind == "page" and is_pdf:
                    continue
                seen.add(url)
                found.append((url, "pdf" if is_pdf else "page"))
                new += 1
            print(f"[search] page {start // 10 + 1}: "
                  f"{len(found)} result links so far (+{new})")

            if len(found) >= max_results or new == 0:
                break
            time.sleep(random.uniform(3, 6))
        ctx.close()
    return found[:max_results]


# ---------------------------------------------------------------------------
# 2. Main-content extraction (no header / footer / nav / images)
# ---------------------------------------------------------------------------
DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "svg",
             "form", "button", "select", "input", "textarea",
             "header", "footer", "nav", "aside",
             "img", "picture", "figure", "video", "audio")
DROP_RE = re.compile(
    r"nav|menu|footer|header|sidebar|side-bar|cookie|consent|banner|advert|"
    r"promo|social|share|comment|related|popup|modal|overlay|breadcrumb|"
    r"subscribe|newsletter|sign-?up|login|toolbar|widget|pagination|tag-cloud",
    re.IGNORECASE)


def _drop_noise(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    for attr in ("class", "id", "role"):
        for tag in soup.find_all(attrs={attr: DROP_RE}):
            tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or \
              soup.find("meta", attrs={"name": name})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def _parse_http_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _parse_any_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    dt = _parse_http_date(raw)
    if dt:
        return dt
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def _page_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    published = _parse_any_date(_meta_content(
        soup, "article:published_time", "og:published_time", "datePublished",
        "date", "pubdate", "publishdate", "DC.date", "dcterms.created"))
    modified = _parse_any_date(_meta_content(
        soup, "article:modified_time", "og:modified_time", "dateModified",
        "dcterms.modified"))
    if not published or not modified:
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except (ValueError, TypeError):
                continue
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict):
                    published = published or _parse_any_date(item.get("datePublished"))
                    modified = modified or _parse_any_date(item.get("dateModified"))
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            published = _parse_any_date(t["datetime"])
    return published, modified


def _page_title(soup: BeautifulSoup, url: str) -> str:
    for getter in (
        lambda: _meta_content(soup, "og:title", "twitter:title"),
        lambda: (soup.find("h1").get_text(" ", strip=True)
                 if soup.find("h1") else ""),
        lambda: (soup.title.get_text(" ", strip=True) if soup.title else ""),
    ):
        t = getter()
        if t and len(t) > 3:
            return re.split(r"\s+[|–—-]\s+[^|–—-]{1,40}$", t)[0].strip()[:200]
    return Path(urlparse(url).path).stem.replace("_", " ").replace("-", " ") \
        or urlparse(url).netloc


def find_main_node(soup: BeautifulSoup):
    """Article container: semantic tags first, then best text-density block."""
    for node in (soup.find("article"), soup.find("main"),
                 soup.find(attrs={"role": "main"})):
        if node and len(node.get_text(" ", strip=True)) > 200:
            return node
    best, best_score = None, 0
    for node in soup.find_all(["article", "main", "section", "div"]):
        text_len = len(node.get_text(" ", strip=True))
        if text_len < 200:
            continue
        link_len = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
        score = text_len - 2 * link_len
        if score > best_score:
            best, best_score = node, score
    return best or soup.body or soup


def extract_main_text(node) -> str:
    blocks, seen = [], set()
    for tag in node.find_all(["h1", "h2", "h3", "h4", "p", "li",
                              "blockquote", "pre", "td", "th"]):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
        if len(text) < 15 or text in seen:
            continue
        seen.add(text)
        blocks.append(text)
    return "\n\n".join(blocks)


_STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "is", "for", "with", "that", "on"},
    "fr": {"le", "la", "les", "de", "et", "est", "des", "une", "dans", "pour"},
    "es": {"el", "la", "de", "y", "en", "los", "las", "una", "para", "con"},
    "de": {"der", "die", "und", "das", "ist", "von", "mit", "den", "ein", "zu"},
}


def fallback_language(text: str) -> str:
    words = re.findall(r"[a-zàâäéèêëîïôöùûüçñß]+", text.lower())
    if not words:
        return "unknown"
    scores = {l: sum(1 for w in words if w in sw) for l, sw in _STOPWORDS.items()}
    best, score = max(scores.items(), key=lambda kv: kv[1])
    return best if score >= 3 else "unknown"


def extract_page(page_html: str, url: str) -> dict:
    soup = BeautifulSoup(page_html, "html.parser")
    title = _page_title(soup, url)
    published, modified = _page_dates(soup)
    html_tag = soup.find("html")
    language = (html_tag.get("lang", "")[:2].lower()
                if html_tag and html_tag.get("lang") else "")
    _drop_noise(soup)
    text = extract_main_text(find_main_node(soup))
    return {"title": title, "text": text, "published": published,
            "modified": modified,
            "language": language or fallback_language(text[:5000])}


def load_existing_urls(meta_file: Path) -> set[str]:
    """URLs already present in the combined metadata.json of a previous run."""
    if not meta_file.exists():
        return set()
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return {m["url"] for m in data if isinstance(m, dict) and m.get("url")}
    except (ValueError, TypeError):
        return set()


def is_too_new(published: datetime | None, before_year: int) -> bool:
    """True if the document was published in before_year or later."""
    if not before_year or not published:
        return False
    cutoff = datetime(before_year, 1, 1, tzinfo=timezone.utc)
    dt = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def guess_document_type(title: str, text_sample: str) -> str:
    blob = (title + " " + text_sample[:2000]).lower()
    if any(w in blob for w in ("guide", "tutorial", "handbook", "manual")):
        return "guide"
    if any(w in blob for w in ("white paper", "whitepaper", "report", "study")):
        return "report"
    if any(w in blob for w in ("abstract", "references", "proceedings")):
        return "paper"
    if any(w in blob for w in ("news", "press release", "breaking")):
        return "news"
    return "article"


# ---------------------------------------------------------------------------
# 3. Processors
# ---------------------------------------------------------------------------
def process_page(url: str, page_dir: Path,
                 out_dir: Path, before_year: int = 0) -> dict | None:
    """Open a result link, keep title + main content, save .txt + metadata."""
    host = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    r = requests.get(url, headers=dict(HEADERS, Referer=host),
                     timeout=60, allow_redirects=True)
    r.raise_for_status()
    if "html" not in r.headers.get("Content-Type", "html").lower():
        print("   skip: not an HTML page")
        return None

    info = extract_page(r.text, url)
    if len(re.findall(r"\w+", info["text"])) < 50:
        print("   skip: no meaningful main content found")
        return None

    http_mod = _parse_http_date(r.headers.get("Last-Modified"))
    published = info["published"] or http_mod
    if is_too_new(published, before_year):
        print(f"   skip: published {published.year} (after {before_year - 1})")
        return None

    folder = unique_doc_folder(page_dir, info["title"])
    final = folder / f"{folder.name}.txt"
    body = (f"{info['title']}\n{'=' * min(len(info['title']), 80)}\n\n"
            f"{info['text']}\n")
    payload = body.encode("utf-8")
    final.write_bytes(payload)

    meta = {
        "url": url,
        "title": info["title"],
        "source": urlparse(url).netloc,
        "publishedAt": _iso(published),
        "lastUpdate": _iso(info["modified"] or published),
        "language": info["language"],
        "wordCount": len(re.findall(r"\w+", info["text"])),
        "filePath": str(final.relative_to(out_dir.parent)),
        "contentHash": hashlib.sha256(payload).hexdigest(),
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "dateConfidence": 1.0 if info["published"] else (0.5 if http_mod else 0.0),
        "documentType": guess_document_type(info["title"], info["text"]),
    }
    (folder / f"{folder.name}.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   saved: {folder.name}/ ({meta['wordCount']} words, "
          f"{meta['language']})")
    return meta


def process_pdf(url: str, pdf_dir: Path,
                out_dir: Path, before_year: int = 0) -> dict | None:
    """Download a PDF and build its metadata."""
    host = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    r = requests.get(url, headers=dict(HEADERS, Referer=host),
                     timeout=60, allow_redirects=True, stream=True)
    r.raise_for_status()
    buf = b""
    for chunk in r.iter_content(chunk_size=8192):
        buf += chunk
        if len(buf) > MAX_PDF_BYTES:
            print("   skip: too large")
            return None
    if not buf.lstrip().startswith(b"%PDF"):
        print("   skip: not a PDF")
        return None

    # title / dates / text from the PDF itself (best effort)
    title = unquote(Path(urlparse(url).path).stem).replace("_", " ").replace("-", " ")
    created = modified = None
    text = ""
    if PdfReader:
        try:
            tmp = pdf_dir / ".tmp.pdf"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(buf)
            reader = PdfReader(str(tmp))
            pdf_meta = {k.lstrip("/"): str(v)
                        for k, v in (reader.metadata or {}).items()}
            if pdf_meta.get("Title", "").strip():
                title = pdf_meta["Title"].strip()
            created = _parse_any_date(pdf_meta.get("CreationDate", "")[2:16])
            modified = _parse_any_date(pdf_meta.get("ModDate", "")[2:16])
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            tmp.unlink()
        except Exception:
            pass

    http_mod_pre = _parse_http_date(r.headers.get("Last-Modified"))
    if is_too_new(created or http_mod_pre, before_year):
        print(f"   skip: published {(created or http_mod_pre).year} "
              f"(after {before_year - 1})")
        return None

    folder = unique_doc_folder(pdf_dir, title)
    final = folder / f"{folder.name}.pdf"
    final.write_bytes(buf)

    http_mod = _parse_http_date(r.headers.get("Last-Modified"))
    published = created or http_mod
    meta = {
        "url": url,
        "title": title,
        "source": urlparse(url).netloc,
        "publishedAt": _iso(published),
        "lastUpdate": _iso(modified or published),
        "language": fallback_language(text[:5000]) if text else "unknown",
        "wordCount": len(re.findall(r"\w+", text)),
        "filePath": str(final.relative_to(out_dir.parent)),
        "contentHash": hashlib.sha256(buf).hexdigest(),
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "dateConfidence": 1.0 if created else (0.5 if http_mod else 0.0),
        "documentType": guess_document_type(title, text),
    }
    (folder / f"{folder.name}.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   saved: {folder.name}/ ({len(buf) // 1024} KB)")
    return meta


# ---------------------------------------------------------------------------
# 3b. Search orchestrator — Google only, via Chromium
# ---------------------------------------------------------------------------
def find_results(query: str, max_results: int, kind: str,
                 headless: bool) -> list[tuple[str, str]]:
    print("[search] querying Google via Chromium ...")
    urls = search_google(query, max_results, kind, headless)
    if urls:
        print(f"[search] Google returned {len(urls)} result links")
    return urls


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Google scraper (Playwright): PDFs + page main content")
    ap.add_argument("query", help="search query, e.g. 'what is SEO before:2019'")
    ap.add_argument("--max", type=int, default=10,
                    help="number of results to collect (default: 10)")
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--type", choices=["pdf", "page", "both"], default="both",
                    help="collect PDFs only, pages only, or both (default: both)")
    ap.add_argument("--before-year", type=int, default=2019,
                    help="skip documents published in this year or later "
                         "(default: 2019; use 0 to disable the date filter)")
    ap.add_argument("--headless", dest="headless", action="store_true",
                    default=True, help="run Chrome headless (default)")
    ap.add_argument("--no-headless", dest="headless", action="store_false",
                    help="show the browser window (needed to solve captchas)")
    args = ap.parse_args()

    pdf_dir = args.out / "pdfs"
    page_dir = args.out / "pages"
    for d in (pdf_dir, page_dir):
        d.mkdir(parents=True, exist_ok=True)

    results = find_results(args.query, args.max, args.type, args.headless)
    if not results:
        sys.exit("No result links found. If Google showed a captcha, run once "
                 "with --no-headless and solve it by hand.")

    print(f"\n{len(results)} result links found\n")

    combined_file = args.out / "metadata.json"
    old_meta = json.loads(combined_file.read_text(encoding="utf-8")) \
        if combined_file.exists() else []
    existing_urls = load_existing_urls(combined_file)
    if existing_urls:
        print(f"[dedup] {len(existing_urls)} URLs already collected — "
              f"they will be skipped")

    all_meta, errors = list(old_meta), []
    skipped_dup = 0
    for i, (url, kind) in enumerate(results, 1):
        if url in existing_urls:
            print(f"[{i}/{len(results)}] ({kind}) {url}")
            print("   skip: URL already exists in metadata.json")
            skipped_dup += 1
            continue
        print(f"[{i}/{len(results)}] ({kind}) {url}")
        try:
            meta = (process_pdf(url, pdf_dir, args.out, args.before_year)
                    if kind == "pdf"
                    else process_page(url, page_dir, args.out, args.before_year))
            if meta:
                all_meta.append(meta)
        except Exception as e:
            print(f"   error: {e}")
            errors.append({"url": url, "error": str(e)})
        time.sleep(POLITE_DELAY)

    combined_file.write_text(
        json.dumps(all_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    if errors:
        (args.out / "errors.json").write_text(
            json.dumps(errors, indent=2, ensure_ascii=False), encoding="utf-8")

    new_meta = [m for m in all_meta if m["url"] not in existing_urls]
    n_pdf = sum(1 for m in new_meta if m["filePath"].endswith(".pdf"))
    print(f"\nDone: {n_pdf} PDFs + {len(new_meta) - n_pdf} pages collected, "
          f"{skipped_dup} duplicates skipped, {len(errors)} failed. "
          f"(metadata.json now holds {len(all_meta)} documents)")
    print(f"PDFs     -> {pdf_dir}/<Title>/<Title>.pdf + <Title>.json")
    print(f"Pages    -> {page_dir}/<Title>/<Title>.txt + <Title>.json")
    print(f"Combined -> {args.out / 'metadata.json'}")


if __name__ == "__main__":
    main()
