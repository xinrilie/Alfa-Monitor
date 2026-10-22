
from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent

CLEAN_JSON = BASE_DIR / "promo_catalog_module_a_clean.json"
RAW_JSON = BASE_DIR / "promo_catalog_module_a.json"

OUT_JSON = BASE_DIR / "promo_source_reference_audit.json"
OUT_CSV = BASE_DIR / "promo_source_reference_audit.csv"

TIMEOUT = 12
DELAY_SECONDS = 2
MAX_ARTICLES = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.7",
}

OFFICIAL_DOMAINS = (
    "alfamart.co.id",
    "alfagift.id",
)

SOCIAL_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "x.com",
    "twitter.com",
)

SOURCE_PATTERNS = re.compile(
    r"@alfamart|@alfagift|instagram|"
    r"akun resmi|sumber resmi|sumber dari|"
    r"dikutip dari|menurut alfamart|"
    r"akun alfamart|alfagift",
    re.IGNORECASE,
)

session = requests.Session()
session.headers.update(HEADERS)


def log(message: str) -> None:
    print(
        f"[{datetime.now().astimezone():%H:%M:%S}] {message}",
        flush=True,
    )


def host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.casefold().split(":")[0]

    return any(
        host == domain or host.endswith("." + domain)
        for domain in domains
    )


