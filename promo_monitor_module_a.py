from __future__ import annotations

import csv
import json
import re
import time

from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, urlencode
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# =========================================================
# KONFIGURASI
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

OUT_JSON = BASE_DIR / "promo_catalog_module_a.json"
OUT_CSV = BASE_DIR / "promo_catalog_products.csv"
RUN_LOG = BASE_DIR / "promo_catalog_runlog.json"
ATTEMPT_JSON = BASE_DIR / "promo_catalog_latest_attempt.json"

HISTORY_DIR = BASE_DIR / "promo_catalog_history"
MANUAL_URLS = BASE_DIR / "promo_sources.txt"

TODAY = datetime.now(ZoneInfo("Asia/Jakarta")).date()

TIMEOUT = 15
DELAY = 0.4

MAX_LISTING_PAGES = 4
MAX_ARTICLES = 18
SEARCH_WINDOW_DAYS = 45

MIN_SUCCESSFUL_ARTICLES = 2
MIN_PRODUCT_ROWS = 5

ARTICLE_HOST = "momsmoney.kontan.co.id"

HEMAT_CATEGORY_URL = (
    "https://momsmoney.kontan.co.id/rubrik/342/Hemat"
)

OFFICIAL_SOURCES = [
    ("Alfamart In-Store", "https://alfamart.co.id/instore"),
    ("Instagram Alfamart", "https://www.instagram.com/alfamart/"),
    ("Alfagift Promo", "https://alfagift.id/promotion-products"),
    ("Alfagift Campaign", "https://web-campaign.alfagift.id/promo"),
]

MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}

OFFER_PATTERN = re.compile(
    r"\bRp\.?\s*\d|"
    r"\bgratis\b|"
    r"\bbeli\s+\d|"
    r"\bdiskon\b|"
    r"\bharga\s+(?:promo|normal|spesial)\b|"
    r"\btebus\s+murah\b",
    re.IGNORECASE,
)

NOISE_PREFIX = re.compile(
    r"^(?:"
    r"buruan\b|bagi moms\b|selain\b|supaya\b|nah\b|"
    r"melansir\b|pembayaran\b|khusus pembayaran\b|"
    r"berlaku untuk\b|maksimal\b|tidak berlaku\b|"
    r"minimal belanja\b|diskon aplikasi\b|"
    r"baca juga\b|cek berita\b|"
    r"promo alfamart\b|promo indomaret\b|"
    r"kuota harian\b|syarat dan ketentuan\b"
    r")",
    re.IGNORECASE,
)


# =========================================================
# UTILITAS
# =========================================================

def say(message: str) -> None:
    print(message, flush=True)


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Jakarta")).isoformat()


def normalize_text(value) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_article_url(url: str) -> str:
    """Buang fragment dan parameter agar identitas artikel stabil."""
    if not url:
        return ""

    url, _ = urldefrag(url.strip())
    parsed = urlparse(url)

    if (parsed.hostname or "").lower() != ARTICLE_HOST:
        return ""

    if not parsed.path.startswith("/news/"):
        return ""

    return parsed._replace(
        scheme="https",
        netloc=ARTICLE_HOST,
        query="",
        fragment="",
    ).geturl().rstrip("/")


def is_article_url(url: str) -> bool:
    parsed = urlparse(url)

    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() == ARTICLE_HOST
        and parsed.path.startswith("/news/")
    )


def is_relevant_title(title: str) -> bool:
    """
    Hanya artikel yang jelas menyebut promo Alfamart/Alfagift.
    Judul Indomaret atau berita umum harus ditolak.
    """
    text = normalize_text(title)

    if "indomaret" in text:
        return False

    if not (
        "alfamart" in text
        or "alfagift" in text
    ):
        return False

    keywords = (
        "promo",
        "promosi",
        "katalog",
        "jsm",
        "psm",
        "serba gratis",
        "paling murah",
        "minyak goreng hemat",
        "gantung gajian",
        "mie time",
        "powder milk",
        "harga spesial",
        "diskon",
    )

    return any(word in text for word in keywords)


