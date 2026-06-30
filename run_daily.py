"""
run_daily.py — Daily orchestrator for the job application automation.

Runs in order:
  1. follow_up.py  — send connection requests for yesterday's applications
  2. linkedin_jobs.py — search + Easy Apply on LinkedIn
  3. google_jobs.py   — search Google Jobs, queue + direct apply

Each step runs independently; failures are caught and logged so the next
step always executes.

Schedule via Windows Task Scheduler:
  Action: python C:\\Dev\\Scheduled\\daily-job-applications\\run_daily.py
  Trigger: Daily at 9:00 AM
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

from job_utils import load_json_list, save_json_atomic

BASE_DIR = Path(__file__).parent
RUN_LOG_FILE = BASE_DIR / "run-log.json"

STEPS = [
    ("follow_up",    "follow_up.py",    "Follow up on yesterday's applications"),
    ("linkedin",     "linkedin_jobs.py", "LinkedIn search + Easy Apply"),
    ("google",       "google_jobs.py",   "Google Jobs search + queue"),
]


def _load_run_log() -> list[dict]:
    return load_json_list(RUN_LOG_FILE)


def _save_run_log(entries: list[dict]) -> None:
    save_json_atomic(RUN_LOG_FILE, entries)


def health_check() -> int:
    """
    Validate local prerequisites without opening browsers or applying anywhere.
    Returns a process exit code.
    """
    print("Daily job application health check")
    print(f"Base directory: {BASE_DIR}")
    print(f"Python: {sys.executable}")

    required_modules = ("playwright", "docx")
    missing_modules = [
        module for module in required_modules
        if importlib.util.find_spec(module) is None
    ]

    required_files = [script for _, script, _ in STEPS] + [
        "filters.py",
        "resume_tailor.py",
    ]
    missing_files = [
        filename for filename in required_files
        if not (BASE_DIR / filename).exists()
    ]

    json_files = [
        BASE_DIR / "applications-log.json",
        BASE_DIR / "google_jobs_queue.json",
        RUN_LOG_FILE,
    ]
    bad_json = []
    for path in json_files:
        if not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            bad_json.append(f"{path.name}: {exc}")

    if missing_modules:
        print(f"Missing Python modules: {', '.join(missing_modules)}")
    if missing_files:
        print(f"Missing files: {', '.join(missing_files)}")
    if bad_json:
        print("Invalid JSON files:")
        for item in bad_json:
            print(f"  - {item}")

    if missing_modules or missing_files or bad_json:
        print("Health check failed.")
        return 1

    print("Health check passed.")
    return 0


def run_step(script_name: str, label: str) -> dict:
    """
    Run a step as a subprocess and capture its result.
    Returns a result dict with status, stdout snippet, and error info.
    """
    script_path = BASE_DIR / script_name
    print(f"\n{'='*60}")
    print(f"[{label}] Starting...")
    print(f"{'='*60}")

    start_time = datetime.utcnow()
    result = {
        "step": label,
        "script": script_name,
        "started_at": start_time.isoformat() + "Z",
        "status": "unknown",
        "error": None,
    }

    try:
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=1800,   # 30 min timeout per step
            cwd=str(BASE_DIR),
            env=env,
        )
        stdout_tail = proc.stdout[-2000:] if proc.stdout else ""
        stderr_tail = proc.stderr[-1000:] if proc.stderr else ""

        print(stdout_tail)
        if stderr_tail:
            print(f"[STDERR] {stderr_tail}")

        if proc.returncode == 0:
            result["status"] = "success"
            print(f"[{label}] Completed successfully.")
        else:
            result["status"] = "failed"
            result["error"] = stderr_tail or f"Exit code {proc.returncode}"
            print(f"[{label}] FAILED (exit code {proc.returncode})")

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = "Step exceeded 30-minute timeout"
        print(f"[{label}] TIMED OUT after 30 minutes.")
    except Exception as e:
        result["status"] = "error"
        result["error"] = traceback.format_exc()
        print(f"[{label}] ERROR: {e}")

    result["finished_at"] = datetime.utcnow().isoformat() + "Z"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily job application automation.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate local prerequisites without opening browsers or applying.",
    )
    args = parser.parse_args()

    if args.check:
        raise SystemExit(health_check())

    print(f"\n{'#'*60}")
    print(f"  Daily Job Application Run — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'#'*60}")

    run_log = _load_run_log()
    run_results = []

    for step_id, script_name, label in STEPS:
        result = run_step(script_name, label)
        run_results.append(result)

    # Persist this run's results
    run_log.append({
        "run_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "run_at": datetime.utcnow().isoformat() + "Z",
        "steps": run_results,
        "overall": "success" if all(r["status"] == "success" for r in run_results) else "partial",
    })
    _save_run_log(run_log)

    # Summary
    print(f"\n{'='*60}")
    print("DAILY RUN SUMMARY")
    print(f"{'='*60}")
    for r in run_results:
        icon = "✓" if r["status"] == "success" else "✗"
        print(f"  {icon} {r['step']}: {r['status']}")
        if r.get("error"):
            print(f"      Error: {r['error'][:200]}")
    print(f"\nRun log saved to: {RUN_LOG_FILE}")


if __name__ == "__main__":
    main()
