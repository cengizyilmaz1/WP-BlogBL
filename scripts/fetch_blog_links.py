import sys
import re
import gzip
import io
import datetime
from typing import Iterable, List, Tuple, Set, Dict

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://cengizyilmaz.net"
OUTPUT_FILE = "README.md"


def fetch_text(url: str, timeout_seconds: int = 15) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()
    return response.text


def read_text_maybe_gzip(url: str, timeout_seconds: int = 20) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=timeout_seconds)
    r.raise_for_status()
    if url.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
            return gz.read().decode("utf-8", errors="replace")
    # Respect content-encoding if server gzips transparently; requests handles it.
    return r.text


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


def extract_title(url: str) -> str:
    try:
        html_text = fetch_text(url)
    except Exception:
        return url
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.title and soup.title.text:
        return soup.title.text.strip()
    h1 = soup.find("h1")
    if h1 and h1.text:
        return h1.text.strip()
    return url


def discover_post_urls(base_url: str) -> List[str]:
    # Öncelik: sitemap üzerinden TÜM URL'ler (backlink için en kapsamlısı)
    sitemap_urls = [u for u in find_urls_from_sitemap(base_url) if u.startswith(base_url)]
    if sitemap_urls:
        return unique_keep_order(sitemap_urls)

    # Sitemap yoksa feed'ler (RSS tipik olarak 10-20 ile sınırlıdır)
    feed_urls = [u for u in find_urls_from_feeds(base_url) if u.startswith(base_url)]
    if feed_urls:
        return unique_keep_order(feed_urls)

    # Son çare: ana sayfadaki yazı benzeri linkleri al
    homepage_urls = [u for u in find_urls_from_homepage(base_url) if u.startswith(base_url)]
    homepage_post_like = [u for u in homepage_urls if is_likely_post(u)]
    return unique_keep_order(homepage_post_like or homepage_urls)


def build_readme(posts: List[Tuple[str, str]]) -> str:
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    total = len(posts)
    lines: List[str] = []
    # Başlık ve kestirme istatistikler
    lines.append("## cengizyilmaz.net Bağlantı Dizini")
    lines.append("")
    lines.append("Backlink amaçlı, sitedeki tüm sayfaların ve yazıların modern bir listesidir.")
    lines.append("")
    lines.append(f"- Kaynak: `{BASE_URL}`")
    lines.append("- Üretim: `scripts/fetch_blog_links.py` ile otomatik")
    lines.append(f"- Toplam: **{total}** | Son güncelleme: **{now_iso}**")
    lines.append("")
    lines.append("### Tüm Kayıtlar")
    lines.append("")
    # Sıralama: URL'e göre istikrarlı; istenirse alfabetik başlığa göre de yapılabilir
    for title, url in posts:
        safe_title = re.sub(r"\s+", " ", title).strip()
        if not safe_title or safe_title.lower() == url.lower():
            # Başlık bulunamadıysa URL'i kısaltarak göster
            safe_title = re.sub(r"^https?://", "", url).rstrip("/")
        lines.append(f"- [{safe_title}]({url})")
    lines.append("")
    lines.append("### Notlar")
    lines.append("")
    lines.append("- Tercihen `sitemap.xml`/`sitemap_index.xml` üzerinden TÜM URL'ler toplanır. RSS yalnızca sınırlı öğe içerir.")
    lines.append("- README her çalıştırmada baştan üretilir ve yalnızca değişiklik varsa commit edilir.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    print(f"[i] Keşif başlıyor: {BASE_URL}")
    urls = discover_post_urls(BASE_URL)
    if not urls:
        print("[!] Herhangi bir URL bulunamadı. Sitede sitemap/RSS olmayabilir.")
    else:
        print(f"[i] {len(urls)} aday URL bulundu. Başlıklar getiriliyor...")

    posts: List[Tuple[str, str]] = []
    for url in urls:
        title = extract_title(url)
        posts.append((title, url))

    readme_text = build_readme(posts)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(readme_text)
    print(f"[i] README güncellendi: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


