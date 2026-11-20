from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_JSON = PROJECT_ROOT / "data" / "raw" / "promo_catalog_module_a.json"
CLEAN_JSON = PROJECT_ROOT / "data" / "processed" / "promo_catalog_module_a_clean.json"
OUTPUT_XLSX = PROJECT_ROOT / "data" / "processed" / "promo_monitor.xlsx"
MERCHANTS = ("Alfamart", "Alfamidi", "Indomaret")


def read_catalog() -> dict[str, Any]:
    path = CLEAN_JSON if CLEAN_JSON.is_file() else RAW_JSON
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Format katalog tidak valid: {path}")
    return data


def as_excel_date(value: Any):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def date_span(start: Any, end: Any) -> str:
    first = as_excel_date(start)
    last = as_excel_date(end)
    if not first or not last:
        return "Periode lihat sumber"
    if first.year == last.year and first.month == last.month:
        return f"{first.day}–{last.day} {first.strftime('%b %Y')}"
    return f"{first.strftime('%d %b %Y')} – {last.strftime('%d %b %Y')}"


def product_is_clear(row: dict[str, Any]) -> bool:
    name = " ".join(str(row.get("product_text") or "").split()).casefold()
    if not name or name.startswith(("q:", "apa saja", "produk apa", "katalog promo", "katalog belanja")):
        return False
    if name in {"shopeepay", "spaylater", "gopay", "dana", "qris", "cashback"}:
        return False
    return True


def build_rows(catalog: dict[str, Any]) -> list[list[Any]]:
    articles = [
        article for article in catalog.get("articles", [])
        if isinstance(article, dict)
        and article.get("status") == "ok"
        and article.get("merchant") in MERCHANTS
        and article.get("promo_status") in {"active", "upcoming"}
        and article.get("source_url")
    ]
    article_by_url = {article["source_url"]: article for article in articles}
    products_by_url: dict[str, list[dict[str, Any]]] = {}
    for product in catalog.get("products", []):
        if not isinstance(product, dict) or not product_is_clear(product):
            continue
        source_url = product.get("source_url")
        if source_url in article_by_url and product.get("promo_status") in {"active", "upcoming"}:
            products_by_url.setdefault(source_url, []).append(product)

    rows = []
    for article in articles:
        source_url = article["source_url"]
        item_rows = products_by_url.get(source_url) or [None]
        for product in item_rows:
            item = product or {}
            promo_name = item.get("product_text") or article.get("title") or "Promo"
            category = item.get("section") or article.get("campaign_type") or "Promo"
            if category == "Lainnya":
                category = "Promo"
            published = as_excel_date(article.get("published_at"))
            rows.append([
                promo_name,
                category,
                article.get("merchant"),
                item.get("normal_price"),
                item.get("promo_price"),
                item.get("discount_amount"),
                item.get("discount_percent"),
                date_span(article.get("valid_from"), article.get("valid_until")),
                "Aktif" if article.get("promo_status") == "active" else "Akan Datang",
                published,
                source_url,
            ])

    rows.sort(key=lambda row: (MERCHANTS.index(row[2]), row[8] != "Aktif", str(row[7]), row[0]))
    return rows


def build_report() -> Path:
    catalog = read_catalog()
    rows = build_rows(catalog)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Promo"
    sheet.sheet_view.showGridLines = False

    sheet.merge_cells("A1:K1")
    sheet["A1"] = "PROMO ALFAMART · ALFAMIDI · INDOMARET"
    sheet["A1"].font = Font(name="Arial", size=15, bold=True, color="17365D")
    sheet["A1"].alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 28

    checked_at = str(catalog.get("checked_at") or "—")
    try:
        checked_label = datetime.fromisoformat(checked_at).strftime("%d %b %Y %H:%M WIB")
    except ValueError:
        checked_label = checked_at
    note = f"Sumber: KatalogPromosi | Update terakhir: {checked_label} | Harga kosong jika katalog hanya tersedia sebagai gambar. Klik ‘Lihat Promo’ untuk detail."
    sheet.merge_cells("A2:K2")
    sheet["A2"] = note
    sheet["A2"].font = Font(name="Arial", size=9, italic=True, color="5B6573")
    sheet["A2"].alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[2].height = 24
    sheet.row_dimensions[3].height = 8

    headers = [
        "Nama Produk / Promo", "Kategori", "Merchant",
        "Harga Normal/Acuan (Rp)", "Harga Promo (Rp)", "Hemat (Rp)",
        "Diskon (%)", "Masa Berlaku", "Status", "Update", "Link Promo",
    ]
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=4, column=column, value=header)
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[4].height = 34

    for row_num, values in enumerate(rows, start=5):
        for column, value in enumerate(values[:11], start=1):
            cell = sheet.cell(row=row_num, column=column, value=value)
            cell.font = Font(name="Arial", size=10, color="243447")
            cell.alignment = Alignment(vertical="center", wrap_text=column in {1, 2, 8})
            if column in {4, 5, 6} and isinstance(value, (int, float)):
                cell.number_format = '"Rp" #,##0'
            elif column == 7 and isinstance(value, (int, float)):
                cell.number_format = '0.0"%"'
            elif column == 10 and value:
                cell.number_format = "dd mmm yyyy"
            elif column == 11 and value:
                cell.hyperlink = value
                cell.value = "Lihat Promo"
                cell.style = "Hyperlink"
            elif column == 9:
                cell.fill = PatternFill("solid", fgColor="E2F0D9" if value == "Aktif" else "D9EAF7")
                cell.font = Font(name="Arial", size=10, bold=True, color="243447")
        sheet.row_dimensions[row_num].height = 25

    if rows:
        table = Table(displayName="PromoTable", ref=f"A4:K{4 + len(rows)}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)
    else:
        sheet["A5"] = "Belum ada promo aktif atau akan datang. Jalankan scraper untuk memperbarui data."
        sheet.merge_cells("A5:K5")

    widths = {
        "A": 42, "B": 24, "C": 16, "D": 21, "E": 18, "F": 16,
        "G": 13, "H": 23, "I": 16, "J": 16, "K": 16,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A5"
    sheet.auto_filter.ref = f"A4:K{4 + len(rows)}" if rows else "A4:K4"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = "1:4"

    OUTPUT_XLSX.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(OUTPUT_XLSX)
    return OUTPUT_XLSX


if __name__ == "__main__":
    print(f"Excel promo: {build_report()}")
