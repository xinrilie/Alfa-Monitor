
from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# =========================================================
# KONFIGURASI
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

CLEAN_JSON = BASE_DIR / "promo_catalog_module_a_clean.json"
RAW_JSON = BASE_DIR / "promo_catalog_module_a.json"

OUTPUT_JSON = BASE_DIR / "promo_official_verification.json"
OUTPUT_CSV = BASE_DIR / "promo_official_verification.csv"

OFFICIAL_DOMAIN = "blog.alfagift.id"
PROMO_CATEGORY = "https://blog.alfagift.id/promo/"

MAX_LISTING_PAGES = 3
MAX_OFFICIAL_ARTICLES = 24
REQUEST_TIMEOUT = 10
REQUEST_DELAY = 0.2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.7",
}

MONTHS = {
    1: "januari",
    2: "februari",
    3: "maret",
    4: "april",
    5: "mei",
    6: "juni",
    7: "juli",
    8: "agustus",
    9: "september",
    10: "oktober",
    11: "november",
    12: "desember",
}

STOPWORDS = {
    "dan", "atau", "dengan", "untuk", "di", "ke", "dari",
    "yang", "rp", "harga", "promo", "spesial", "hemat",
    "diskon", "normal", "hingga", "sampai", "periode",
    "hanya", "cuma", "sebesar", "seharga", "pilihan",
}

SIZE_TOKEN = re.compile(
    r"^\d+(?:\.\d+)?"
    r"(?:ml|cl|liter|litre|kg|mg|gr|g|pcs|pc|tablet|kapsul|cm)$",
    re.IGNORECASE,
)

RUPIAH_PATTERN = re.compile(
    r"\brp\.?\s*([0-9][0-9.,]*)",
    re.IGNORECASE,
)

session = requests.Session()
session.headers.update(HEADERS)


# =========================================================
# UTILITAS
# =========================================================

def log(message: str) -> None:
    print(
        f"[{datetime.now().astimezone():%H:%M:%S}] {message}",
        flush=True,
    )


