import sys
import re
import gzip
import io
import datetime
from typing import Iterable, List, Tuple, Set, Dict, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

# ---------------- Config ----------------

BASE_URL = "https://cengizyilmaz.net"
OUTPUT_FILE = "README.md"

MAX_WORKERS = 8             # parallel workers for metadata fetch
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 20
MAX_URLS = 10000            # safety cap

# Common WordPress permalinks for posts:
POST_PATTERNS = [
    r"/\d{4}/\d{2}/",       # /YYYY/MM/...
    r"/blog/",
    r"/posts?/",
    r"/yaz(i|ı|ilar|ılar)/",
]

# Exclude non-post paths:
EXCLUDE_PATTERNS = [
    r"/tag/",
    r"/(kategori|category)/",
    r"/etiket/",
    r"/author/",
    r"/page/\d+/?$",
    r"\?s=",                # search
    r"/(attachment|amp)/",  # common non-canonical variants
]

# If a sitemap URL contains any of these, treat it as "post-only" sitemap:
POST_SITEMAP_HINTS = [
    "post-sitemap", "posts-sitemap",
    "sitemap-posttype-post",
    "sitemap-posts",
]

# ------------- HTTP Session -------------

def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.8,tr-TR,tr;q=0.7",
    })

    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

SESSION = build_session()

def fetch_text(url: str, timeout_seconds: int = READ_TIMEOUT) -> str:
    r = SESSION.get(url, timeout=(CONNECT_TIMEOUT, timeout_seconds))
    r.raise_for_status()
    return r.text

def fetch_json(url: str, timeout_seconds: int = READ_TIMEOUT) -> dict:
    r = SESSION.get(url, timeout=(CONNECT_TIMEOUT, timeout_seconds))
    r.raise_for_status()
    return r.json()

def read_text_maybe_gzip(url: str, timeout_seconds: int = READ_TIMEOUT) -> str:
    r = SESSION.get(url, timeout=(CONNECT_TIMEOUT, timeout_seconds))
    r.raise_for_status()
    if url.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
            return gz.read().decode("utf-8", errors="replace")
    return r.text

# ------------- URL helpers --------------

def unique_keep_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out

def strip_utm(u: str) -> str:
    p = urlparse(u)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(qs)))

def normalize_url(u: str) -> str:
    u = strip_utm(u)
    p = urlparse(u)
    scheme = p.scheme.lower() or "https"
    netloc = p.netloc.lower()
    path = p.path or "/"
    # prefer trailing slash for canonical consistency
    if not path.endswith("/"):
        path += "/"
    return urlunparse((scheme, netloc, path, p.params, p.query, p.fragment))

def is_post_url(url: str) -> bool:
    low = url.lower()
    if any(re.search(p, low) for p in EXCLUDE_PATTERNS):
        return False
    return any(re.search(p, low) for p in POST_PATTERNS)

# -------- WordPress REST (preferred) ----

def try_fetch_all_posts_via_wpapi(base_url: str) -> List[Tuple[str, str, Optional[str], Optional[str]]]:
    """
    Returns list of (title, canonical_url, published_iso, description) using WP REST API.
    If REST API not available, returns empty list.
    """
    api = f"{base_url.rstrip('/')}/wp-json/wp/v2/posts"
    params = {
        "per_page": 100,
        "_fields": "title,link,date,excerpt",
        "status": "publish",
        "orderby": "date",
        "order": "desc",
        "page": 1,
    }
    results: List[Tuple[str, str, Optional[str], Optional[str]]] = []

    try:
        while True:
            url = f"{api}?{urlencode(params)}"
            data = fetch_json(url)
            if not isinstance(data, list) or not data:
                break
            for it in data:
                title = it.get("title", {}).get("rendered") or it.get("title") or ""
                link = normalize_url(it.get("link") or "")
                pub = it.get("date") or None
                # excerpt may be HTML; strip tags quickly
                excerpt_html = (it.get("excerpt", {}) or {}).get("rendered") or ""
                excerpt_text = BeautifulSoup(excerpt_html, "html.parser").get_text(" ", strip=True) if excerpt_html else None
                if excerpt_text and len(excerpt_text) > 180:
                    excerpt_text = excerpt_text[:177] + "..."
                results.append((title.strip() or link, link, pub, excerpt_text))
            # next page
            params["page"] += 1
    except Exception:
        # Not WP or blocked → fallback to sitemaps
        return []
    return results

