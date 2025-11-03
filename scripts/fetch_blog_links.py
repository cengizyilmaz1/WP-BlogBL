import sys
import re
import gzip
import io
import time
import datetime
from typing import Iterable, List, Tuple, Set, Dict, Optional

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://cengizyilmaz.net"
OUTPUT_FILE = "README.md"


def fetch_text(url: str, timeout_seconds: int = 15, max_retries: int = 3) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }
    backoff = 0.5
    for attempt in range(max_retries):
        time.sleep(0.25)
        response = requests.get(url, headers=headers, timeout=timeout_seconds)
        if response.status_code == 429 and attempt < max_retries - 1:
            time.sleep(backoff)
            backoff *= 2
            continue
        response.raise_for_status()
        return response.text
    return response.text


def read_text_maybe_gzip(url: str, timeout_seconds: int = 20) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }
    time.sleep(0.25)
    r = requests.get(url, headers=headers, timeout=timeout_seconds)
    r.raise_for_status()
    if url.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
            return gz.read().decode("utf-8", errors="replace")
    # Respect content-encoding if server gzips transparently; requests handles it.
    return r.text


POST_PATTERNS = [r"/\d{4}/\d{2}/", r"/blog/", r"/posts?/", r"/yaz(i|ı|ılar)/"]
EXCLUDE_PATTERNS = [r"/tag/", r"/(kategori|category)/", r"/etiket/", r"/page/\d+/?$", r"\?s="]


def is_post_url(url: str) -> bool:
    import re as _re
    low = url.lower()
    if any(_re.search(p, low) for p in EXCLUDE_PATTERNS):
        return False
    return any(_re.search(p, low) for p in POST_PATTERNS)


def collect_urls_from_sitemap_url(sitemap_url: str, visited: Set[str]) -> List[str]:
    # Avoid cycles
    if sitemap_url in visited:
        return []
    visited.add(sitemap_url)
    try:
        xml_text = read_text_maybe_gzip(sitemap_url)
    except Exception:
        return []
    soup = BeautifulSoup(xml_text, "xml")
    urls: List[str] = []
    # If this is a sitemap index, iterate child sitemaps
    if soup.find("sitemapindex"):
        for sm in soup.find_all("sitemap"):
            loc_tag = sm.find("loc")
            if not loc_tag or not loc_tag.text:
                continue
            child_url = loc_tag.text.strip()
            urls.extend(collect_urls_from_sitemap_url(child_url, visited))
        return urls
    # Otherwise assume urlset
    for u in soup.find_all("url"):
        loc_tag = u.find("loc")
        if loc_tag and loc_tag.text:
            urls.append(loc_tag.text.strip())
    # Some non-standard sitemaps only have <loc> under root
    if not urls:
        for loc_tag in soup.find_all("loc"):
            text = (loc_tag.text or "").strip()
            if text:
                urls.append(text)
    return urls


def find_sitemap_candidates_from_robots(base_url: str) -> List[str]:
    robots_url = f"{base_url.rstrip('/')}/robots.txt"
    try:
        text = fetch_text(robots_url)
    except Exception:
        return []
    candidates: List[str] = []
    for line in text.splitlines():
        if line.lower().startswith("sitemap:"):
            val = line.split(":", 1)[1].strip()
            if val:
                candidates.append(val)
    return candidates


def find_urls_from_sitemap(base_url: str) -> List[str]:
    candidates = [
        f"{base_url.rstrip('/')}/sitemap.xml",
        f"{base_url.rstrip('/')}/sitemap_index.xml",
    ]
    candidates.extend(find_sitemap_candidates_from_robots(base_url))
    # Keep order and unique
    candidates = unique_keep_order(candidates)
    visited: Set[str] = set()
    urls: List[str] = []
    for url in candidates:
        urls.extend(collect_urls_from_sitemap_url(url, visited))
    return unique_keep_order(urls)


def is_likely_post(url: str) -> bool:
    path = url.lower()
    # Kept for homepage fallback filtering; sitemap artık TÜM URL'leri getiriyor.
    post_hints = ["/blog/", "/yazi/", "/yazilar/", "/post/", "/posts/", "/articles/", "/notlar/"]
    return any(h in path for h in post_hints)


def find_urls_from_feeds(base_url: str) -> List[str]:
    feed_paths = ["/feed", "/rss.xml", "/atom.xml", "/feed.xml"]
    urls: List[str] = []
    for path in feed_paths:
        feed_url = f"{base_url.rstrip('/')}{path}"
        try:
            xml_text = fetch_text(feed_url)
        except Exception:
            continue
        soup = BeautifulSoup(xml_text, "xml")
        # RSS item > link
        for item in soup.find_all("item"):
            link_tag = item.find("link")
            if link_tag and link_tag.text:
                urls.append(link_tag.text.strip())
        # Atom entry > link href
        for entry in soup.find_all("entry"):
            link_tag = entry.find("link")
            href = link_tag.get("href") if link_tag else None
            if href:
                urls.append(href.strip())
    return urls


