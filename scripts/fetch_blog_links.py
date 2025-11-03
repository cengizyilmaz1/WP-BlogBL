import sys
import re
import datetime
from typing import Iterable, List, Tuple, Set

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


def find_urls_from_sitemap(base_url: str) -> List[str]:
    candidates = [
        f"{base_url.rstrip('/')}/sitemap.xml",
        f"{base_url.rstrip('/')}/sitemap_index.xml",
    ]
    urls: List[str] = []
    for url in candidates:
        try:
            xml_text = fetch_text(url)
        except Exception:
            continue
        soup = BeautifulSoup(xml_text, "xml")
        loc_tags = soup.find_all("loc")
        for tag in loc_tags:
            loc = (tag.text or "").strip()
            if loc:
                urls.append(loc)
    return urls


def is_likely_post(url: str) -> bool:
    path = url.lower()
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
    urls: List[str] = []

    sitemap_urls = find_urls_from_sitemap(base_url)
    post_like_sitemap = [u for u in sitemap_urls if is_likely_post(u)]
    if post_like_sitemap:
        urls.extend(post_like_sitemap)

    if not urls:
        feed_urls = find_urls_from_feeds(base_url)
        urls.extend(feed_urls)

    if not urls:
        homepage_urls = find_urls_from_homepage(base_url)
        urls.extend([u for u in homepage_urls if is_likely_post(u)])

    urls = [u for u in urls if u.startswith(base_url)]
    urls = unique_keep_order(urls)
    return urls


def build_readme(posts: List[Tuple[str, str]]) -> str:
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    total = len(posts)
    lines: List[str] = []
    lines.append("## cengizyilmaz.net Bağlantı Dizini")
    lines.append("")
    lines.append("Bu depo, `cengizyilmaz.net` üzerindeki yazıların konu başlıklarını ve bağlantılarını bir araya getirir. Arama motorlarına yardımcı olacak hafif bir backlink listesi olarak tasarlanmıştır.")
    lines.append("")
    lines.append(f"- Kaynak: `{BASE_URL}`")
    lines.append("- Üretim: `scripts/fetch_blog_links.py` ile otomatik oluşturulur")
    lines.append("")
    lines.append("### Güncel Liste")
    lines.append("")
    lines.append(f"Toplam {total} kayıt | Son güncelleme: {now_iso}")
    lines.append("")
    for title, url in posts:
        safe_title = re.sub(r"\s+", " ", title).strip()
        lines.append(f"- [{safe_title}]({url})")
    lines.append("")
    lines.append("### Notlar")
    lines.append("")
    lines.append("- Betik önce `sitemap.xml` üzerinden yazı linklerini bulmayı dener; uygun değilse RSS/Atom kayıtlarına ve son çare olarak ana sayfadaki bağlantılara başvurur.")
    lines.append("- README içeriği her çalıştırmada yeniden üretilir.")
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