# --------------- Sitemaps ----------------

def find_sitemap_candidates_from_robots(base_url: str) -> List[str]:
    robots_url = f"{base_url.rstrip('/')}/robots.txt"
    try:
        text = fetch_text(robots_url)
    except Exception:
        return []
    cands: List[str] = []
    for line in text.splitlines():
        if line.lower().startswith("sitemap:"):
            val = line.split(":", 1)[1].strip()
            if val:
                cands.append(val)
    return cands

def collect_urls_from_sitemap(sitemap_url: str) -> List[str]:
    """Return the <loc> URLs from a single urlset sitemap (not index)."""
    try:
        xml = read_text_maybe_gzip(sitemap_url)
    except Exception:
        return []
    soup = BeautifulSoup(xml, "xml")
    out: List[str] = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if loc and loc.text:
            out.append(loc.text.strip())
    if not out:
        # Some minimal sitemaps place <loc> under root
        for loc in soup.find_all("loc"):
            if loc and loc.text:
                out.append(loc.text.strip())
    return out

def expand_sitemap_index(index_url: str) -> List[str]:
    """Expand a sitemap index into child sitemap URLs."""
    try:
        xml = read_text_maybe_gzip(index_url)
    except Exception:
        return []
    soup = BeautifulSoup(xml, "xml")
    child_urls: List[str] = []
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc and loc.text:
            child_urls.append(loc.text.strip())
    return child_urls

def find_all_sitemaps(base_url: str) -> List[str]:
    cands = [
        f"{base_url.rstrip('/')}/sitemap.xml",
        f"{base_url.rstrip('/')}/sitemap_index.xml",
        *find_sitemap_candidates_from_robots(base_url),
    ]
    return unique_keep_order(cands)

def urls_from_post_sitemaps_only(base_url: str) -> List[str]:
    """
    If the site exposes post-only sitemaps (e.g., post-sitemap.xml),
    use ONLY those to ensure we truly get posts (not categories/tags).
    """
    out: List[str] = []
    seen: Set[str] = set()
    for sm in find_all_sitemaps(base_url):
        # Expand indexes:
        work = [sm]
        if sm.endswith("sitemap_index.xml"):
            work = expand_sitemap_index(sm) or [sm]
        for w in work:
            lw = w.lower()
            if any(h in lw for h in POST_SITEMAP_HINTS):
                for loc in collect_urls_from_sitemap(w):
                    if loc not in seen:
                        seen.add(loc)
                        out.append(loc)
    return unique_keep_order(out)

def urls_from_generic_sitemaps(base_url: str) -> List[str]:
    """
    Generic sitemap crawl (index → all urlsets). Then filter to posts.
    """
    out: List[str] = []
    seen: Set[str] = set()
    for sm in find_all_sitemaps(base_url):
        # First, try expanding index:
        children = expand_sitemap_index(sm)
        if children:
            for ch in children:
                for loc in collect_urls_from_sitemap(ch):
                    if loc not in seen:
                        seen.add(loc)
                        out.append(loc)
        else:
            for loc in collect_urls_from_sitemap(sm):
                if loc not in seen:
                    seen.add(loc)
                    out.append(loc)
    # Keep only posts under the same base
    out = [u for u in out if u.startswith(base_url)]
    out = [u for u in out if is_post_url(u)]
    return unique_keep_order(out)