def campaign_type(title: str) -> str:
    text = normalize_text(title)

    patterns = [
        ("Serba Gratis", r"serba gratis"),
        ("JSM", r"\bjsm\b"),
        ("PSM", r"\bpsm\b"),
        ("Paling Murah Sejagat", r"paling murah"),
        ("Minyak Goreng Hemat", r"minyak goreng hemat"),
        ("Gantung / Gajian Untung", r"gantung|gajian untung"),
        ("Mie Time", r"mie time"),
        ("Powder Milk Fair", r"powder milk"),
        ("Alfagift", r"alfagift"),
        ("Carnaval Ice Cream", r"carnaval ice cream"),
        ("Es Krim", r"es krim"),
        ("Home Care", r"home care"),
        ("Body Care", r"body care"),
    ]

    for label, pattern in patterns:
        if re.search(pattern, text):
            return label

    return "Lainnya"


def rupiah_numbers(text: str) -> list[int]:
    matches = re.findall(
        r"\bRp\.?\s*([0-9][0-9.,]*)",
        text,
        flags=re.IGNORECASE,
    )

    result = []

    for value in matches:
        digits = re.sub(r"\D", "", value)

        if digits:
            result.append(int(digits))

    return result


def labeled_price(
    text: str,
    label_pattern: str,
) -> int | None:
    match = re.search(
        rf"(?:{label_pattern})\s*[:\-]?\s*"
        rf"Rp\.?\s*([0-9][0-9.,]*)",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


# =========================================================
# PERIODE PROMO
# =========================================================

def parse_period_from_text(
    text: str,
) -> tuple[str | None, str | None]:
    text = normalize_text(text)
    month_pattern = "|".join(MONTHS)

    patterns = [
        (
            "cross_month",
            rf"\b(\d{{1,2}})\s+({month_pattern})\s*"
            rf"[-–—]\s*(\d{{1,2}})\s+({month_pattern})"
            rf"\s+(\d{{4}})\b",
        ),
        (
            "same_month",
            rf"\b(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}})"
            rf"\s+({month_pattern})\s+(\d{{4}})\b",
        ),
        (
            "single_day",
            rf"\b(\d{{1,2}})\s+({month_pattern})"
            rf"\s+(\d{{4}})\b",
        ),
    ]

    for pattern_type, pattern in patterns:
        match = re.search(pattern, text)

        if not match:
            continue

        try:
            if pattern_type == "cross_month":
                d1, m1, d2, m2, year = match.groups()

                start = date(
                    int(year), MONTHS[m1], int(d1)
                )
                end = date(
                    int(year), MONTHS[m2], int(d2)
                )

            elif pattern_type == "same_month":
                d1, d2, month_name, year = match.groups()
                month = MONTHS[month_name]

                start = date(int(year), month, int(d1))
                end = date(int(year), month, int(d2))

            else:
                day, month_name, year = match.groups()

                start = date(
                    int(year), MONTHS[month_name], int(day)
                )
                end = start

            if end < start:
                continue

            return start.isoformat(), end.isoformat()

        except (ValueError, KeyError):
            continue

    return None, None


def extract_period(
    title: str,
    lead_text: str,
) -> tuple[str | None, str | None]:
    # Judul diutamakan agar tanggal artikel terkait di bagian bawah
    # tidak dianggap sebagai periode promo artikel utama.
    start, end = parse_period_from_text(title)

    if start and end:
        return start, end

    return parse_period_from_text(lead_text)


def promo_status(
    valid_from: str | None,
    valid_until: str | None,
) -> str:
    if not valid_from or not valid_until:
        return "unknown"

    try:
        start = date.fromisoformat(valid_from)
        end = date.fromisoformat(valid_until)
    except (ValueError, TypeError):
        return "unknown"

    if end < start:
        return "unknown"

    if TODAY < start:
        return "upcoming"

    if TODAY > end:
        return "expired"

    return "active"


# =========================================================
# PENGECEKAN SUMBER RESMI
# =========================================================

def check_official_sources(
    session: requests.Session,
) -> list[dict]:
    results = []

    for index, (name, url) in enumerate(
        OFFICIAL_SOURCES,
        start=1,
    ):
        say(
            f"[RESMI {index}/{len(OFFICIAL_SOURCES)}] "
            f"Memeriksa {name}..."
        )

        record = {
            "name": name,
            "url": url,
            "status": "unknown",
            "http_status": None,
            "final_url": None,
            "note": None,
        }

        try:
            response = session.get(
                url,
                timeout=15,
                allow_redirects=True,
            )

            record["http_status"] = response.status_code
            record["final_url"] = response.url

            body = response.text[:8000].casefold()

            if response.status_code in (401, 403, 429):
                record["status"] = "blocked_or_limited"
                record["note"] = (
                    "Tidak dicoba melewati proteksi."
                )

            elif any(
                marker in body
                for marker in (
                    "captcha",
                    "checking your browser",
                    "verify you are human",
                )
            ):
                record["status"] = "challenge_page"

            elif response.ok:
                record["status"] = "reachable"
                record["note"] = (
                    "Halaman merespons; belum tentu berisi "
                    "rincian katalog promo yang dapat diekstrak."
                )

            else:
                record["status"] = "http_error"

        except requests.RequestException as exc:
            record["status"] = "request_error"
            record["note"] = f"{type(exc).__name__}: {exc}"

        results.append(record)
        time.sleep(DELAY)

    return results


# =========================================================
# MENEMUKAN ARTIKEL PROMO
# =========================================================

def read_manual_urls() -> list[str]:
    if not MANUAL_URLS.exists():
        return []

    urls = []

    for line in MANUAL_URLS.read_text(
        encoding="utf-8"
    ).splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if is_article_url(line):
            urls.append(clean_article_url(line))

    return list(OrderedDict.fromkeys(urls))


def get_anchor_title(anchor) -> str:
    title = anchor.get_text(" ", strip=True)

    if title and len(title) > 4:
        return title

    # Fallback jika teks anchor hanya ikon atau kosong.
    for parent in anchor.parents:
        if parent.name in {"h1", "h2", "h3", "h4"}:
            return parent.get_text(" ", strip=True)

        heading = parent.find(["h1", "h2", "h3", "h4"])

        if heading:
            text = heading.get_text(" ", strip=True)

            if len(text) > 4:
                return text

        if parent.name in {"article", "body"}:
            break

    return (
        anchor.get("title")
        or anchor.get("aria-label")
        or ""
    ).strip()


def discover_articles(
    session: requests.Session,
) -> tuple[list[dict], list[dict]]:
    candidates = []
    listing_log = []
    seen_urls = set()

    for page_number in range(1, MAX_LISTING_PAGES + 1):
        listing_url = HEMAT_CATEGORY_URL

        if page_number > 1:
            listing_url += f"?page={page_number}"

        say(
            f"[DAFTAR {page_number}/{MAX_LISTING_PAGES}] "
            f"Membaca kategori Hemat..."
        )
        say(f"  URL: {listing_url}")

        record = {
            "page": page_number,
            "url": listing_url,
            "status": "unknown",
            "links_seen": 0,
            "relevant_articles": 0,
            "error": None,
        }

        try:
            response = session.get(
                listing_url,
                timeout=15,
                allow_redirects=True,
            )

            if response.status_code in (401, 403, 429):
                record["status"] = "blocked_or_limited"
                record["error"] = (
                    f"HTTP {response.status_code}; "
                    "tidak dicoba melewati proteksi."
                )
                listing_log.append(record)
                say(f"  ! {record['error']}")
                break

            response.raise_for_status()

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            links = soup.select('a[href*="/news/"]')
            record["links_seen"] = len(links)
            record["status"] = "ok"

            for anchor in links:
                raw_href = anchor.get("href", "")
                absolute_url = urljoin(
                    response.url,
                    raw_href,
                )

                article_url = clean_article_url(absolute_url)

                if not article_url:
                    continue

                title = get_anchor_title(anchor)

                if not is_relevant_title(title):
                    continue

                if article_url in seen_urls:
                    continue

                seen_urls.add(article_url)

                candidates.append({
                    "title_from_listing": title,
                    "url": article_url,
                    "discovery_source": (
                        "Momsmoney Hemat listing"
                    ),
                    "query_term": f"halaman_{page_number}",
                })

                record["relevant_articles"] += 1

            listing_log.append(record)

            say(
                f"  -> {record['links_seen']} tautan; "
                f"{record['relevant_articles']} artikel relevan"
            )

        except Exception as exc:
            record["status"] = "error"
            record["error"] = (
                f"{type(exc).__name__}: {exc}"
            )

            listing_log.append(record)
            say(f"  ! Gagal membaca daftar: {exc}")

        time.sleep(DELAY)

    def sort_key(candidate):
        title = candidate.get("title_from_listing", "")
        start_text, end_text = parse_period_from_text(title)

        status = promo_status(start_text, end_text)

        status_rank = {
            "upcoming": 3,
            "active": 2,
            "expired": 1,
            "unknown": 0,
        }.get(status, 0)

        end_date = (
            date.fromisoformat(end_text)
            if end_text
            else date.min
        )

        return (
            status_rank,
            end_date,
            normalize_text(title),
        )

    candidates.sort(
        key=sort_key,
        reverse=True,
    )

    return candidates, listing_log


# =========================================================
# EKSTRAKSI TEKS BARIS PRODUK
# =========================================================

def get_leaf_text_lines(node) -> list[str]:
    """
    Pertahankan pemisah <br> agar beberapa penawaran dalam satu
    elemen HTML tidak otomatis digabung menjadi satu produk.
    """
    fragment = BeautifulSoup(
        str(node),
        "html.parser",
    )

    for br in fragment.find_all("br"):
        br.replace_with("\n")

    text = fragment.get_text(" ", strip=True)

    lines = []

    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            lines.append(line)

    return lines


def split_product_offer(
    line: str,
) -> tuple[str | None, str | None]:
    """
    Nama produk harus dipisahkan dari teks penawaran.
    Baris editorial tanpa pemisah tidak dianggap SKU.
    """
    text = re.sub(r"\s+", " ", line).strip()

    delimiter_pattern = re.compile(r":|[–—]")

    offer_start = re.compile(
        r"^(?:"
        r"Rp\.?\s*\d|"
        r"harga\b|"
        r"promo\b|"
        r"diskon\b|"
        r"beli\s+\d|"
        r"gratis\b|"
        r"tebus\s+murah\b"
        r")",
        re.IGNORECASE,
    )

    for match in delimiter_pattern.finditer(text):
        name_candidate = text[:match.start()].strip()
        offer_candidate = text[match.end():].strip()

        if not name_candidate or not offer_candidate:
            continue

        if len(name_candidate) > 140:
            continue

        if len(name_candidate.split()) > 20:
            continue

        if offer_start.match(offer_candidate):
            return name_candidate, offer_candidate

    return None, None


def parse_product_line(
    raw_line: str,
    section: str | None,
    source_url: str,
) -> dict | None:
    line = re.sub(r"\s+", " ", raw_line).strip()
    line = re.sub(r"^[\-\*\u2022]+\s*", "", line)

    if len(line) < 8 or len(line) > 280:
        return None

    # Jangan menganggap artikel merchant lain atau ketentuan pembayaran
    # sebagai produk promo Alfamart.
    if NOISE_PREFIX.search(line):
        return None

    product_name, offer_text = split_product_offer(line)

    if not product_name or not offer_text:
        return None

    if not OFFER_PATTERN.search(offer_text):
        return None

    # Hindari syarat pembayaran dan teks minimum transaksi.
    payment_prefix = re.compile(
        r"^(?:"
        r"khusus pembayaran\b|"
        r"pembayaran\b|"
        r"minimal belanja\b|"
        r"berlaku untuk\b|"
        r"maksimal\b|"
        r"tidak berlaku\b|"
        r"kuota\b|"
        r"diskon aplikasi\b"
        r")",
        re.IGNORECASE,
    )

    if payment_prefix.match(product_name):
        return None

    prices = rupiah_numbers(offer_text)

    normal_price = labeled_price(
        offer_text,
        r"harga\s+normal|normal",
    )

    promo_price = labeled_price(
        offer_text,
        r"harga\s+promo|harga\s+spesial|"
        r"harga\s+setelah\s+diskon|promo",
    )

    quality_flags = []
    price_method = "not_extracted"
    price_basis = "no_single_price"

    # Misalnya "harga normal Rp 2.000/potongan".
    # Nilai itu bukan harga normal satu botol AQUA.
    normal_unit_context = re.search(
        r"(?:harga\s+normal|normal)\s*[:\-]?\s*"
        r"Rp\.?\s*[0-9][0-9.,]*"
        r"([^,;)]{0,40})",
        offer_text,
        flags=re.IGNORECASE,
    )

    if (
        normal_price is not None
        and normal_unit_context
        and re.search(
            r"/\s*(?:potongan|pcs|pc|item|2pcs|per)"
            r"|potongan",
            normal_unit_context.group(1),
            flags=re.IGNORECASE,
        )
    ):
        normal_price = None
        quality_flags.append("normal_price_ambiguous_unit")

    purchase_match = re.match(
        r"^(beli\s+\d+\s+gratis\s+\d+)",
        offer_text,
        flags=re.IGNORECASE,
    )

    bundle_match = re.match(
        r"^beli\s+\d+\b",
        offer_text,
        flags=re.IGNORECASE,
    )

    if promo_price is not None:
        price_method = "explicit_promo_label"
        price_basis = (
            "bundle_offer_amount"
            if bundle_match
            else "offer_price"
        )

    elif prices and bundle_match:
        # Beli 2 Rp25.000 adalah nominal penawaran paket,
        # bukan otomatis harga satu unit.
        promo_price = prices[0]
        price_method = "explicit_bundle_offer_amount"
        price_basis = "bundle_offer_amount"

    elif prices and re.match(
        r"^Rp\.?\s*\d",
        offer_text,
        flags=re.IGNORECASE,
    ):
        promo_price = prices[0]
        price_method = "offer_amount_without_promo_label"
        price_basis = "offer_price"

    elif prices and normal_price is not None:
        alternatives = [
            value for value in prices
            if value != normal_price
        ]

        if alternatives:
            promo_price = alternatives[0]
            price_method = "inferred_from_multiple_prices"
            price_basis = "offer_price"
            quality_flags.append("price_inferred")

    elif prices:
        promo_price = prices[0]
        price_method = "first_offer_amount_needs_verification"
        price_basis = "unverified_offer_amount"
        quality_flags.append("price_inferred")

    elif purchase_match:
        price_method = "no_single_price_bundle_offer"
        price_basis = "non_numeric_bundle_offer"

    elif re.search(
        r"\bbeli\s+\d+\s+gratis\s+\d+",
        offer_text,
        flags=re.IGNORECASE,
    ):
        price_method = "no_single_price_bundle_offer"
        price_basis = "non_numeric_bundle_offer"

    # Jangan diam-diam mengubah harga bila nominal tampak janggal.
    if (
        promo_price is not None
        and normal_price is not None
        and promo_price > normal_price
    ):
        quality_flags.append("promo_above_normal_review")

    deal_match = re.search(
        r"\bbeli\s*\d+\s*gratis\s*\d+|"
        r"\bbeli\s*\d+\s*hemat|"
        r"\btebus\s+murah\b",
        offer_text,
        flags=re.IGNORECASE,
    )

    return {
        "product_text": product_name,
        "promo_price": promo_price,
        "normal_price": normal_price,
        "price_parse_method": price_method,
        "price_basis": price_basis,
        "purchase_condition": (
            purchase_match.group(1)
            if purchase_match
            else (
                bundle_match.group(0)
                if bundle_match
                else None
            )
        ),
        "price_quality_flag": (
            " | ".join(quality_flags)
            if quality_flags
            else None
        ),
        "offer_text": offer_text,
        "deal_type": (
            deal_match.group(0)
            if deal_match
            else None
        ),
        "section": section,
        "source_url": source_url,
        "raw_line": line,
    }


def article_container(soup: BeautifulSoup):
    """
    Prioritaskan elemen konten artikel daripada keseluruhan halaman.
    Hapus sidebar dan rekomendasi yang berisiko ikut terbaca.
    """
    selectors = [
        ".detail__body",
        ".detail__body-text",
        ".detail-body",
        ".detail-content",
        "[class*='article-content']",
        "article",
        "main",
    ]

    container = None

    for selector in selectors:
        candidate = soup.select_one(selector)

        if candidate:
            if len(candidate.get_text(" ", strip=True)) > 200:
                container = candidate
                break

    if container is None:
        container = soup.body or soup

    # Hapus komponen halaman yang bukan badan artikel.
    for unwanted in container.select(
        "aside, "
        "[class*='related-article'], "
        "[class*='related-news'], "
        "[class*='recommendation'], "
        "[class*='most-read'], "
        "[class*='popular-news'], "
        "[class*='social-share'], "
        "[class*='breadcrumb']"
    ):
        unwanted.decompose()

    return container


def extract_product_rows(
    container,
    source_url: str,
) -> list[dict]:
    rows = []
    seen = set()
    section = None

    nodes = container.find_all(
        ["h2", "h3", "h4", "li", "p"]
    )

    for node in nodes:
        if node.name in {"h2", "h3", "h4"}:
            heading = node.get_text(" ", strip=True)

            if heading:
                section = heading[:150]

            continue

        # Jika elemen ini membungkus elemen produk lain,
        # proses anaknya saja agar tidak menggandakan teks.
        if node.find(["li", "p"]):
            continue

        for line in get_leaf_text_lines(node):
            parsed = parse_product_line(
                line,
                section,
                source_url,
            )

            if not parsed:
                continue

            key = (
                normalize_text(parsed["product_text"]),
                normalize_text(parsed["offer_text"]),
                parsed.get("promo_price"),
            )

            if key in seen:
                continue

            seen.add(key)
            rows.append(parsed)

    return rows


# =========================================================
# SCRAPE SATU ARTIKEL
# =========================================================

def fetch_article(
    session: requests.Session,
    candidate: dict,
) -> dict:
    url = candidate["url"]

    result = {
        "requested_url": url,
        "source_url": None,
        "discovery_source": candidate.get("discovery_source"),
        "query_term": candidate.get("query_term"),
        "title": None,
        "source_type": "secondary",
        "source_name": "Momsmoney / Kontan",
        "campaign_type": "unknown",
        "published_at": None,
        "valid_from": None,
        "valid_until": None,
        "promo_status": "unknown",
        "status": "unknown",
        "product_count": 0,
        "products": [],
        "note": None,
    }

    try:
        response = session.get(
            url,
            timeout=15,
            allow_redirects=True,
        )

        if response.status_code in (401, 403, 429):
            result["status"] = "blocked_or_limited"
            result["note"] = (
                "Akses ditolak atau dibatasi; "
                "tidak dicoba melewati proteksi."
            )
            return result

        response.raise_for_status()

        final_url = clean_article_url(response.url)

        if not final_url:
            result["status"] = "invalid_source_url"
            return result

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        h1 = soup.find("h1")

        title = (
            h1.get_text(" ", strip=True)
            if h1
            else ""
        )

        result["title"] = title
        result["source_url"] = final_url

        # Filter ulang menggunakan judul halaman yang sebenarnya.
        if not is_relevant_title(title):
            result["status"] = "irrelevant_article"
            result["note"] = (
                "Judul artikel sebenarnya tidak sesuai "
                "dengan kriteria promo Alfamart/Alfagift."
            )
            return result

        result["campaign_type"] = campaign_type(title)

        published_meta = (
            soup.find(
                "meta",
                attrs={"property": "article:published_time"},
            )
            or soup.find(
                "meta",
                attrs={"name": "pubdate"},
            )
        )

        if published_meta:
            result["published_at"] = (
                published_meta.get("content")
                or published_meta.get("datetime")
            )

        if not result["published_at"]:
            time_node = soup.find("time")

            if time_node:
                result["published_at"] = (
                    time_node.get("datetime")
                    or time_node.get_text(" ", strip=True)
                )

        container = article_container(soup)

        # Hanya bagian awal artikel yang digunakan sebagai fallback
        # tanggal; jangan mencari tanggal di rekomendasi bawah halaman.
        lead_paragraphs = [
            p.get_text(" ", strip=True)
            for p in container.find_all("p")
            if p.get_text(" ", strip=True)
        ]

        lead_text = "\n".join(
            lead_paragraphs[:8]
        )[:3500]

        start, end = extract_period(
            title,
            lead_text,
        )

        result["valid_from"] = start
        result["valid_until"] = end
        result["promo_status"] = promo_status(
            start,
            end,
        )

        products = extract_product_rows(
            container,
            final_url,
        )

        for product in products:
            product["campaign_title"] = title
            product["campaign_type"] = result["campaign_type"]
            product["valid_from"] = start
            product["valid_until"] = end
            product["promo_status"] = result["promo_status"]

        result["products"] = products
        result["product_count"] = len(products)
        result["status"] = "ok"

    except Exception as exc:
        result["status"] = "error"
        result["note"] = (
            f"{type(exc).__name__}: {exc}"
        )

    return result


# =========================================================
# VALIDASI DAN EKSPOR
# =========================================================

def flatten_products(articles: list[dict]) -> list[dict]:
    rows = []

    for article in articles:
        rows.extend(article.get("products", []))

    return rows


def save_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_csv(
    path: Path,
    rows: list[dict],
) -> None:
    fields = [
        "product_text",
        "campaign_title",
        "campaign_type",
        "promo_status",
        "valid_from",
        "valid_until",
        "promo_price",
        "normal_price",
        "price_parse_method",
        "price_basis",
        "purchase_condition",
        "price_quality_flag",
        "deal_type",
        "offer_text",
        "section",
        "source_url",
        "raw_line",
    ]

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


def validate_run(
    articles: list[dict],
    products: list[dict],
) -> dict:
    successful = [
        item for item in articles
        if item.get("status") == "ok"
    ]

    valid_products = [
        row for row in products
        if row.get("product_text")
        and row.get("source_url")
    ]

    reasons = []

    if len(successful) < MIN_SUCCESSFUL_ARTICLES:
        reasons.append(
            f"Artikel sukses hanya {len(successful)}; "
            f"minimal {MIN_SUCCESSFUL_ARTICLES}."
        )

    if len(valid_products) < MIN_PRODUCT_ROWS:
        reasons.append(
            f"Baris produk valid hanya {len(valid_products)}; "
            f"minimal {MIN_PRODUCT_ROWS}."
        )

    return {
        "valid": not reasons,
        "successful_articles": len(successful),
        "valid_product_rows": len(valid_products),
        "reasons": reasons,
    }


# =========================================================
# MAIN
# =========================================================

def main() -> None:
    started = time.monotonic()

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        )
    })

    try:
        official_checks = check_official_sources(session)

        # Cari artikel promo terkini dari empat halaman listing.
        candidates, listing_log = discover_articles(session)

        # URL manual bersifat opsional.
        for url in read_manual_urls():
            if all(item["url"] != url for item in candidates):
                candidates.append({
                    "title_from_listing": "",
                    "url": url,
                    "discovery_source": "manual_url",
                    "query_term": "manual",
                })

        selected = candidates[:MAX_ARTICLES]

        say(
            f"\nArtikel dipilih: {len(selected)} "
            f"(batas {MAX_ARTICLES})"
        )

        articles = []

        for index, candidate in enumerate(
            selected,
            start=1,
        ):
            say(
                f"\n[ARTIKEL {index}/{len(selected)}] "
                f"{candidate.get('query_term')}"
            )
            say(f"URL: {candidate['url']}")
            say("  -> Membaca halaman...")

            item = fetch_article(
                session,
                candidate,
            )
            articles.append(item)

            say(
                f"  -> {item['status']} | "
                f"{item.get('campaign_type')} | "
                f"produk={item.get('product_count', 0)} | "
                f"{item.get('valid_from') or '?'} s.d. "
                f"{item.get('valid_until') or '?'} | "
                f"{item.get('promo_status')}"
            )

            if item.get("note"):
                say(f"  Catatan: {item['note']}")

            time.sleep(DELAY)

        products = flatten_products(articles)

        active_count = sum(
            row.get("promo_status") == "active"
            for row in products
        )

        upcoming_count = sum(
            row.get("promo_status") == "upcoming"
            for row in products
        )

        expired_count = sum(
            row.get("promo_status") == "expired"
            for row in products
        )

        unknown_count = sum(
            row.get("promo_status") == "unknown"
            for row in products
        )

        quality_flags = sum(
            bool(row.get("price_quality_flag"))
            for row in products
        )

        payload = {
            "checked_at": timestamp(),
            "reference_date": TODAY.isoformat(),
            "module": "A - promo source collector",
            "source_type": "secondary_public_articles",
            "listing_url": HEMAT_CATEGORY_URL,
            "listing_pages_checked": len(listing_log),
            "article_count": len(articles),
            "successful_articles": sum(
                row.get("status") == "ok"
                for row in articles
            ),
            "product_row_count": len(products),
            "active_product_rows": active_count,
            "upcoming_product_rows": upcoming_count,
            "expired_product_rows": expired_count,
            "unknown_period_product_rows": unknown_count,
            "rows_with_price_quality_flags": quality_flags,
            "official_source_checks": official_checks,
            "listing_search_log": listing_log,
            "articles": articles,
            "products": products,
        }

        validation = validate_run(
            articles,
            products,
        )

        payload["validation"] = validation

        # Simpan percobaan teranyar walaupun validasi gagal.
        save_json(ATTEMPT_JSON, payload)

        if validation["valid"]:
            HISTORY_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            history_file = (
                HISTORY_DIR
                / (
                    datetime.now(ZoneInfo("Asia/Jakarta"))
                    .strftime("%Y%m%d_%H%M%S")
                    + ".json"
                )
            )

            save_json(history_file, payload)
            save_json(OUT_JSON, payload)
            write_csv(OUT_CSV, products)

            output_status = "updated"

        else:
            output_status = "preserved_previous_catalog"

        run_log = {
            "checked_at": payload["checked_at"],
            "output_status": output_status,
            "validation": validation,
            "listing_search_log": listing_log,
            "article_count": len(articles),
            "successful_articles": payload["successful_articles"],
            "product_row_count": len(products),
            "rows_with_price_quality_flags": quality_flags,
            "articles": [
                {
                    "title": item.get("title"),
                    "source_url": item.get("source_url"),
                    "status": item.get("status"),
                    "promo_status": item.get("promo_status"),
                    "valid_from": item.get("valid_from"),
                    "valid_until": item.get("valid_until"),
                    "product_count": item.get("product_count"),
                    "note": item.get("note"),
                }
                for item in articles
            ],
        }

        save_json(RUN_LOG, run_log)

        elapsed = time.monotonic() - started

        say("\n" + "=" * 58)
        say("SELESAI - MODUL A")
        say("=" * 58)
        say(f"Artikel ditemukan       : {len(selected)}")
        say(f"Artikel berhasil        : {payload['successful_articles']}")
        say(f"Baris produk            : {len(products)}")
        say(f"Baris promo aktif       : {active_count}")
        say(f"Baris promo mendatang   : {upcoming_count}")
        say(f"Baris promo kedaluwarsa : {expired_count}")
        say(f"Periode unknown         : {unknown_count}")
        say(f"Baris dengan flag harga : {quality_flags}")
        say(f"Validasi run            : {validation['valid']}")
        say(f"Status katalog          : {output_status}")
        say(f"Waktu                   : {elapsed:.1f} detik")
        say(f"Katalog terakhir        : {OUT_JSON}")
        say(f"Percobaan terbaru       : {ATTEMPT_JSON}")
        say(f"Run log                 : {RUN_LOG}")
        say(f"CSV                     : {OUT_CSV}")

        if validation["reasons"]:
            say("\nKatalog tidak diperbarui:")

            for reason in validation["reasons"]:
                say(f"- {reason}")

        say("=" * 58)

    finally:
        session.close()


if __name__ == "__main__":
    main()