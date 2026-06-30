"""
google_jobs.py — Google Jobs search + queue builder for Juviny Noriega.

Flow:
  1. Search Google Jobs panel for DFW + remote listings (last 24h)
  2. Extract listings from the Google Jobs carousel/panel
  3. Run through filters.py — save qualifying jobs to google_jobs_queue.json
  4. For listings with a direct apply URL (not Workday/Taleo/iCIMS),
     attempt form-fill application via Playwright

Run:  python google_jobs.py
Requires: pip install playwright && playwright install chromium
"""

import asyncio
import json
import re
import traceback
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from filters import is_contractor_role, is_agency_role, is_fake_posting, score_job
from job_utils import already_seen, load_json_list, save_json_atomic
from resume_tailor import tailor_resume

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
QUEUE_FILE = BASE_DIR / "google_jobs_queue.json"
LOG_FILE = BASE_DIR / "applications-log.json"
RESUMES_DIR = BASE_DIR / "tailored_resumes"
RESUMES_DIR.mkdir(exist_ok=True)

SCORE_THRESHOLD = 6.0

# ATS platforms that require account creation — skip direct apply for these
ATS_BLOCKLIST = {
    "workday", "taleo", "icims", "greenhouse.io", "lever.co",
    "smartrecruiters", "bamboohr", "jobvite", "successfactors",
    "myworkday", "careers.google", "recruiting.ultipro",
}

PROFILE = {
    "name": "Your Name",
    "first_name": "Your",
    "last_name": "Name",
    "email": "you@example.com",
    "phone": "5551234567",
    "location": "Your City, ST",
    "city": "Your City",
    "state": "ST",
    "authorized": "Yes",
    "sponsorship": "No",
    "education": "Bachelor's",
    "field": "Your Field",
    "gpa": "",
    "years_experience": "0",
    "linkedin": "linkedin.com/in/your-profile",
}

SEARCH_QUERIES = [
    # Local + hybrid/onsite, last 24h
    (
        "https://www.google.com/search"
        "?q=software+engineer+OR+data+engineer+hybrid+OR+onsite+your+city"
        "&tbs=qdr:d"
    ),
    # Local ML/data/junior, last 24h
    (
        "https://www.google.com/search"
        "?q=data+engineer+OR+ml+engineer+junior+your+city+jobs"
        "&tbs=qdr:d"
    ),
]


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _load_queue() -> list[dict]:
    return load_json_list(QUEUE_FILE)


def _save_queue(entries: list[dict]) -> None:
    save_json_atomic(QUEUE_FILE, entries)


def _load_log() -> list[dict]:
    return load_json_list(LOG_FILE)


def _save_log(entries: list[dict]) -> None:
    save_json_atomic(LOG_FILE, entries)


def _already_queued(queue: list[dict], title: str, company: str) -> bool:
    return any(
        e.get("title") == title and e.get("company") == company
        for e in queue
    )


def _is_ats_url(url: str) -> bool:
    """Return True if the apply URL points to a known ATS requiring an account."""
    url_lower = (url or "").lower()
    return any(ats in url_lower for ats in ATS_BLOCKLIST)


# ---------------------------------------------------------------------------
# Google Jobs panel extraction
# ---------------------------------------------------------------------------

async def extract_google_jobs(page: Page) -> list[dict]:
    """
    Extract job listings from the Google Jobs panel.
    Google renders a carousel of job cards inside the search results.
    """
    jobs = []

    # Click "More jobs" / expand panel if present
    try:
        expand_btn = await page.query_selector("div[jsname='eTl7c'] button, button[aria-label*='more jobs']")
        if expand_btn:
            await expand_btn.click()
            await page.wait_for_timeout(1500)
    except Exception:
        pass

    # Job cards inside the Google Jobs SERP widget
    cards = await page.query_selector_all(
        "li.iFjolb, div.tNxQIb, div[data-ved] div.BjJfJf"
    )

    for card in cards:
        try:
            title_el = await card.query_selector("div.BjJfJf, div.sH3zOn, h3")
            company_el = await card.query_selector("div.vNEEBe, div.KKh3md span")
            location_el = await card.query_selector("div.Qk80Jf, span.r0wTof")
            date_el = await card.query_selector("span.SuWscb, div.LL4CDc")

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            date_text = (await date_el.inner_text()).strip() if date_el else ""

            if not title or not company:
                continue

            # Click the card to reveal the detail panel with apply link
            await card.click()
            await page.wait_for_timeout(1200)

            # Extract apply links from the detail panel
            apply_links = await page.query_selector_all(
                "div.pE8vnd a[href], div.whazf a[href], span.ocNFgb a[href]"
            )
            apply_urls = []
            for link in apply_links:
                href = await link.get_attribute("href")
                if href and href.startswith("http"):
                    apply_urls.append(href)

            # Full description from detail pane
            desc_el = await page.query_selector("div.NgUYpe, div.HBvzbc")
            description = (await desc_el.inner_text()).strip() if desc_el else ""

            jobs.append({
                "title": title,
                "company": company,
                "location": location,
                "date": date_text,
                "description": description,
                "apply_urls": apply_urls,
                "work_type": "",
            })
        except Exception as e:
            print(f"[google] Card extraction error: {e}")
            continue

    return jobs


# ---------------------------------------------------------------------------
# Direct apply via Playwright form fill
# ---------------------------------------------------------------------------