def discover_all_post_urls(base_url: str) -> List[str]:
    """
    Ensure we collect ALL posts:
    1) Try WordPress REST (complete, exact).
    2) Else try post-only sitemaps (post-sitemap.xml, etc.).
    3) Else generic sitemaps filtered by post patterns.
    """
    # 1) WordPress REST API (returns normalized links directly)
    wp_posts = try_fetch_all_posts_via_wpapi(base_url)
    if wp_posts:
        print(f"[i] Source: WordPress REST API | posts: {len(wp_posts)}")
        return unique_keep_order([p[1] for p in wp_posts])  # just URLs; metadata will be refetched below

    # 2) Post-only sitemaps
    post_sitemap_urls = urls_from_post_sitemaps_only(base_url)
    if post_sitemap_urls:
        print(f"[i] Source: post-only sitemaps | urls: {len(post_sitemap_urls)}")
        return unique_keep_order([normalize_url(u) for u in post_sitemap_urls])

    # 3) Generic sitemaps + post filter
    generic = urls_from_generic_sitemaps(base_url)
    if generic:
        print(f"[i] Source: generic sitemaps | urls: {len(generic)}")
        return unique_keep_order([normalize_url(u) for u in generic])

    print("[!] No URLs discovered. Is sitemap/RSS/API available?")
    return []

# ------------- Metadata ------------------

def extract_canonical(soup: BeautifulSoup, fallback_url: str) -> str:
    link = soup.find("link", rel=lambda v: v and "canonical" in v.lower())
    cand = (link.get("href") or "").strip() if link else ""
    return cand or fallback_url

def extract_metadata(url: str) -> Tuple[str, Optional[str], Optional[str], str]:
    """
    Returns: (title, description, published_iso, canonical_url)
    """
    try:
        html = fetch_text(url)
    except Exception:
        n = normalize_url(url)
        return n, None, None, n

    soup = BeautifulSoup(html, "html.parser")

    canon = normalize_url(extract_canonical(soup, url))

    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    else:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(strip=True)
        elif soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(strip=True)
        else:
            title = canon

    desc: Optional[str] = None
    ogd = soup.find("meta", property="og:description")
    if ogd and ogd.get("content"):
        desc = ogd["content"].strip()
    else:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            desc = md["content"].strip()
        else:
            p = soup.find("p")
            if p and p.get_text(strip=True):
                desc = p.get_text(strip=True)
    if desc:
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 180:
            desc = desc[:177] + "..."

    pub_iso: Optional[str] = None
    ap = soup.find("meta", property="article:published_time")
    if ap and ap.get("content"):
        pub_iso = ap["content"].strip()
    else:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            pub_iso = time_tag["datetime"].strip()

    return title, desc, pub_iso, canon

def sitemap_lastmods(base_url: str) -> Dict[str, str]:
    """
    Build a map of URL -> lastmod from all sitemaps (helps when no published_time is present).
    """
    mp: Dict[str, str] = {}
    for sm in find_all_sitemaps(base_url):
        try:
            xml = read_text_maybe_gzip(sm)
        except Exception:
            continue
        s = BeautifulSoup(xml, "xml")
        for u in s.find_all("url"):
            loc_tag = u.find("loc")
            if not loc_tag or not loc_tag.text:
                continue
            loc = normalize_url(loc_tag.text.strip())
            lm_tag = u.find("lastmod")
            if lm_tag and lm_tag.text:
                mp[loc] = lm_tag.text.strip()
    return mp

# ------------- README --------------------

