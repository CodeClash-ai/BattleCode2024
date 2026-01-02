#!/usr/bin/env python3
import argparse
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

DEFAULT_START = "https://releases.battlecode.org/javadoc/battlecode24/3.0.5/index.html"
DEFAULT_ROOT = "https://releases.battlecode.org/javadoc/battlecode24/3.0.5/"
DEFAULT_OUTDIR = "battlecode24_javadoc_3.0.5_txt"

JUNK_TAGS = {"script", "style", "noscript"}
# assets we don't want to fetch/convert
SKIP_EXT = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".jar", ".war",
    ".pdf",
}

def normalize(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() + "\n"

def is_within_root(url: str, root: str) -> bool:
    return url.startswith(root)

def should_skip_url(url: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    for ext in SKIP_EXT:
        if path.endswith(ext):
            return True
    # Javadoc sometimes has "index.html?..." but we strip fragments only; queries ok.
    return False

def pick_main_content(soup: BeautifulSoup) -> Tag:
    # Newer Javadoc uses <main> for the page content.
    main = soup.find("main")
    if main:
        return main

    # Fall back to common containers in various Javadoc versions/themes.
    for sel in [
        ("div", {"class": "contentContainer"}),   # older
        ("div", {"class": "content"}),            # sometimes
        ("div", {"id": "content"}),               # sometimes
        ("article", None),
        ("body", None),
    ]:
        tag, attrs = sel
        found = soup.find(tag, attrs=attrs) if attrs else soup.find(tag)
        if found:
            return found

    return soup  # last resort

def table_to_text(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for cell in tr.find_all(["th", "td"]):
            txt = cell.get_text(" ", strip=True)
            cells.append(txt)
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for t in soup.find_all(list(JUNK_TAGS)):
        t.decompose()

    root = pick_main_content(soup)

    out = []

    def emit(txt: str):
        if txt:
            out.append(txt)

    def walk(el):
        if isinstance(el, NavigableString):
            emit(str(el))
            return
        if not isinstance(el, Tag):
            return

        name = el.name.lower()

        # Skip nav/sidebars if they exist
        if name in {"nav", "header", "footer"}:
            return
        # Skip elements commonly used for nav in Javadoc
        cls = " ".join(el.get("class", [])).lower()
        if any(x in cls for x in ["navbar", "navlist", "header", "footer", "side", "sidebar", "indexnav", "topnav"]):
            return

        if name in JUNK_TAGS:
            return

        if name in {"h1","h2","h3","h4","h5","h6"}:
            level = int(name[1])
            heading = el.get_text(" ", strip=True)
            emit("\n\n" + ("#" * level) + " " + heading + "\n\n")
            return

        if name == "pre":
            code = el.get_text("\n", strip=False)
            emit("\n\n```" + "\n" + code.rstrip() + "\n```\n\n")
            return

        if name == "table":
            emit("\n\n" + table_to_text(el) + "\n\n")
            return

        if name == "br":
            emit("\n")
            return

        if name == "li":
            txt = el.get_text(" ", strip=True)
            if txt:
                emit("\n- " + txt + "\n")
            return

        # Treat common block tags as newline boundaries
        is_block = name in {
            "p","div","section","article","main",
            "ul","ol","blockquote",
            "dl","dt","dd",
            "hr"
        }
        if name == "hr":
            emit("\n\n" + ("-" * 40) + "\n\n")
            return

        if is_block:
            emit("\n")

        for child in el.children:
            walk(child)

        if is_block:
            emit("\n")

    walk(root)
    return normalize("".join(out))

def url_to_outpath(url: str, root: str, outdir: Path) -> Path:
    # Map URL path under root to a .txt file under outdir
    rel = url[len(root):]
    # If rel is empty, name it index
    if not rel or rel.endswith("/"):
        rel = rel + "index.html"
    # Strip query for filenames
    rel = rel.split("?", 1)[0]
    rel_path = Path(rel)
    if rel_path.suffix.lower() in {".html", ".htm"}:
        rel_path = rel_path.with_suffix(".txt")
    else:
        rel_path = rel_path.with_suffix(rel_path.suffix + ".txt")
    return outdir / rel_path

def extract_links(html: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href)
        abs_url, _frag = urldefrag(abs_url)  # remove #...
        links.add(abs_url)
    return links

def fetch(session: requests.Session, url: str, timeout: int) -> str:
    r = session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR)
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    start = args.start
    root = args.root
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    q = deque([start])
    seen = set()
    saved = 0

    session = requests.Session()

    print(f"[+] Start: {start}")
    print(f"[+] Root:  {root}")
    print(f"[+] Out:   {outdir}")
    print(f"[+] Max pages: {args.max_pages}")

    while q and saved < args.max_pages:
        url = q.popleft()
        if url in seen:
            continue
        seen.add(url)

        if not is_within_root(url, root):
            continue
        if should_skip_url(url):
            continue

        try:
            if args.delay > 0:
                time.sleep(args.delay)
            html = fetch(session, url, args.timeout)
        except Exception as e:
            print(f"[!] Fetch failed: {url}\n    {e}", file=sys.stderr)
            continue

        # Convert to text and write
        text = html_to_text(html)
        outpath = url_to_outpath(url, root, outdir)
        outpath.parent.mkdir(parents=True, exist_ok=True)
        outpath.write_text(text, encoding="utf-8")

        saved += 1
        print(f"[✓] {saved:4d} saved: {outpath.relative_to(outdir)}")

        # Enqueue more links
        for link in extract_links(html, url):
            if link not in seen and is_within_root(link, root) and not should_skip_url(link):
                q.append(link)

    print(f"[✓] Done. Visited {len(seen)} pages, saved {saved} text files in {outdir}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        sys.exit(130)