def atomic_write(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def save_json(data: dict) -> None:
    atomic_write(
        OUT_JSON,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_articles() -> list[dict]:
    input_file = CLEAN_JSON if CLEAN_JSON.exists() else RAW_JSON

    if not input_file.exists():
        raise FileNotFoundError(
            f"Katalog tidak ditemukan: {CLEAN_JSON.name} "
            f"atau {RAW_JSON.name}"
        )

    with input_file.open("r", encoding="utf-8") as f:
        catalog = json.load(f)

    articles = catalog.get("articles", [])

    if not isinstance(articles, list):
        raise ValueError("Key 'articles' bukan sebuah list.")

    unique = []
    seen = set()

    for article in articles:
        if not isinstance(article, dict):
            continue

        url = (
            article.get("source_url")
            or article.get("requested_url")
            or ""
        ).strip()

        if not url or url in seen:
            continue

        host = urlparse(url).hostname or ""

        # Audit hanya artikel Momsmoney yang sudah menjadi sumber
        # katalog proyek; tidak menjelajah domain lain.
        if not host_matches(host, ("kontan.co.id",)):
            continue

        seen.add(url)
        unique.append(article)

    return unique[:MAX_ARTICLES]


def choose_content(soup: BeautifulSoup):
    selectors = (
        ".entry-content",
        ".article-content",
        ".detail-content",
        ".read__content",
        "article",
        "main",
    )

    candidates = []

    for selector in selectors:
        candidates = soup.select(selector)
        if candidates:
            break

    if not candidates:
        return soup

    return max(
        candidates,
        key=lambda el: len(el.get_text(" ", strip=True)),
    )


def audit_article(article: dict) -> dict:
    url = (
        article.get("source_url")
        or article.get("requested_url")
        or ""
    ).strip()

    result = {
        "campaign_title": article.get("title", ""),
        "campaign_type": article.get("campaign_type", ""),
        "valid_from": article.get("valid_from", ""),
        "valid_until": article.get("valid_until", ""),
        "secondary_article_url": url,
        "page_status": "not_checked",
        "official_links": [],
        "source_reference_snippets": [],
        "source_provenance_status": "not_checked",
        "note": "",
    }

    try:
        response = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        result["page_status"] = "request_error"
        result["source_provenance_status"] = "article_unavailable"
        result["note"] = type(exc).__name__
        return result

    if response.status_code == 429:
        result["page_status"] = "http_429"
        result["source_provenance_status"] = "not_checked_due_to_429"
        result["note"] = (
            "Audit dihentikan untuk menghormati rate limit."
        )
        return result

    if response.status_code != 200:
        result["page_status"] = f"http_{response.status_code}"
        result["source_provenance_status"] = "article_unavailable"
        result["note"] = "Halaman artikel tidak dapat dibaca."
        return result

    result["page_status"] = "ok"

    soup = BeautifulSoup(response.text, "html.parser")
    content = choose_content(soup)

    snippets = []
    for node in content.select(
        "p, li, blockquote, figcaption, .source"
    ):
        text = re.sub(
            r"\s+",
            " ",
            node.get_text(" ", strip=True),
        ).strip()

        if text and SOURCE_PATTERNS.search(text):
            snippets.append(text[:500])

    result["source_reference_snippets"] = list(dict.fromkeys(snippets))

    links = []
    seen_links = set()

    for anchor in content.select("a[href]"):
        href = anchor.get("href", "").strip()

        if not href:
            continue

        host = urlparse(href).hostname or ""
        link_text = anchor.get_text(" ", strip=True)

        if host_matches(host, OFFICIAL_DOMAINS):
            kind = "official_website_link"
        elif host_matches(host, SOCIAL_DOMAINS):
            kind = "social_media_link_candidate"
        else:
            continue

        key = (href, kind)

        if key in seen_links:
            continue

        seen_links.add(key)

        links.append({
            "url": href,
            "host": host,
            "link_text": link_text[:200],
            "kind": kind,
            "owner_verified": False,
        })

    result["official_links"] = links

    if any(
        link["kind"] == "official_website_link"
        for link in links
    ):
        result["source_provenance_status"] = (
            "official_website_link_found"
        )
        result["note"] = (
            "Ada tautan ke domain resmi; isi dan harga promo "
            "tetap perlu dicocokkan."
        )

    elif any(
        link["kind"] == "social_media_link_candidate"
        for link in links
    ):
        result["source_provenance_status"] = (
            "social_source_link_candidate_found"
        )
        result["note"] = (
            "Tautan media sosial ditemukan, tetapi pemilik akun "
            "dan keaslian unggahan belum diverifikasi."
        )

    elif snippets:
        result["source_provenance_status"] = (
            "official_source_mentioned_no_direct_link"
        )
        result["note"] = (
            "Artikel menyebut sumber yang berhubungan dengan "
            "Alfamart/Alfagift, tetapi tautan langsung tidak ditemukan."
        )

    else:
        result["source_provenance_status"] = (
            "no_official_reference_found"
        )
        result["note"] = (
            "Tidak ditemukan tautan langsung atau penyebutan "
            "sumber resmi pada bagian artikel yang terbaca."
        )

    return result


def main() -> int:
    print("=" * 64)
    print("AUDIT REFERENSI SUMBER PROMO")
    print("=" * 64)

    try:
        articles = load_articles()
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1

    log(f"Artikel unik yang akan diperiksa: {len(articles)}")

    results = []

    for index, article in enumerate(articles, start=1):
        title = article.get("title") or article.get("campaign_type") or "-"

        log(f"[{index}/{len(articles)}] {title}")

        result = audit_article(article)
        results.append(result)

        log(
            f"  Status halaman: {result['page_status']} | "
            f"Referensi: {result['source_provenance_status']} | "
            f"Tautan: {len(result['official_links'])}"
        )

        # Hentikan jika Momsmoney sendiri membatasi permintaan.
        if result["page_status"] == "http_429":
            for remaining in articles[index:]:
                results.append({
                    "campaign_title": remaining.get("title", ""),
                    "campaign_type": remaining.get("campaign_type", ""),
                    "valid_from": remaining.get("valid_from", ""),
                    "valid_until": remaining.get("valid_until", ""),
                    "secondary_article_url": (
                        remaining.get("source_url")
                        or remaining.get("requested_url", "")
                    ),
                    "page_status": "not_checked_due_to_429",
                    "official_links": [],
                    "source_reference_snippets": [],
                    "source_provenance_status": "not_checked_due_to_429",
                    "note": (
                        "Tidak diakses karena rate limit ditemukan."
                    ),
                })
            break

        time.sleep(DELAY_SECONDS)

    report = {
        "checked_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "purpose": (
            "Menemukan referensi sumber resmi yang dicantumkan "
            "pada artikel sekunder. Bukan verifikasi harga."
        ),
        "article_count": len(results),
        "results": results,
    }

    save_json(report)

    fields = [
        "campaign_title",
        "campaign_type",
        "valid_from",
        "valid_until",
        "secondary_article_url",
        "page_status",
        "source_provenance_status",
        "official_links",
        "source_reference_snippets",
        "note",
    ]

    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
        extrasaction="ignore",
    )
    writer.writeheader()

    for row in results:
        writer.writerow({
            **row,
            "official_links": json.dumps(
                row["official_links"], ensure_ascii=False
            ),
            "source_reference_snippets": json.dumps(
                row["source_reference_snippets"],
                ensure_ascii=False,
            ),
        })

    atomic_write(OUT_CSV, buffer.getvalue())

    print()
    print("=" * 64)
    print("AUDIT SELESAI")
    print(f"Artikel diperiksa: {len(results)}")
    print(f"JSON: {OUT_JSON}")
    print(f"CSV : {OUT_CSV}")
    print("=" * 64)

    print(
        "Catatan: ditemukan tautan atau penyebutan sumber bukan "
        "berarti harga produk sudah terverifikasi."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