async def attempt_direct_apply(page: Page, job: dict, resume_path: str) -> bool:
    """
    For jobs with a non-ATS direct apply URL, try to fill and submit the form.
    Returns True if form was submitted.
    """
    direct_urls = [u for u in job.get("apply_urls", []) if not _is_ats_url(u)]
    if not direct_urls:
        return False

    apply_url = direct_urls[0]
    print(f"[direct_apply] Navigating to: {apply_url[:80]}")

    try:
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        # Fill common form fields by label/placeholder heuristics
        field_map = {
            "first name": PROFILE["first_name"],
            "last name": PROFILE["last_name"],
            "full name": PROFILE["name"],
            "name": PROFILE["name"],
            "email": PROFILE["email"],
            "phone": PROFILE["phone"],
            "city": PROFILE["city"],
            "location": PROFILE["location"],
            "linkedin": PROFILE["linkedin"],
            "gpa": PROFILE["gpa"],
            "years": PROFILE["years_experience"],
        }

        inputs = await page.query_selector_all("input[type='text'], input[type='email'], input[type='tel'], input[type='number']")
        for inp in inputs:
            try:
                placeholder = (await inp.get_attribute("placeholder") or "").lower()
                name_attr = (await inp.get_attribute("name") or "").lower()
                aria_label = (await inp.get_attribute("aria-label") or "").lower()
                hint = f"{placeholder} {name_attr} {aria_label}"

                for key, val in field_map.items():
                    if key in hint:
                        await inp.triple_click()
                        await inp.fill(str(val))
                        break
            except Exception:
                continue

        # Upload resume if there's a file input
        file_inputs = await page.query_selector_all("input[type='file']")
        for fi in file_inputs:
            try:
                accepted = (await fi.get_attribute("accept") or "").lower()
                if "pdf" in accepted or "doc" in accepted or accepted == "":
                    await fi.set_input_files(resume_path)
                    break
            except Exception:
                continue

        # Handle select dropdowns for authorization/sponsorship
        selects = await page.query_selector_all("select")
        for sel in selects:
            try:
                name_attr = (await sel.get_attribute("name") or "").lower()
                aria_label = (await sel.get_attribute("aria-label") or "").lower()
                hint = f"{name_attr} {aria_label}"
                if "sponsor" in hint:
                    await sel.select_option(label=re.compile(r"no", re.I))
                elif "authorized" in hint or "authorization" in hint:
                    await sel.select_option(label=re.compile(r"yes", re.I))
            except Exception:
                continue

        # Try clicking the submit button
        submit_btn = await page.query_selector(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Apply'), button:has-text('Submit')"
        )
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_timeout(2500)
            print(f"[direct_apply] Submitted form for {job['title']} @ {job['company']}")
            return True
        else:
            print(f"[direct_apply] No submit button found at {apply_url}")
            return False

    except Exception as e:
        print(f"[direct_apply] Error: {e}")
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    queue = _load_queue()
    log = _load_log()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=40)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        for search_url in SEARCH_QUERIES:
            print(f"\n[search] {search_url[:80]}...")
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2500)
            except Exception as e:
                print(f"[search] Failed: {e}")
                continue

            jobs = await extract_google_jobs(page)
            print(f"[search] Extracted {len(jobs)} raw listings")

            for job in jobs:
                if already_seen(log, job):
                    print(f"[skip] Already logged: {job['title']} @ {job['company']}")
                    continue

                # Filter
                if is_agency_role(job["company"]):
                    print(f"[filter] Agency: {job['company']}")
                    continue
                if is_contractor_role(job["title"], job["description"]):
                    print(f"[filter] Contractor: {job['title']}")
                    continue
                if is_fake_posting(job["title"], job["company"], job["description"]):
                    print(f"[filter] Fake: {job['title']} @ {job['company']}")
                    continue

                score = score_job(
                    job["title"], job["company"], job["location"],
                    job["description"], job["work_type"]
                )
                print(f"[score] {score:.1f} — {job['title']} @ {job['company']}")

                if score < SCORE_THRESHOLD:
                    continue

                # Add to queue for manual review
                if not _already_queued(queue, job["title"], job["company"]):
                    queue_entry = {**job, "score": score, "queued_at": datetime.utcnow().isoformat() + "Z"}
                    queue.append(queue_entry)
                    _save_queue(queue)
                    print(f"[queue] Added: {job['title']} @ {job['company']}")

                # Attempt direct apply if URL is not ATS-gated
                has_direct = any(not _is_ats_url(u) for u in job.get("apply_urls", []))
                if has_direct:
                    safe_company = re.sub(r"[^\w]", "_", job["company"])[:30]
                    safe_title = re.sub(r"[^\w]", "_", job["title"])[:30]
                    resume_path = str(RESUMES_DIR / f"resume_{safe_title}_{safe_company}.docx")
                    try:
                        tailor_resume(job["title"], job["company"], job["description"], resume_path)
                    except Exception as e:
                        print(f"[resume] Failed: {e}")
                        resume_path = ""

                    if resume_path:
                        success = await attempt_direct_apply(page, job, resume_path)
                        log.append({
                            "title": job["title"],
                            "company": job["company"],
                            "location": job["location"],
                            "url": job.get("apply_urls", [""])[0],
                            "score": score,
                            "status": "applied" if success else "queued",
                            "resume": resume_path,
                            "applied_at": datetime.utcnow().isoformat() + "Z",
                            "follow_up_sent": False,
                            "source": "google",
                        })
                        _save_log(log)
                        await page.wait_for_timeout(3000)

        await browser.close()

    print(f"\n[done] Queue saved to {QUEUE_FILE} ({len(queue)} entries)")


if __name__ == "__main__":
    asyncio.run(run())
