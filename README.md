# Merchants Monitor

Scraper promo Alfamart, Alfamidi, dan Indomaret dari [KatalogPromosi](https://katalogpromosi.com/). Scraper membaca beranda dan halaman kategori tiap merchant, menyimpan tautan artikel, tanggal terbit, periode promo, ringkasan, gambar katalog, serta baris produk yang dapat dibaca sebagai teks.

## Menjalankan

Gunakan Python 3.10 atau lebih baru.

```powershell
python -m pip install -r requirements.txt
python run_promo_monitor.py
```

Untuk mengambil data saja tanpa normalisasi:

```powershell
python src/merchants_monitor/scraper.py
```

## Struktur

```text
merchants-monitor/
├── data/
│   ├── raw/                 # Data langsung dari scraper
│   └── processed/           # Katalog dan CSV hasil normalisasi
├── reports/
│   ├── promo_catalog_runlog.json
│   └── <snapshot katalog>.json
├── src/
│   ├── scraper.py             # Ambil promo dari KatalogPromosi
│   └── normalizer.py          # Rapikan data dan hitung flag harga
├── run_promo_monitor.py     # Entry point pipeline
└── requirements.txt
```

Output promo aktif berada di `data/processed/promo_active_clean.csv`. Detail artikel dan tautan gambar katalog ada di `data/raw/promo_catalog_module_a.json`.

## Catatan sumber

Konten dan tanggal berlaku mengikuti informasi yang diterbitkan KatalogPromosi. Gambar katalog disertakan sebagai URL; produk yang hanya tertulis di dalam gambar belum diekstrak melalui OCR.
