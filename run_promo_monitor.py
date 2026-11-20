from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

SCRAPER = BASE_DIR / "src" / "scraper.py"
NORMALIZER = BASE_DIR / "src" / "normalizer.py"
EXCEL_REPORT = BASE_DIR / "src" / "excel_report.py"

RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"
ATTEMPT_JSON = RAW_DIR / "promo_catalog_latest_attempt.json"
RAW_JSON = RAW_DIR / "promo_catalog_module_a.json"

OUTPUT_FILES = [
    PROCESSED_DIR / "promo_catalog_module_a_clean.json",
    PROCESSED_DIR / "promo_catalog_products_clean.csv",
    PROCESSED_DIR / "promo_active_clean.csv",
    PROCESSED_DIR / "promo_history_clean.csv",
    PROCESSED_DIR / "promo_price_review_remaining.csv",
    PROCESSED_DIR / "promo_monitor.xlsx",
]

LOG_DIR = BASE_DIR / "reports"

# Batas waktu (detik) per langkah. Ubah di sini jika data membesar.
SCRAPER_TIMEOUT = 120
NORMALIZER_TIMEOUT = 30
EXCEL_REPORT_TIMEOUT = 60

# Lock agar dua run tidak menulis file yang sama bersamaan.
LOCK_FILE = BASE_DIR / "promo_monitor.lock"
LOCK_STALE_SECONDS = 3600


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def signature(path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def read_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def acquire_lock():
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(LOCK_FILE, flags)
    except FileExistsError:
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise RuntimeError(
                f"Pipeline lain masih berjalan (lock {LOCK_FILE.name}, "
                f"umur {int(age)} detik). Run ini dibatalkan."
            )
        print(f"Lock lama ({int(age)} detik) dianggap basi, dihapus.")
        LOCK_FILE.unlink(missing_ok=True)
        fd = os.open(LOCK_FILE, flags)

    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def kill_process_tree(proc):
    """Matikan proses beserta seluruh proses turunannya (mis. browser)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.kill()


def run_script(path, title, timeout_seconds):
    print("\n" + "=" * 60, flush=True)
    print(title, flush=True)
    print(f"Script: {path.name}", flush=True)
    print(f"Batas waktu: {timeout_seconds} detik", flush=True)
    print("=" * 60, flush=True)

    if not path.is_file():
        raise RuntimeError(f"File tidak ditemukan: {path}")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-u", str(path)],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    lines = queue.Queue()

    def stream_output():
        try:
            for line in iter(proc.stdout.readline, ""):
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=stream_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds

    try:
        reader_finished = False
        while not reader_finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and proc.poll() is None:
                kill_process_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                while True:
                    try:
                        item = lines.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        break
                    print(item, end="", flush=True)
                print(
                    f"\nTIMEOUT: {path.name} melewati "
                    f"{timeout_seconds} detik.",
                    flush=True,
                )
                raise RuntimeError(
                    f"{path.name} tidak selesai dalam batas waktu. "
                    "Proses (dan turunannya) dihentikan oleh runner."
                )

            try:
                item = lines.get(timeout=min(0.25, max(0.01, remaining)))
            except queue.Empty:
                if proc.poll() is not None and not reader.is_alive():
                    break
                continue

            if item is None:
                reader_finished = True
            else:
                print(item, end="", flush=True)

        proc.wait()
    except BaseException:
        # Ctrl+C atau error lain: jangan tinggalkan proses yatim.
        kill_process_tree(proc)
        raise

    if proc.returncode != 0:
        raise RuntimeError(
            f"{path.name} gagal dengan exit code {proc.returncode}."
        )


def validate_scrape(previous_attempt_sig, previous_raw_sig):
    print("\n" + "=" * 60)
    print("[2/5] VALIDASI SCRAPING")
    print("=" * 60)

    if not ATTEMPT_JSON.is_file():
        raise RuntimeError(
            "Laporan promo_catalog_latest_attempt.json tidak ditemukan."
        )

    if signature(ATTEMPT_JSON) == previous_attempt_sig:
        raise RuntimeError(
            "Laporan scraping tidak berubah. "
            "Hasil validasi lama tidak akan digunakan."
        )

    attempt = read_json(ATTEMPT_JSON)
    validation = attempt.get("validation", {})

    print("Laporan terbaru berubah: YA")
    print(f"Validasi valid: {validation.get('valid')}")

    if validation.get("valid") is not True:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        raise RuntimeError(
            "Validasi scraping gagal. Normalisasi dibatalkan."
        )

    if not RAW_JSON.is_file():
        raise RuntimeError("Katalog sumber tidak ditemukan.")

    if signature(RAW_JSON) == previous_raw_sig:
        print(
            "PERINGATAN: katalog sumber (promo_catalog_module_a.json) "
            "tidak berubah sejak run sebelumnya."
        )


def verify_outputs(since_ns):
    print("\n" + "=" * 60)
    print("[5/5] VERIFIKASI FILE OUTPUT")
    print("=" * 60)
    failures = []

    for path in OUTPUT_FILES:
        if not path.is_file() or path.stat().st_size == 0:
            print(f"[GAGAL] {path.name} (hilang atau kosong)")
            failures.append(path.name)
        elif path.stat().st_mtime_ns < since_ns:
            print(f"[GAGAL] {path.name} (tidak diperbarui run ini)")
            failures.append(path.name)
        else:
            print(f"[OK] {path.name} ({path.stat().st_size:,} byte)")

    if failures:
        raise RuntimeError(
            "File output hilang, kosong, atau basi: " + ", ".join(failures)
        )


def save_log(data):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "run_latest.json"

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return path


def run_step(log, name, func, *args, **kwargs):
    """Jalankan satu langkah, catat nama dan durasinya ke log."""
    log["current_step"] = name
    t0 = time.monotonic()
    result = func(*args, **kwargs)
    log["steps"].append(
        {"name": name, "seconds": round(time.monotonic() - t0, 2)}
    )
    return result


def main():
    # Konsol Windows sering bukan UTF-8; cegah UnicodeEncodeError palsu.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    started = time.monotonic()
    log = {
        "started_at": now(),
        "status": "running",
        "steps": [],
        "failed_step": None,
        "error": None,
    }
    locked = False

    print("=" * 60)
    print("PROMO MONITOR - PIPELINE UTAMA")
    print(f"Folder: {BASE_DIR}")
    print("=" * 60)

    try:
        acquire_lock()
        locked = True

        previous_attempt_sig = signature(ATTEMPT_JSON)
        previous_raw_sig = signature(RAW_JSON)

        run_step(
            log, "scraping", run_script,
            SCRAPER, "[1/5] SCRAPING KATALOG PROMO", SCRAPER_TIMEOUT,
        )

        run_step(
            log, "validation", validate_scrape,
            previous_attempt_sig, previous_raw_sig,
        )

        normalize_started_ns = time.time_ns()
        run_step(
            log, "normalization", run_script,
            NORMALIZER, "[3/5] NORMALISASI HARGA PROMO", NORMALIZER_TIMEOUT,
        )

        run_step(
            log, "excel_report", run_script,
            EXCEL_REPORT, "[4/5] MEMBUAT LAPORAN EXCEL", EXCEL_REPORT_TIMEOUT,
        )

        run_step(
            log, "output_verification", verify_outputs,
            normalize_started_ns,
        )

        log["status"] = "success"

    except KeyboardInterrupt:
        log["status"] = "interrupted"
        log["failed_step"] = log.get("current_step")
        log["error"] = "Dihentikan dengan Ctrl+C"
        print("\nProses dihentikan pengguna.")

    except Exception as exc:
        log["status"] = "failed"
        log["failed_step"] = log.get("current_step")
        log["error"] = str(exc)
        print(f"\nPIPELINE GAGAL: {exc}")
        if log["failed_step"]:
            print(f"Langkah yang gagal: {log['failed_step']}")

    finally:
        if locked:
            try:
                release_lock()
            except Exception as exc:
                print(f"Gagal menghapus lock: {exc}")

        log.pop("current_step", None)
        log["finished_at"] = now()
        log["duration_seconds"] = round(time.monotonic() - started, 2)

        try:
            log_path = save_log(log)
            print(f"Log: {log_path}")
        except Exception as exc:
            print(f"Gagal menulis log: {exc}")

    print("\n" + "=" * 60)
    print(f"STATUS: {log['status'].upper()}")
    print(f"Durasi: {log['duration_seconds']} detik")
    print("=" * 60)

    return 0 if log["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
