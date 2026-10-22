from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

SCRAPER = BASE_DIR / "promo_monitor_module_a.py"
NORMALIZER = BASE_DIR / "normalize_price_flags.py"

ATTEMPT_JSON = BASE_DIR / "promo_catalog_latest_attempt.json"
RAW_JSON = BASE_DIR / "promo_catalog_module_a.json"

OUTPUT_FILES = [
    BASE_DIR / "promo_catalog_module_a_clean.json",
    BASE_DIR / "promo_catalog_products_clean.csv",
    BASE_DIR / "promo_active_clean.csv",
    BASE_DIR / "promo_history_clean.csv",
    BASE_DIR / "promo_price_review_remaining.csv",
]

LOG_DIR = BASE_DIR / "promo_monitor_logs"

# Batas waktu (detik) per langkah. Ubah di sini jika data membesar.
SCRAPER_TIMEOUT = 120
NORMALIZER_TIMEOUT = 30

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


def show_captured_output(output):
    if not output:
        return
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    print(output, end="" if output.endswith("\n") else "\n", flush=True)


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

    try:
        output, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = exc.stdout
        print(
            f"\nTIMEOUT: {path.name} melewati "
            f"{timeout_seconds} detik.",
            flush=True,
        )
        show_captured_output(output)
        raise RuntimeError(
            f"{path.name} tidak selesai dalam batas waktu. "
            "Proses (dan turunannya) dihentikan oleh runner."
        ) from exc
    except BaseException:
        # Ctrl+C atau error lain: jangan tinggalkan proses yatim.
        kill_process_tree(proc)
        raise

    show_captured_output(output)

    if proc.returncode != 0:
        raise RuntimeError(
            f"{path.name} gagal dengan exit code {proc.returncode}."
        )


def validate_scrape(previous_attempt_sig, previous_raw_sig):
    print("\n" + "=" * 60)
    print("[2/4] VALIDASI SCRAPING")
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
    print("[4/4] VERIFIKASI FILE OUTPUT")
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
    filename = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    path = LOG_DIR / filename

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
            SCRAPER, "[1/4] SCRAPING KATALOG PROMO", SCRAPER_TIMEOUT,
        )

        run_step(
            log, "validation", validate_scrape,
            previous_attempt_sig, previous_raw_sig,
        )

        normalize_started_ns = time.time_ns()
        run_step(
            log, "normalization", run_script,
            NORMALIZER, "[3/4] NORMALISASI HARGA PROMO", NORMALIZER_TIMEOUT,
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
