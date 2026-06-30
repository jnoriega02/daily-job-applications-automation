"""
follow_up.py — Send LinkedIn follow-up messages for yesterday's applications.

Flow:
  1. Read applications-log.json
  2. Find entries from yesterday where follow_up_sent = false
  3. For each, search LinkedIn for the job poster/recruiter via the job URL
  4. Send a connection request or InMail with personalized template
  5. Mark follow_up_sent = true in the log

Run:  python follow_up.py
Requires: pip install playwright && playwright install chromium
          linkedin_cookies.json must exist (run linkedin_jobs.py first)
"""

import asyncio
import json
import re
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

from job_utils import load_json_list, save_json_atomic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "applications-log.json"
COOKIES_FILE = BASE_DIR / "linkedin_cookies.json"

# Skills to highlight per role keyword (used in message personalization)
ROLE_SKILL_MAP = {
    "data engineer": ["ETL pipeline development", "Hive/SQL data engineering"],
    "ml engineer":   ["ML/NLP model development", "Python/PyTorch"],
    "ai engineer":   ["AI/NLP systems", "Python/PyTorch"],
    "software engineer": ["Python backend development", "full-stack engineering"],
    "backend":       ["Python/FastAPI development", "REST API design"],
    "tpm":           ["technical product management", "Agile delivery"],
    "analytics":     ["data pipelines", "SQL/Python analytics"],
    "platform":      ["platform engineering", "DevOps/cloud infrastructure"],
    "default":       ["Python engineering", "data-driven problem solving"],
}

MESSAGE_TEMPLATE = (
    "Hi {name}, I applied for the {title} role at {company} yesterday and "
    "wanted to follow up briefly. My background in {skill1} and {skill2} "
    "aligns closely with what you're looking for, and I'd love to connect. "
    "Your Name | you@example.com"
)


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def _load_log() -> list[dict]:
    return load_json_list(LOG_FILE)


def _save_log(entries: list[dict]) -> None:
    save_json_atomic(LOG_FILE, entries)