def find_urls_from_homepage(base_url: str) -> List[str]:
    try:
        html_text = fetch_text(base_url)
    except Exception:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    urls: Set[str] = set()
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = f"{base_url.rstrip('/')}{href}"
        if href.startswith(base_url):
            urls.add(href)
    return list(urls)


def unique_keep_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extract_metadata(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (title, description, published_iso)."""
    try:
        html_text = fetch_text(url)
    except Exception:
        return url, None, None
    soup = BeautifulSoup(html_text, "html.parser")

    # Title: og:title -> h1 -> <title>
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
            title = url

    # Description: og:description -> meta name=description -> first <p>
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
        if len(desc) > 200:
            desc = desc[:197] + "..."

    # Date: article:published_time -> <time datetime>
    pub_iso: Optional[str] = None
    ap = soup.find("meta", property="article:published_time")
    if ap and ap.get("content"):
        pub_iso = ap["content"].strip()
    else:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            pub_iso = time_tag["datetime"].strip()

    return title, desc, pub_iso


def discover_post_urls(base_url: str) -> List[str]:
    # Öncelik: sitemap üzerinden TÜM URL'ler (backlink için en kapsamlısı)
    sitemap_urls = [u for u in find_urls_from_sitemap(base_url) if u.startswith(base_url)]
    sitemap_urls = [u for u in sitemap_urls if is_post_url(u)]
    if sitemap_urls:
        return unique_keep_order(sitemap_urls)

    # Sitemap yoksa feed'ler (RSS tipik olarak 10-20 ile sınırlıdır)
    feed_urls = [u for u in find_urls_from_feeds(base_url) if u.startswith(base_url)]
    feed_urls = [u for u in feed_urls if is_post_url(u)]
    if feed_urls:
        return unique_keep_order(feed_urls)

    # Son çare: ana sayfadaki yazı benzeri linkleri al
    homepage_urls = [u for u in find_urls_from_homepage(base_url) if u.startswith(base_url)]
    homepage_post_like = [u for u in homepage_urls if is_post_url(u)]
    return unique_keep_order(homepage_post_like or homepage_urls)


def build_readme(posts: List[Tuple[str, str, Optional[str], Optional[str]]]) -> str:
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

    def guess_year_from_url(u: str) -> str:
        m = re.search(r"/(20\d{2})/", u)
        return m.group(1) if m else "Diğer"

    def year_from_iso(iso: Optional[str], url: str) -> str:
        if iso:
            m = re.match(r"(\d{4})-", iso)
            if m:
                return m.group(1)
        return guess_year_from_url(url)

    grouped: Dict[str, List[Tuple[str, str, Optional[str], Optional[str]]]] = {}
    for title, url, pub_iso, desc in posts:
        y = year_from_iso(pub_iso, url)
        grouped.setdefault(y, []).append((title, url, pub_iso, desc))

    total = len(posts)
    lines: List[str] = []
    lines.append("## cengizyilmaz.net — Öne Çıkan Yazılar Dizini")
    lines.append("")
    lines.append("Bu repoda Exchange/AD ve diğer teknik konulardaki makalelerimin özetlerini ve bağlantılarını bulursunuz. Backlink ve hızlı keşif için hazırlanmıştır.")
    lines.append("")
    lines.append(f"- Kaynak: `{BASE_URL}`")
    lines.append(f"- Toplam: **{total}** | Son güncelleme: **{now_iso}**")
    lines.append("")
    for year in sorted(grouped.keys(), reverse=True):
        lines.append(f"### {year}")
        lines.append("")
        for title, url, pub_iso, desc in grouped[year]:
            safe_title = re.sub(r"\s+", " ", title or url).strip()
            if not safe_title or safe_title.lower() == url.lower():
                safe_title = re.sub(r"^https?://", "", url).rstrip("/")
            date_prefix = ""
            if pub_iso and re.match(r"^\d{4}-\d{2}-\d{2}", pub_iso):
                date_prefix = pub_iso[:10] + " — "
            line = f"- {date_prefix}**{safe_title}** — [{url}]({url})"
            if desc:
                line += f": {desc}"
            lines.append(line)
        lines.append("")
    lines.append("> Not: Yalnızca makaleler listelenir; kategori/etiket sayfaları hariçtir.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    print(f"[i] Keşif başlıyor: {BASE_URL}")
    urls = discover_post_urls(BASE_URL)
    if not urls:
        print("[!] Herhangi bir URL bulunamadı. Sitede sitemap/RSS olmayabilir.")
    else:
        print(f"[i] {len(urls)} aday URL bulundu. Başlıklar getiriliyor...")

    posts: List[Tuple[str, str, Optional[str], Optional[str]]] = []
    for url in urls:
        title, desc, pub_iso = extract_metadata(url)
        posts.append((title, url, pub_iso, desc))

    readme_text = build_readme(posts)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(readme_text)
    print(f"[i] README güncellendi: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