def build_readme(posts: List[Tuple[str, str, Optional[str], Optional[str]]]) -> str:
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

    def year_of(pub: Optional[str], url: str) -> str:
        if pub and re.match(r"^\d{4}-\d{2}-\d{2}", pub):
            return pub[:4]
        m = re.search(r"/(20\d{2})/", url)
        return m.group(1) if m else "Other"

    def sort_key(p):
        _title, _url, pub, _desc = p
        return pub[:10] if pub else "1900-01-01"

    posts_sorted = sorted(posts, key=sort_key, reverse=True)
    latest = posts_sorted[:50]

    grouped: Dict[str, List[Tuple[str, str, Optional[str], Optional[str]]]] = {}
    for title, url, pub_iso, desc in posts_sorted:
        y = year_of(pub_iso, url)
        grouped.setdefault(y, []).append((title, url, pub_iso, desc))

    total = len(posts)
    lines: List[str] = []
    lines.append("## cengizyilmaz.net — Posts Index")
    lines.append("")
    lines.append("This repository curates links to my technical articles (Exchange, AD, Microsoft 365, etc.) for discovery and reference.")
    lines.append("")
    lines.append(f"- Source: `{BASE_URL}`")
    lines.append(f"- Total: **{total}** | Last updated: **{now_iso}**")
    lines.append("")

    # Latest 50
    lines.append("### Latest 50")
    lines.append("")
    for title, url, pub_iso, desc in latest:
        safe_title = re.sub(r"\s+", " ", title or url).strip()
        if not safe_title or safe_title.lower() == url.lower():
            safe_title = re.sub(r"^https?://", "", url).rstrip("/")
        date_prefix = f"{pub_iso[:10]} — " if pub_iso and re.match(r"^\d{4}-\d{2}-\d{2}", pub_iso) else ""
        line = f"- {date_prefix}**{safe_title}** — [{url}]({url})"
        if desc:
            line += f": {desc}"
        lines.append(line)
    lines.append("")

    # By year
    for year in sorted(grouped.keys(), reverse=True):
        lines.append(f"### {year} ({len(grouped[year])})")
        lines.append("")
        for title, url, pub_iso, desc in grouped[year]:
            safe_title = re.sub(r"\s+", " ", title or url).strip()
            if not safe_title or safe_title.lower() == url.lower():
                safe_title = re.sub(r"^https?://", "", url).rstrip("/")
            date_prefix = f"{pub_iso[:10]} — " if pub_iso and re.match(r'^\d{4}-\d{2}-\d{2}', pub_iso) else ""
            line = f"- {date_prefix}**{safe_title}** — [{url}]({url})"
            if desc:
                line += f": {desc}"
            lines.append(line)
        lines.append("")

    lines.append("> Note: Only **post** URLs are listed; taxonomy/search/pagination URLs are excluded.")
    lines.append("")
    return "\n".join(lines)

# --------------- Main --------------------

def main() -> int:
    print(f"[i] Discovering posts for: {BASE_URL}")

    # Collect URLs (prefer WP API if available)
    urls = discover_all_post_urls(BASE_URL)
    if not urls:
        print("[!] No post URLs discovered.")
        return 1

    # Optional: safety cap
    if len(urls) > MAX_URLS:
        urls = urls[:MAX_URLS]

    # Map of URL -> lastmod from sitemaps (used as fallback date)
    lastmods = sitemap_lastmods(BASE_URL)

    # Fetch metadata in parallel
    raw: List[Tuple[str, str, Optional[str], Optional[str], str]] = []  # title, url, pub, desc, canon
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(extract_metadata, u): u for u in urls}
        for f in as_completed(futures):
            try:
                title, desc, pub_iso, canon = f.result()
            except Exception as e:
                u = futures[f]
                n = normalize_url(u)
                print(f"[warn] Metadata failed: {u} -> {e}")
                title, desc, pub_iso, canon = n, None, None, n
            raw.append((title, canon, pub_iso, desc, canon))

    # Canonical de-dup + lastmod fallback
    seen: Set[str] = set()
    posts: List[Tuple[str, str, Optional[str], Optional[str]]] = []
    for title, url, pub_iso, desc, canon in raw:
        key = normalize_url(canon)
        if key in seen:
            continue
        seen.add(key)
        if not pub_iso:
            pub_iso = lastmods.get(key)
        posts.append((title, key, pub_iso, desc))

    readme = build_readme(posts)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(readme)

    print(f"[i] README updated: {OUTPUT_FILE} (total {len(posts)} posts)")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as e:
        print(f"[err] HTTPError: {e}")
        sys.exit(2)
    except Exception as e:
        print(f"[err] {e}")
        sys.exit(3)