def atomic_write(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def save_json(path: Path, data: dict) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def fetch_html(url: str) -> tuple[BeautifulSoup | None, str]:
    """Mengambil halaman; kegagalan dilaporkan tanpa menghentikan audit."""
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            return None, f"http_{response.status_code}"

        return BeautifulSoup(response.text, "html.parser"), "ok"

    except requests.RequestException as exc:
        return None, f"request_error: {type(exc).__name__}"


def canonical_url(url: str) -> str:
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        return ""

    if parsed.netloc.casefold() != OFFICIAL_DOMAIN:
        return ""

    return f"https://{OFFICIAL_DOMAIN}{parsed.path.rstrip('/')}/"


def is_article_url(url: str) -> bool:
    canonical = canonical_url(url)

    if not canonical:
        return False

    path = urlparse(canonical).path.strip("/").casefold()

    if not path:
        return False

    blocked_parts = (
        "category/",
        "tag/",
        "author/",
        "page/",
        "wp-content/",
        "wp-json/",
        "feed/",
    )

    if any(part in path for part in blocked_parts):
        return False

    if path in {"promo", "about", "howtobuy"}:
        return False

    return True


# =========================================================
# PENCARIAN ARTIKEL RESMI
# =========================================================

def collect_article_links(
    soup: BeautifulSoup,
    base_url: str,
) -> list[tuple[str, str]]:
    found = []
    seen = set()

    # Judul artikel biasanya berada di heading. Hindari mengambil
    # seluruh anchor halaman karena bisa berisi menu dan tautan terkait.
    selectors = (
        "h1 a[href], h2 a[href], h3 a[href], "
        ".entry-title a[href], .post-title a[href]"
    )

    for anchor in soup.select(selectors):
        href = anchor.get("href")

        if not href:
            continue

        url = canonical_url(urljoin(base_url, href))

        if not url or not is_article_url(url) or url in seen:
            continue

        title = anchor.get_text(" ", strip=True)

        seen.add(url)
        found.append((url, title))

    return found


def get_content_container(
    soup: BeautifulSoup,
) -> BeautifulSoup:
    selectors = (
        ".entry-content",
        ".post-content",
        ".post-body",
        ".single-content",
        "article",
        "main",
    )

    candidates = []

    for selector in selectors:
        candidates.extend(soup.select(selector))

        if candidates:
            # Utamakan area artikel bila tersedia.
            if selector in {
                ".entry-content",
                ".post-content",
                ".post-body",
                ".single-content",
            }:
                break

    if not candidates:
        return soup

    return max(
        candidates,
        key=lambda element: len(element.get_text(" ", strip=True)),
    )


def get_official_article(url: str) -> tuple[dict | None, str]:
    soup, status = fetch_html(url)

    if soup is None:
        return None, status

    container = get_content_container(soup)

    title_element = soup.select_one("h1")
    title = (
        title_element.get_text(" ", strip=True)
        if title_element
        else (soup.title.get_text(" ", strip=True) if soup.title else url)
    )

    body_text = container.get_text(" ", strip=True)

    snippets = []
    seen_snippets = set()

    for node in container.select("tr, li, p, h2, h3, h4"):
        text = re.sub(
            r"\s+",
            " ",
            node.get_text(" ", strip=True),
        ).strip()

        if not text or len(text) < 8 or len(text) > 2000:
            continue

        if text not in seen_snippets:
            seen_snippets.add(text)
            snippets.append(text)

    # Fallback jika artikel tidak menggunakan elemen HTML umum.
    if not snippets:
        snippets = [
            part.strip()
            for part in re.split(r"[\r\n]+", body_text)
            if len(part.strip()) >= 8
        ]

    return {
        "url": url,
        "title": title,
        "body": body_text,
        "snippets": snippets,
    }, "ok"


def crawl_official_blog() -> tuple[list[dict], list[dict]]:
    log("Mencari artikel di blog promo resmi Alfagift...")

    listing_logs = []
    article_urls = []
    seen_urls = set()

    for page_number in range(1, MAX_LISTING_PAGES + 1):
        if page_number == 1:
            listing_url = PROMO_CATEGORY
        else:
            listing_url = urljoin(
                PROMO_CATEGORY,
                f"page/{page_number}/",
            )

        log(f"Memeriksa daftar resmi: halaman {page_number}")

        soup, status = fetch_html(listing_url)

        listing_logs.append({
            "url": listing_url,
            "status": status,
        })

        if soup is None:
            log(f"  Gagal membaca daftar: {status}")
            continue

        links = collect_article_links(soup, listing_url)

        for url, title in links:
            if url in seen_urls:
                continue

            seen_urls.add(url)
            article_urls.append({
                "url": url,
                "listing_title": title,
            })

        log(f"  Tautan artikel ditemukan: {len(links)}")
        time.sleep(REQUEST_DELAY)

    article_urls = article_urls[:MAX_OFFICIAL_ARTICLES]
    official_articles = []
    article_logs = []

    for index, item in enumerate(article_urls, start=1):
        log(
            f"Membaca artikel resmi {index}/{len(article_urls)}: "
            f"{item['url']}"
        )

        article, status = get_official_article(item["url"])

        article_logs.append({
            "url": item["url"],
            "status": status,
        })

        if article:
            official_articles.append(article)

        time.sleep(REQUEST_DELAY)

    logs = {
        "listing_pages": listing_logs,
        "article_pages": article_logs,
    }

    return official_articles, logs


# =========================================================
# NORMALISASI NAMA PRODUK
# =========================================================

def normalize_name(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()

    # Satukan variasi penulisan merek yang umum.
    text = re.sub(r"\bsilver\s*queen\b", "silverqueen", text)
    text = re.sub(r"\bwall\s*['’]?\s*s\b", "walls", text)
    text = re.sub(r"\bso\s*klin\b", "soklin", text)
    text = re.sub(r"\bultra\s+milk\b", "ultramilk", text)
    text = re.sub(r"\btop\s+coffee\b", "topcoffee", text)

    # Samakan ukuran yang memiliki atau tidak memiliki spasi.
    def normalize_size(match):
        number = match.group(1).replace(",", ".")
        unit = match.group(2).casefold()

        # 1.2 kg dan 1,2 kg akan mempunyai representasi yang sama.
        number = number.rstrip("0").rstrip(".") if "." in number else number

        return number + unit

    text = re.sub(
        r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*"
        r"(kilogram|liter|litre|tablet|kapsul|ml|cl|kg|mg|gr|g|pcs|pc|cm|l)"
        r"\b",
        normalize_size,
        text,
    )

    # Pertahankan nama variasi seperti 3 in 1 sebagai satu token.
    text = re.sub(r"\b(\d+)\s+in\s+(\d+)\b", r"\1in\2", text)

    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_tokens(text: str) -> set[str]:
    normalized = normalize_name(text)

    tokens = {
        token
        for token in normalized.split()
        if token not in STOPWORDS and not token.isdigit()
    }

    return tokens


def product_name_score(product_name: str, line: str) -> float:
    product_tokens = name_tokens(product_name)
    line_tokens = name_tokens(line)

    if not product_tokens or not line_tokens:
        return 0.0

    # Jika produk memiliki ukuran eksplisit, ukuran tersebut harus ada
    # pada baris resmi yang sama agar varian berbeda tidak tertukar.
    size_tokens = {
        token
        for token in product_tokens
        if SIZE_TOKEN.fullmatch(token)
    }

    if size_tokens and not size_tokens.issubset(line_tokens):
        return 0.0

    overlap = product_tokens & line_tokens
    coverage = len(overlap) / len(product_tokens)

    # Untuk nama sangat pendek, wajib cocok seluruhnya.
    if len(product_tokens) <= 2 and coverage < 1.0:
        return 0.0

    # Similarity karakter digunakan hanya sebagai sinyal tambahan.
    # Skor tinggi tidak dengan sendirinya membuktikan harga.
    product_string = " ".join(sorted(product_tokens))
    line_string = " ".join(sorted(line_tokens))
    char_score = SequenceMatcher(
        None,
        product_string,
        line_string,
    ).ratio()

    return max(coverage, min(coverage + 0.03, char_score))


# =========================================================
# HARGA DAN PERIODE
# =========================================================

def extract_rupiah_amounts(text: str) -> list[int]:
    amounts = []

    for match in RUPIAH_PATTERN.finditer(text or ""):
        raw = match.group(1)
        digits = re.sub(r"[.,]", "", raw)

        try:
            amounts.append(int(digits))
        except ValueError:
            continue

    return amounts


def official_promo_prices(line: str) -> set[int]:
    """
    Ambil angka harga resmi dari satu baris.

    Jika ada harga normal/coret dan harga promo, gunakan harga terakhir
    sebelum catatan diskon. Ini mencegah harga normal dianggap harga promo.
    """
    text = line or ""
    lower = text.casefold()
    amounts = extract_rupiah_amounts(text)

    if not amounts:
        return set()

    has_old_and_new_price = any(
        marker in lower
        for marker in (
            "harga normal",
            "harga coret",
            "harga promo",
            "menjadi",
            "➡",
            "→",
            "setelah diskon",
        )
    )

    if has_old_and_new_price:
        # Hanya bagian penawaran utama; abaikan keterangan hemat
        # yang biasanya berada di dalam tanda kurung.
        primary_text = text.split("(", 1)[0]
        primary_amounts = extract_rupiah_amounts(primary_text)

        if primary_amounts:
            return {primary_amounts[-1]}

    # Untuk harga berbentuk rentang, dua ujung rentang dapat berlaku
    # bagi beberapa varian produk dalam satu baris.
    return set(amounts)


def parse_iso_date(value) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def date_pattern(day_number: int) -> str:
    return rf"0?{day_number}"


def period_matches(
    article_text: str,
    valid_from,
    valid_until,
) -> bool:
    start = parse_iso_date(valid_from)
    end = parse_iso_date(valid_until)

    if not start or not end:
        return False

    text = unicodedata.normalize("NFKC", article_text or "").casefold()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)

    start_day = date_pattern(start.day)
    end_day = date_pattern(end.day)

    start_month = MONTHS[start.month]
    end_month = MONTHS[end.month]

    separator = r"(?:-|sampai|hingga|s\.?\s*d\.?)"

    if start == end:
        pattern = (
            rf"\b{start_day}\s+{start_month}"
            rf"(?:\s+{start.year})?\b"
        )
        return re.search(pattern, text) is not None

    if start.year == end.year and start.month == end.month:
        pattern = (
            rf"\b{start_day}\s*{separator}\s*{end_day}"
            rf"\s+{start_month}(?:\s+{start.year})?\b"
        )

        if re.search(pattern, text):
            return True

        # Sebagian artikel menulis bulan di kedua sisi rentang.
        pattern_both_months = (
            rf"\b{start_day}\s+{start_month}\s*{separator}\s*"
            rf"{end_day}\s+{end_month}"
            rf"(?:\s+{start.year})?\b"
        )

        return re.search(pattern_both_months, text) is not None

    # Rentang lintas bulan: kedua tanggal harus tampak dalam teks
    # dengan nama bulan yang benar.
    start_pattern = (
        rf"\b{start_day}\s+{start_month}"
        rf"(?:\s+{start.year})?\b"
    )
    end_pattern = (
        rf"\b{end_day}\s+{end_month}"
        rf"(?:\s+{end.year})?\b"
    )

    return (
        re.search(start_pattern, text) is not None
        and re.search(end_pattern, text) is not None
    )


def offer_condition_matches(product: dict, line: str) -> bool:
    """Cocokkan syarat promo teks seperti Beli 2 Gratis 1."""
    candidate = " ".join([
        str(product.get("purchase_condition") or ""),
        str(product.get("offer_text") or ""),
        str(product.get("deal_type") or ""),
    ]).casefold()

    official = (line or "").casefold()

    buy_numbers = re.findall(r"\bbeli\s*(\d+)\b", candidate)
    free_numbers = re.findall(r"\bgratis\s*(\d+)\b", candidate)

    if buy_numbers or free_numbers:
        for number in buy_numbers:
            if not re.search(rf"\bbeli\s*{number}\b", official):
                return False

        for number in free_numbers:
            if not re.search(rf"\bgratis\s*{number}\b", official):
                return False

        return True

    return False


# =========================================================
# VERIFIKASI PER PRODUK
# =========================================================

STATUS_PRIORITY = {
    "official_blog_price_period_match": 5,
    "official_blog_offer_period_match": 4,
    "official_blog_price_match_period_unconfirmed": 3,
    "official_blog_product_period_match_price_mismatch": 2,
    "official_blog_product_found_needs_review": 1,
}


def verify_one_product(
    product: dict,
    official_articles: list[dict],
) -> dict:
    name = str(product.get("product_text") or "").strip()
    promo_price = product.get("promo_price")
    valid_from = product.get("valid_from")
    valid_until = product.get("valid_until")

    best = None
    best_priority = -1

    for article in official_articles:
        body = article["body"]
        period_ok = period_matches(body, valid_from, valid_until)

        for line in article["snippets"]:
            score = product_name_score(name, line)

            # Ambang konservatif untuk mengurangi salah-cocok.
            if score < 0.85:
                continue

            amounts = official_promo_prices(line)

            price_ok = (
                isinstance(promo_price, (int, float))
                and not isinstance(promo_price, bool)
                and int(promo_price) in amounts
            )

            offer_ok = offer_condition_matches(product, line)

            if price_ok and period_ok:
                status = "official_blog_price_period_match"
            elif (
                promo_price is None
                and offer_ok
                and period_ok
            ):
                status = "official_blog_offer_period_match"
            elif price_ok:
                status = "official_blog_price_match_period_unconfirmed"
            elif period_ok:
                status = "official_blog_product_period_match_price_mismatch"
            else:
                status = "official_blog_product_found_needs_review"

            priority = STATUS_PRIORITY[status]

            candidate = {
                "verification_status": status,
                "official_source_name": "Blog resmi Alfagift",
                "official_source_url": article["url"],
                "official_article_title": article["title"],
                "official_match_text": line[:1000],
                "official_name_score": round(score, 3),
                "official_period_match": period_ok,
                "official_promo_price_match": price_ok,
                "official_offer_condition_match": offer_ok,
                "official_prices_found": sorted(amounts),
            }

            if priority > best_priority:
                best = candidate
                best_priority = priority

            # Kecocokan harga dan periode sudah ditemukan. Tidak perlu
            # mencari skor lebih tinggi untuk baris yang sama.
            if status == "official_blog_price_period_match":
                break

        if best and best["verification_status"] == (
            "official_blog_price_period_match"
        ):
            break

    if best:
        result = dict(product)
        result.update(best)
        result["verification_checked_at"] = (
            datetime.now().astimezone().isoformat(timespec="seconds")
        )
        return result

    result = dict(product)

    if not official_articles:
        status = "official_source_unavailable"
    else:
        status = "secondary_only"

    result.update({
        "verification_status": status,
        "official_source_name": "Blog resmi Alfagift",
        "official_source_url": "",
        "official_article_title": "",
        "official_match_text": "",
        "official_name_score": 0,
        "official_period_match": False,
        "official_promo_price_match": False,
        "official_offer_condition_match": False,
        "official_prices_found": [],
        "verification_checked_at": (
            datetime.now().astimezone().isoformat(timespec="seconds")
        ),
    })

    return result


# =========================================================
# OUTPUT LAPORAN
# =========================================================

CSV_FIELDS = [
    "product_text",
    "promo_price",
    "normal_price",
    "price_basis",
    "purchase_condition",
    "offer_text",
    "campaign_type",
    "campaign_title",
    "valid_from",
    "valid_until",
    "promo_status",
    "source_url",
    "verification_status",
    "official_source_name",
    "official_source_url",
    "official_article_title",
    "official_match_text",
    "official_name_score",
    "official_period_match",
    "official_promo_price_match",
    "official_offer_condition_match",
    "official_prices_found",
    "verification_checked_at",
]


def save_csv(rows: list[dict]) -> None:
    buffer = StringIO(newline="")

    writer = csv.DictWriter(
        buffer,
        fieldnames=CSV_FIELDS,
        extrasaction="ignore",
    )

    writer.writeheader()

    for row in rows:
        csv_row = {}

        for field in CSV_FIELDS:
            value = row.get(field)

            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            elif value is None:
                value = ""

            csv_row[field] = value

        writer.writerow(csv_row)

    atomic_write(OUTPUT_CSV, buffer.getvalue())


# =========================================================
# PROGRAM UTAMA
# =========================================================

def main() -> int:
    print("=" * 64)
    print("VERIFIKASI SILANG SUMBER RESMI ALFAGIFT")
    print("=" * 64)

    input_path = CLEAN_JSON if CLEAN_JSON.is_file() else RAW_JSON

    if not input_path.is_file():
        log("ERROR: Katalog input tidak ditemukan.")
        log(f"Periksa: {CLEAN_JSON.name} atau {RAW_JSON.name}")
        return 1

    log(f"Membaca kandidat produk dari: {input_path.name}")

    try:
        with input_path.open("r", encoding="utf-8") as file:
            catalog = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"ERROR membaca JSON: {exc}")
        return 1

    products = catalog.get("products", [])

    if not isinstance(products, list) or not products:
        log("ERROR: Katalog tidak memiliki daftar products yang valid.")
        return 1

    log(f"Jumlah kandidat: {len(products)}")

    official_articles, crawl_logs = crawl_official_blog()

    log(f"Artikel resmi berhasil dibaca: {len(official_articles)}")

    results = []

    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            continue

        if index % 50 == 0 or index == len(products):
            log(f"Memeriksa kandidat {index}/{len(products)}")

        results.append(
            verify_one_product(product, official_articles)
        )

    counts = {}

    for row in results:
        status = row["verification_status"]
        counts[status] = counts.get(status, 0) + 1

    report = {
        "checked_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "input_file": input_path.name,
        "official_domain": OFFICIAL_DOMAIN,
        "official_category_url": PROMO_CATEGORY,
        "scope": (
            "Mencocokkan teks produk, harga, dan periode pada artikel "
            "promo blog resmi Alfagift yang berhasil diambil. Laporan "
            "ini tidak menjamin harga real-time, stok toko, cakupan "
            "semua artikel resmi, maupun harga untuk setiap wilayah."
        ),
        "crawl": {
            "listing_pages_configured": MAX_LISTING_PAGES,
            "article_limit": MAX_OFFICIAL_ARTICLES,
            "official_articles_fetched": len(official_articles),
            "logs": crawl_logs,
        },
        "summary": {
            "candidate_count": len(results),
            "status_counts": counts,
        },
        "results": results,
    }

    save_json(OUTPUT_JSON, report)
    save_csv(results)

    print()
    print("=" * 64)
    print("RINGKASAN VERIFIKASI")
    print("=" * 64)
    print(f"Total kandidat: {len(results)}")
    print(f"Artikel resmi terbaca: {len(official_articles)}")

    for status in sorted(counts):
        print(f"{status:50s}: {counts[status]}")

    print()
    print("FILE LAPORAN:")
    print(f"- {OUTPUT_CSV}")
    print(f"- {OUTPUT_JSON}")

    if not official_articles:
        print()
        print(
            "PERINGATAN: tidak ada artikel resmi yang berhasil dibaca. "
            "Tidak ada kandidat yang boleh dianggap terverifikasi."
        )

    print("=" * 64)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