def _get_yesterday_entries(log: list[dict]) -> list[dict]:
    """Return log entries applied yesterday where follow_up_sent is False."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    results = []
    for entry in log:
        if entry.get("follow_up_sent"):
            continue
        status = (entry.get("status") or "").lower()
        applied_via = (entry.get("applied_via") or "").lower()
        if status not in {"applied", "verified_applied"} and "apply" not in applied_via:
            continue
        applied_str = entry.get("applied_at", "")
        if not applied_str:
            legacy_date = entry.get("date", "")
            if legacy_date:
                try:
                    if datetime.fromisoformat(legacy_date).date() == yesterday:
                        results.append(entry)
                except ValueError:
                    pass
            continue
        try:
            applied_date = datetime.fromisoformat(applied_str.replace("Z", "+00:00")).date()
            if applied_date == yesterday:
                results.append(entry)
        except ValueError:
            continue
    return results


def _pick_skills(title: str) -> tuple[str, str]:
    """Select two relevant skills based on job title."""
    title_lower = title.lower()
    for keyword, skills in ROLE_SKILL_MAP.items():
        if keyword in title_lower:
            return skills[0], skills[1] if len(skills) > 1 else skills[0]
    return ROLE_SKILL_MAP["default"][0], ROLE_SKILL_MAP["default"][-1]


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

async def load_cookies(context: BrowserContext) -> bool:
    if COOKIES_FILE.exists():
        try:
            cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            await context.add_cookies(cookies)
            return True
        except Exception as e:
            print(f"[cookies] Failed to load: {e}")
    return False


async def save_cookies(context: BrowserContext) -> None:
    try:
        cookies = await context.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LinkedIn recruiter search
# ---------------------------------------------------------------------------

async def find_recruiter_on_job_page(page: Page, job_url: str) -> dict | None:
    """
    Visit the LinkedIn job page and look for the poster/recruiter card
    that LinkedIn sometimes shows below the job description.
    Returns dict with name and profile_url, or None if not found.
    """
    try:
        if not job_url or "linkedin.com" not in job_url:
            return None

        await page.goto(job_url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        # LinkedIn shows a "Meet the hiring team" or "Job poster" section
        poster_el = await page.query_selector(
            "div.hirer-card__hirer-information a, "
            "div.job-details-jobs-unified-top-card__job-insight--highlight a, "
            "a[data-control-name='job_details_hirer_info']"
        )
        if not poster_el:
            return None

        name_el = await poster_el.query_selector("span.hirer-card__hirer-name, span.t-bold")
        name = (await name_el.inner_text()).strip() if name_el else ""
        profile_url = await poster_el.get_attribute("href") or ""

        if name:
            return {"name": name.split()[0], "profile_url": profile_url}
    except Exception as e:
        print(f"[recruiter] Error finding recruiter: {e}")
    return None


# ---------------------------------------------------------------------------
# LinkedIn messaging
# ---------------------------------------------------------------------------

async def send_connection_request(page: Page, profile_url: str, message: str) -> bool:
    """
    Navigate to a LinkedIn profile and send a connection request with a note.
    Returns True on success.
    """
    try:
        full_url = profile_url if profile_url.startswith("http") else f"https://www.linkedin.com{profile_url}"
        await page.goto(full_url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        # Click Connect button
        connect_btn = await page.query_selector(
            "button[aria-label*='Connect'], button:has-text('Connect')"
        )
        if not connect_btn:
            print("[follow_up] No Connect button found — may already be connected or has follow button.")
            return False

        await connect_btn.click()
        await page.wait_for_timeout(1500)

        # Click "Add a note"
        add_note_btn = await page.query_selector(
            "button[aria-label='Add a note'], button:has-text('Add a note')"
        )
        if add_note_btn:
            await add_note_btn.click()
            await page.wait_for_timeout(1000)

            note_textarea = await page.query_selector("textarea#custom-message, textarea[name='message']")
            if note_textarea:
                # LinkedIn limits connection notes to 300 chars
                await note_textarea.fill(message[:300])
                await page.wait_for_timeout(500)

        # Click Send / Done
        send_btn = await page.query_selector(
            "button[aria-label='Send now'], button:has-text('Send'), "
            "button[aria-label='Done']"
        )
        if send_btn:
            await send_btn.click()
            await page.wait_for_timeout(1500)
            print("[follow_up] Connection request sent.")
            return True

    except Exception as e:
        print(f"[follow_up] Error sending connection: {e}")
        traceback.print_exc()
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    log = _load_log()
    entries_to_follow_up = _get_yesterday_entries(log)

    if not entries_to_follow_up:
        print("[follow_up] No entries to follow up on today.")
        return

    print(f"[follow_up] {len(entries_to_follow_up)} applications to follow up on.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=50)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await load_cookies(context)
        page = await context.new_page()

        # Verify still logged in
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15_000)
        await page.wait_for_timeout(1500)
        if "login" in page.url:
            print("[follow_up] Not logged in — run linkedin_jobs.py first to refresh cookies.")
            await browser.close()
            return

        for entry in entries_to_follow_up:
            try:
                job_url = entry.get("url", "")
                title = entry.get("title", "")
                company = entry.get("company", "")

                print(f"\n[follow_up] Processing: {title} @ {company}")

                recruiter = await find_recruiter_on_job_page(page, job_url)

                # Build message
                skill1, skill2 = _pick_skills(title)
                recruiter_name = recruiter["name"] if recruiter else "there"
                message = MESSAGE_TEMPLATE.format(
                    name=recruiter_name,
                    title=title,
                    company=company,
                    skill1=skill1,
                    skill2=skill2,
                )

                sent = False
                if recruiter and recruiter.get("profile_url"):
                    sent = await send_connection_request(page, recruiter["profile_url"], message)
                    await save_cookies(context)

                if not sent:
                    print(f"[follow_up] Could not send for {title} @ {company} — recruiter not found or send failed.")

                # Update log entry regardless (prevents repeated attempts)
                for log_entry in log:
                    if (
                        log_entry.get("title") == title
                        and log_entry.get("company") == company
                        and not log_entry.get("follow_up_sent")
                    ):
                        log_entry["follow_up_sent"] = True
                        log_entry["follow_up_at"] = datetime.utcnow().isoformat() + "Z"
                        log_entry["follow_up_status"] = "sent" if sent else "recruiter_not_found"
                        break

                _save_log(log)
                await page.wait_for_timeout(3000)  # polite delay

            except Exception as e:
                print(f"[follow_up] Error for {entry.get('title', '')}: {e}")
                traceback.print_exc()
                continue

        await browser.close()

    print("[follow_up] Done.")


if __name__ == "__main__":
    asyncio.run(run())
