
from __future__ import annotations

import copy
import csv
import json
import re
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any


# =========================================================
# KONFIGURASI
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

RAW_JSON = BASE_DIR / "promo_catalog_module_a.json"

CLEAN_JSON = BASE_DIR / "promo_catalog_module_a_clean.json"
CLEAN_CSV = BASE_DIR / "promo_catalog_products_clean.csv"
ACTIVE_CSV = BASE_DIR / "promo_active_clean.csv"
HISTORY_CSV = BASE_DIR / "promo_history_clean.csv"
REVIEW_CSV = BASE_DIR / "promo_price_review_remaining.csv"


# =========================================================
# UTILITAS
# =========================================================

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_date(value: Any) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (ValueError, TypeError):
        return None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


def has_flag(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def write_atomic(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def write_json(path: Path, payload: dict) -> None:
    write_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    preferred_columns = [
        "product_text",
        "promo_price",
        "normal_price",
        "discount_amount",
        "discount_percent",
        "discount_calculation_status",
        "price_basis",
        "bundle_quantity",
        "unit_promo_price",
        "unit_normal_price",
        "purchase_condition",
        "price_quality_flag",
        "offer_text",
        "deal_type",
        "campaign_type",
        "campaign_title",
        "valid_from",
        "valid_until",
        "promo_status",
        "source_url",
        "raw_line",
        "duplicate_count",
        "merged_source_urls",
    ]

    all_columns = set()

    for row in rows:
        all_columns.update(row.keys())

    columns = [
        column for column in preferred_columns
        if column in all_columns
    ]

    columns.extend(
        sorted(
            column
            for column in all_columns
            if column not in columns
        )
    )

    if not columns:
        columns = ["product_text"]

    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=columns,
        extrasaction="ignore",
    )
    writer.writeheader()

    for row in rows:
        safe_row = {}

        for column in columns:
            value = row.get(column)

            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            elif value is None:
                value = ""

            safe_row[column] = value

        writer.writerow(safe_row)

    write_atomic(path, buffer.getvalue())


# =========================================================
# MENENTUKAN STATUS PROMO BERDASARKAN TANGGAL
# =========================================================

def get_promo_status(
    row: dict,
    reference_date: date,
) -> str:
    """
    Tanggal akhir yang telah lewat selalu mengalahkan status lama
    yang mungkin masih tertulis 'active' pada data sumber.
    """
    start = parse_date(row.get("valid_from"))
    end = parse_date(row.get("valid_until"))

    if end is not None and end < reference_date:
        return "expired"

    if start is not None and start > reference_date:
        return "upcoming"

    if start is not None and end is not None:
        return "active"

    # Tanggal tidak cukup untuk memastikan promo masih berlaku.
    # Jangan tampilkan di katalog promo berjalan.
    return "unknown"


# =========================================================
# DETEKSI HARGA PAKET
# =========================================================

EXPLICIT_PACKAGE_PATTERN = re.compile(
    r"(?:/|\bper\s+)\s*(\d+)\s*"
    r"(?:pcs?|pieces?|buah|unit|botol|pack)\b",
    re.IGNORECASE,
)

BUY_PATTERN = re.compile(
    r"\bbeli\s*(\d+)\b",
    re.IGNORECASE,
)

RUPIAH_PATTERN = re.compile(
    r"\brp\.?\s*([0-9][0-9.,]*)",
    re.IGNORECASE,
)

REGIONAL_REBATE_PATTERN = re.compile(
    r"potongan|luar jawa|bali|medan|luar wilayah",
    re.IGNORECASE,
)


def source_price_text(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in (
            "raw_line",
            "offer_text",
            "purchase_condition",
            "product_text",
        )
    )


def detect_bundle_quantity(row: dict) -> int | None:
    """
    Prioritaskan jumlah unit yang dinyatakan eksplisit seperti /3pcs.

    'Beli 2 Gratis 1' tidak otomatis dianggap harga paket 2 atau 3
    karena jumlah produk yang dibayar dan diterima berbeda.
    """
    text = source_price_text(row)

    quantities = {
        int(match.group(1))
        for match in EXPLICIT_PACKAGE_PATTERN.finditer(text)
        if int(match.group(1)) > 0
    }

    if len(quantities) == 1:
        return next(iter(quantities))

    # Untuk penawaran beli N berharga paket, hanya inferensikan jumlah
    # paket jika sumber memiliki harga eksplisit dan bukan promosi gratis.
    promo_price = row.get("promo_price")
    basis = normalize_text(row.get("price_basis"))

    if (
        is_number(promo_price)
        and basis in {
            "bundle_offer_amount",
            "explicit_bundle_offer_amount",
        }
        and not re.search(r"\bgratis\b", text, re.IGNORECASE)
    ):
        match = BUY_PATTERN.search(text)

        if match:
            quantity = int(match.group(1))
            if quantity > 0:
                return quantity

    return None


def is_ambiguous_regional_rebate(row: dict) -> bool:
    text = source_price_text(row).casefold()

    return (
        "harga normal" in text
        and "potongan" in text
        and bool(REGIONAL_REBATE_PATTERN.search(text))
    )


# =========================================================
# KLASIFIKASI DAN PERHITUNGAN DISKON
# =========================================================

def normalize_product(
    product: dict,
    reference_date: date,
) -> dict:
    row = copy.deepcopy(product)

    quantity = detect_bundle_quantity(row)
    promo_price = row.get("promo_price")
    normal_price = row.get("normal_price")
    price_flag = row.get("price_quality_flag")

    row["bundle_quantity"] = quantity

    # -----------------------------------------------------
    # Klasifikasi harga paket
    # -----------------------------------------------------

    if quantity is not None:
        row["price_basis"] = f"bundle_price_{quantity}pcs"

        if not str(row.get("purchase_condition") or "").strip():
            row["purchase_condition"] = f"Paket {quantity} pcs"

    # -----------------------------------------------------
    # Harga ambigu atau tidak konsisten
    # -----------------------------------------------------

    ambiguous_rebate = is_ambiguous_regional_rebate(row)

    if ambiguous_rebate:
        # Angka potongan bukan otomatis harga normal barang.
        row["normal_price"] = None
        row["price_quality_flag"] = (
            "review_ambiguous_regional_rebate"
        )

    elif (
        is_number(promo_price)
        and is_number(normal_price)
        and promo_price > normal_price
    ):
        # Diskon tidak dapat dihitung jika harga promo melebihi
        # harga normal pada basis yang sama.
        if not has_flag(price_flag):
            row["price_quality_flag"] = (
                "review_promo_price_exceeds_normal_price"
            )

    # -----------------------------------------------------
    # Hitung penghematan rupiah dan persentase
    # -----------------------------------------------------

    row["discount_amount"] = None
    row["discount_percent"] = None
    row["discount_calculation_status"] = "not_calculated"

    # Harga per unit hanya dihitung jika jumlah paket diketahui
    # secara eksplisit dan kedua harga merepresentasikan paket sama.
    row["unit_promo_price"] = None
    row["unit_normal_price"] = None

    if (
        quantity is not None
        and quantity > 0
        and is_number(promo_price)
    ):
        row["unit_promo_price"] = round(
            promo_price / quantity,
            2,
        )

        if is_number(normal_price):
            row["unit_normal_price"] = round(
                normal_price / quantity,
                2,
            )

    elif (
        quantity is None
        and is_number(promo_price)
        and is_number(normal_price)
    ):
        # Tanpa keterangan paket, harga satuan ditampilkan hanya
        # sebagai nilai yang dinyatakan pada sumber.
        row["unit_promo_price"] = promo_price
        row["unit_normal_price"] = normal_price

    if has_flag(row.get("price_quality_flag")):
        row["discount_calculation_status"] = (
            "review_required_price_flag"
        )

    elif (
        not is_number(promo_price)
        or not is_number(normal_price)
    ):
        row["discount_calculation_status"] = (
            "missing_comparable_price"
        )

    elif normal_price <= 0:
        row["discount_calculation_status"] = (
            "invalid_normal_price"
        )

    elif promo_price > normal_price:
        row["discount_calculation_status"] = (
            "promo_price_exceeds_normal_price"
        )

    else:
        row["discount_amount"] = round(
            normal_price - promo_price,
            2,
        )

        row["discount_percent"] = round(
            (normal_price - promo_price)
            / normal_price
            * 100,
            2,
        )

        row["discount_calculation_status"] = (
            "calculated_same_offer_basis"
        )

    # -----------------------------------------------------
    # Status tanggal yang konsisten dengan tanggal referensi
    # -----------------------------------------------------

    row["promo_status"] = get_promo_status(
        row,
        reference_date,
    )

    return row


# =========================================================
# DEDUPLIKASI
# =========================================================

def offer_key(row: dict) -> tuple:
    return (
        normalize_text(row.get("product_text")),
        row.get("promo_price"),
        row.get("normal_price"),
        normalize_text(row.get("offer_text")),
        normalize_text(row.get("deal_type")),
        normalize_text(row.get("purchase_condition")),
        normalize_text(row.get("price_basis")),
        row.get("bundle_quantity"),
        normalize_text(row.get("campaign_type")),
        normalize_text(row.get("valid_from")),
        normalize_text(row.get("valid_until")),
        normalize_text(row.get("promo_status")),
    )


def deduplicate_products(
    rows: list[dict],
) -> tuple[list[dict], int]:
    unique: dict[tuple, dict] = {}
    duplicates = 0

    for row in rows:
        key = offer_key(row)
        source_url = row.get("source_url")

        if key not in unique:
            item = copy.deepcopy(row)
            item["duplicate_count"] = 1
            item["merged_source_urls"] = (
                [source_url] if source_url else []
            )
            unique[key] = item
            continue

        duplicates += 1
        kept = unique[key]
        kept["duplicate_count"] += 1

        if source_url:
            sources = kept.setdefault("merged_source_urls", [])

            if source_url not in sources:
                sources.append(source_url)

    return list(unique.values()), duplicates


# =========================================================
# PROSES ARTIKEL
# =========================================================

def process_articles(
    articles: Any,
    reference_date: date,
) -> tuple[list[dict], list[dict]]:
    current_articles = []
    archived_articles = []

    if not isinstance(articles, list):
        return current_articles, archived_articles

    for article in articles:
        if not isinstance(article, dict):
            continue

        item = copy.deepcopy(article)
        status = get_promo_status(item, reference_date)
        item["promo_status"] = status

        nested = item.get("products", [])

        if isinstance(nested, list):
            item["products"] = [
                normalize_product(product, reference_date)
                for product in nested
                if isinstance(product, dict)
                and get_promo_status(
                    product, reference_date
                ) in {"active", "upcoming"}
            ]

        if status in {"active", "upcoming"}:
            current_articles.append(item)
        elif status == "expired":
            archived_articles.append(item)
        # Periode tidak dikenal tidak dimasukkan ke katalog berjalan.

    return current_articles, archived_articles


# =========================================================
# OUTPUT
# =========================================================

def main() -> int:
    print("=" * 64)
    print("NORMALISASI PROMO, FILTER TANGGAL, DAN DISKON")
    print("=" * 64)

    if not RAW_JSON.is_file():
        print(f"ERROR: File katalog tidak ditemukan: {RAW_JSON}")
        return 1

    try:
        with RAW_JSON.open("r", encoding="utf-8") as file:
            source = json.load(file)

        if not isinstance(source, dict):
            raise ValueError("Katalog sumber harus berupa JSON object.")

        source_products = source.get("products", [])

        if not isinstance(source_products, list):
            raise ValueError("Key products tidak valid.")

        reference_date = (
            parse_date(source.get("reference_date"))
            or datetime.now().astimezone().date()
        )

        print(f"[1/6] Tanggal referensi: {reference_date.isoformat()}")
        print(f"      Baris sumber: {len(source_products)}")

        print("[2/6] Normalisasi harga dan jumlah paket...")

        normalized = [
            normalize_product(row, reference_date)
            for row in source_products
            if isinstance(row, dict)
        ]

        print("[3/6] Menggabungkan penawaran duplikat...")

        all_unique, duplicates = deduplicate_products(normalized)

        active = [
            row for row in all_unique
            if row["promo_status"] == "active"
        ]

        upcoming = [
            row for row in all_unique
            if row["promo_status"] == "upcoming"
        ]

        expired = [
            row for row in all_unique
            if row["promo_status"] == "expired"
        ]

        unknown = [
            row for row in all_unique
            if row["promo_status"] == "unknown"
        ]

        # Hanya promo berjalan dan mendatang yang muncul pada
        # katalog bersih. Riwayat expired tidak dicampur ke katalog.
        current_products = active + upcoming

        current_articles, archived_articles = process_articles(
            source.get("articles", []),
            reference_date,
        )

        print("[4/6] Memisahkan promo aktif, mendatang, dan kedaluwarsa...")

        review_rows = [
            row for row in current_products
            if has_flag(row.get("price_quality_flag"))
        ]

        clean_payload = copy.deepcopy(source)

        clean_payload["products"] = current_products
        clean_payload["articles"] = current_articles
        clean_payload["reference_date"] = reference_date.isoformat()
        clean_payload["checked_at"] = now_iso()

        clean_payload["article_count"] = len(current_articles)
        clean_payload["successful_articles"] = sum(
            1
            for article in current_articles
            if article.get("status") == "ok"
        )

        clean_payload["product_row_count"] = len(current_products)
        clean_payload["active_product_rows"] = len(active)
        clean_payload["upcoming_product_rows"] = len(upcoming)
        clean_payload["expired_product_rows"] = 0
        clean_payload["unknown_period_product_rows"] = 0
        clean_payload["rows_with_price_quality_flags"] = len(review_rows)

        clean_payload["normalization"] = {
            "normalized_at": now_iso(),
            "reference_date": reference_date.isoformat(),
            "source_row_count": len(source_products),
            "unique_offer_count_all_statuses": len(all_unique),
            "duplicates_merged": duplicates,
            "active_offer_count": len(active),
            "upcoming_offer_count": len(upcoming),
            "expired_offer_count_archived": len(expired),
            "unknown_period_offer_count_excluded": len(unknown),
            "current_catalog_count": len(current_products),
            "price_review_count_current": len(review_rows),
            "expired_articles_archived": len(archived_articles),
            "discount_fields": [
                "discount_amount",
                "discount_percent",
                "discount_calculation_status",
            ],
        }

        print("[5/6] Menulis katalog dan arsip...")

        write_json(CLEAN_JSON, clean_payload)
        write_csv(CLEAN_CSV, current_products)
        write_csv(ACTIVE_CSV, active)
        write_csv(HISTORY_CSV, expired)
        write_csv(REVIEW_CSV, review_rows)

        print("[6/6] Selesai.")

    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        return 1

    print()
    print("=" * 64)
    print("HASIL NORMALISASI")
    print("=" * 64)
    print(f"Tanggal referensi              : {reference_date}")
    print(f"Baris sumber                   : {len(source_products)}")
    print(f"Penawaran unik semua status    : {len(all_unique)}")
    print(f"Duplikasi digabung             : {duplicates}")
    print(f"Promo aktif                    : {len(active)}")
    print(f"Promo mendatang                : {len(upcoming)}")
    print(f"Promo kedaluwarsa diarsipkan   : {len(expired)}")
    print(f"Periode tidak diketahui        : {len(unknown)}")
    print(f"Promo di katalog bersih        : {len(current_products)}")
    print(f"Perlu review harga             : {len(review_rows)}")

    print("\nOUTPUT:")
    print(f"- {CLEAN_JSON}")
    print(f"- {CLEAN_CSV}")
    print(f"- {ACTIVE_CSV}")
    print(f"- {HISTORY_CSV}")
    print(f"- {REVIEW_CSV}")

    # Tampilkan contoh perhitungan paket jika tersedia.
    example = next(
        (
            row for row in current_products
            if row.get("bundle_quantity") == 3
            and is_number(row.get("promo_price"))
            and is_number(row.get("normal_price"))
            and row.get("discount_percent") is not None
        ),
        None,
    )

    if example:
        print("\nCONTOH HARGA PAKET:")
        print(f"Produk              : {example.get('product_text')}")
        print(f"Jumlah paket        : {example.get('bundle_quantity')} pcs")
        print(f"Harga normal        : Rp{example.get('normal_price'):,.0f}")
        print(f"Harga promo         : Rp{example.get('promo_price'):,.0f}")
        print(f"Hemat               : Rp{example.get('discount_amount'):,.0f}")
        print(f"Diskon              : {example.get('discount_percent'):.2f}%")
        print(f"Harga promo per pcs : Rp{example.get('unit_promo_price'):,.2f}")

    print("=" * 64)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
