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
│   └── run_latest.json       # Status pipeline terakhir
├── src/
│   ├── scraper.py             # Ambil promo dari KatalogPromosi
│   └── normalizer.py          # Rapikan data dan hitung flag harga
├── run_promo_monitor.py     # Entry point pipeline
└── requirements.txt
```

Workbook hasil pipeline berada di `data/processed/promo_monitor.xlsx`. Isinya satu tabel promo dengan filter merchant, status, dan masa berlaku. Harga hanya diisi jika terbaca jelas sebagai data produk; katalog yang hanya berupa gambar tetap bisa dibuka lewat tautan sumber. File data mentah dan laporan run tersimpan lokal dan diabaikan Git.

## Catatan sumber

Konten dan tanggal berlaku mengikuti informasi yang diterbitkan KatalogPromosi. Gambar katalog disertakan sebagai URL; produk yang hanya tertulis di dalam gambar belum diekstrak melalui OCR.
