"""
linkedin_jobs.py — LinkedIn job search + Easy Apply automation.

Flow:
  1. Load cookies from linkedin_cookies.json (or prompt manual login + save cookies)
  2. Search multiple LinkedIn URLs (DFW hybrid/onsite first, remote fallback)
  3. Filter + score each listing via filters.py (keep score >= 6, stop at 5 qualified)
  4. For each qualified job: extract full description, tailor resume, upload to LinkedIn,
     click Easy Apply, fill all modal steps, log to applications-log.json

Run:  python linkedin_jobs.py
Requires: pip install playwright python-docx && playwright install chromium
"""

import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

from filters import is_contractor_role, is_agency_role, is_entry_level_role, is_fake_posting, score_job
from job_utils import already_seen, load_json_list, save_json_atomic
from resume_tailor import tailor_resume

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
COOKIES_FILE = BASE_DIR / "linkedin_cookies.json"
LOG_FILE = BASE_DIR / "applications-log.json"
RESUMES_DIR = BASE_DIR / "tailored_resumes"
RESUMES_DIR.mkdir(exist_ok=True)

SCORE_THRESHOLD = 6.0
MAX_APPLICATIONS = 5

PROFILE = {
    "name": "Your Name",
    "email": "you@example.com",
    "phone": "5551234567",
    "location": "Your City, ST",
    "authorized": True,
    "sponsorship": False,
    "education": "Bachelor's",
    "field": "Your Field",
    "gpa": "",
    "years_experience": 0,
    "linkedin": "linkedin.com/in/your-profile",
}

# Search URLs — customize these for your target roles and locations.
SEARCH_URLS = [
    # Local hybrid + onsite, Easy Apply, last 24h
    (
        "https://www.linkedin.com/jobs/search/"
        "?keywords=software+engineer+OR+data+engineer"
        "&location=Your+City"
        "&f_TPR=r86400&f_WT=3%2C1&f_AL=true"
    ),
    # Local ML/AI/TPM, last 24h
    (
        "https://www.linkedin.com/jobs/search/"
        "?keywords=ml+engineer+OR+ai+engineer+OR+technical+product+manager"
        "&location=Your+City"
        "&f_TPR=r86400&f_WT=3%2C1"
    ),
    # US remote Easy Apply fallback, last 24h
    (
        "https://www.linkedin.com/jobs/search/"
        "?keywords=software+engineer+OR+data+engineer"
        "&location=United+States"
        "&f_TPR=r86400&f_WT=2&f_AL=true"
    ),
]


# ---------------------------------------------------------------------------
# Session / cookie helpers
# ---------------------------------------------------------------------------

async def load_cookies(context: BrowserContext) -> bool:
    """Load saved cookies into the browser context. Returns True if loaded."""
    if COOKIES_FILE.exists():
        try:
            cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            await context.add_cookies(cookies)
            print("[cookies] Loaded from linkedin_cookies.json")
            return True
        except Exception as e:
            print(f"[cookies] Failed to load: {e}")
    return False


async def save_cookies(context: BrowserContext) -> None:
    """Persist current browser cookies to disk."""
    try:
        cookies = await context.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        print("[cookies] Saved to linkedin_cookies.json")
    except Exception as e:
        print(f"[cookies] Failed to save: {e}")


async def ensure_logged_in(page: Page, context: BrowserContext) -> None:
    """
    Navigate to LinkedIn. If not authenticated, wait for the user to log in
    manually (up to 3 minutes), then save cookies.
    """
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    # Check if we're on the feed (logged in) or redirected to login
    if "feed" in page.url or "mynetwork" in page.url:
        print("[auth] Already logged in.")
        await save_cookies(context)
        return

    print("[auth] Not logged in. Opening login page — please log in manually.")
    print("[auth] You have 3 minutes.")
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    # Wait until URL contains /feed or /mynetwork
    try:
        await page.wait_for_url(re.compile(r"linkedin\.com/(feed|mynetwork|jobs)"), timeout=180_000)
        print("[auth] Login detected. Saving cookies.")
        await save_cookies(context)
    except PWTimeout:
        raise RuntimeError("[auth] Login timed out after 3 minutes. Please re-run the script.")


async def handle_challenge(page: Page, timeout: int = 120_000) -> bool:
    """
    If LinkedIn threw a security challenge/CAPTCHA, wait for the user to solve it.
    Returns True if resolved, False if timed out.
    """
    if "checkpoint" not in page.url and "challenge" not in page.url:
        return True

    print("\n[challenge] LinkedIn security check detected!")
    print(f"[challenge] Please complete it in the browser window. Waiting up to {timeout // 1000}s...\n")
    try:
        await page.wait_for_url(
            re.compile(r"linkedin\.com/(feed|jobs|mynetwork)"),
            timeout=timeout,
        )
        print("[challenge] Resolved. Continuing.\n")
        return True
    except Exception:
        print("[challenge] Timed out waiting for challenge resolution.")
        return False


async def dismiss_overlay(page: Page) -> None:
    """Close any modal overlay that might intercept clicks."""
    try:
        for selector in (
            "button.artdeco-modal__dismiss",
            "button[aria-label='Dismiss']",
            "button[data-test-modal-close-btn]",
        ):
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await page.wait_for_timeout(400)
                return
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Job listing extraction
# ---------------------------------------------------------------------------

async def scroll_and_extract_jobs(page: Page) -> list[dict]:
    """
    Scroll the results panel to load all jobs on the current search page,
    then extract structured listing data.
    """
    jobs = []

    # Bail early if we landed on a challenge page
    if "checkpoint" in page.url or "challenge" in page.url:
        return jobs

    # Scroll the job list panel to lazy-load all cards
    for _ in range(8):
        try:
            await page.evaluate("window.scrollBy(0, 600)")
            await page.wait_for_timeout(600)
        except Exception:
            break  # Page navigated away; stop scrolling

    # LinkedIn changes class names frequently. Prefer stable job id/link
    # attributes and parse visible card text as a fallback.
    try:
        jobs = await page.evaluate(
            """() => {
                const cards = [...document.querySelectorAll(
                    'li[data-occludable-job-id], li.jobs-search-results__list-item, div[data-job-id]'
                )];
                const seen = new Set();
                return cards.map((card) => {
                    const link = card.querySelector('a[href*="/jobs/view/"]');
                    const href = link ? link.href.split('?')[0] : '';
                    const idMatch = href.match(/\\/jobs\\/view\\/(\\d+)/);
                    const jobId = card.getAttribute('data-occludable-job-id')
                        || card.querySelector('[data-job-id]')?.getAttribute('data-job-id')
                        || (idMatch ? idMatch[1] : '');
                    if (!href && !jobId) return null;
                    const key = jobId || href;
                    if (seen.has(key)) return null;
                    seen.add(key);

                    const rawLines = (card.innerText || '')
                        .split('\\n')
                        .map((line) => line.trim())
                        .filter(Boolean)
                        .filter((line) => !/^promoted$/i.test(line))
                        .filter((line) => !/^viewed$/i.test(line))
                        .filter((line) => !/^actively reviewing applicants$/i.test(line));

                    const title =
                        link?.getAttribute('aria-label')?.replace(/^View job: /i, '').trim()
                        || rawLines[0]
                        || '';
                    const company = rawLines.find((line, index) =>
                        index > 0
                        && !/easy apply|applied|viewed|promoted|ago|applicant/i.test(line)
                        && !/,|remote|hybrid|on-site|onsite|united states/i.test(line)
                    ) || rawLines[1] || '';
                    const location = rawLines.find((line) =>
                        /remote|hybrid|on-site|onsite|united states|, [A-Z]{2}\\b|metroplex|area/i.test(line)
                    ) || '';

                    return {
                        title,
                        company,
                        location,
                        work_type: '',
                        job_id: jobId,
                        url: href,
                        has_easy_apply: /easy apply/i.test(card.innerText || ''),
                    };
                }).filter((job) => job && job.title && job.company);
            }"""
        )
    except Exception as e:
        print(f"[extract] Browser-side extraction failed: {e}")
        jobs = []

    return jobs


async def extract_job_detail_from_panel(page: Page, job_card_index: int) -> dict:
    """
    Click a job card by index to load its description in the right-side panel.
    Reads the description without navigating away from the search results page.
    """
    try:
        # Dismiss any overlay that might block the click
        await dismiss_overlay(page)

        cards = await page.query_selector_all(
            "li.jobs-search-results__list-item, div.job-search-card"
        )
        if job_card_index >= len(cards):
            return {"description": "", "work_type": ""}

        card = cards[job_card_index]
        await card.scroll_into_view_if_needed()
        await card.click()
        await page.wait_for_timeout(2000)

        # Check if LinkedIn threw a challenge after the click
        if not await handle_challenge(page):
            return {"description": "", "work_type": ""}

        # Description lives in the detail panel on the right side of the search page
        desc_el = await page.query_selector(
            "div.jobs-description__content, "
            "div.jobs-description-content__text, "
            "div.description__text, "
            "article.jobs-description"
        )
        description = (await desc_el.inner_text()).strip() if desc_el else ""

        # Work type from criteria list
        criteria_els = await page.query_selector_all(
            "span.job-criteria__text, li.job-criteria__item span"
        )
        work_type = ""
        for el in criteria_els:
            text = (await el.inner_text()).strip().lower()
            if any(x in text for x in ("hybrid", "remote", "on-site", "onsite")):
                work_type = text
                break

        return {"description": description, "work_type": work_type}
    except Exception as e:
        print(f"[detail] Panel extraction failed (card {job_card_index}): {e}")
        return {"description": "", "work_type": ""}


# ---------------------------------------------------------------------------
# Resume upload to LinkedIn saved resumes
# ---------------------------------------------------------------------------

async def upload_resume_to_linkedin(page: Page, resume_path: str) -> bool:
    """
    Navigate to LinkedIn resume settings and upload the tailored resume.
    Returns True on success.
    """
    try:
        await page.goto(
            "https://www.linkedin.com/jobs/application-settings/",
            wait_until="domcontentloaded",
            timeout=15_000,
        )
        await page.wait_for_timeout(2000)

        # Find resume upload input
        upload_input = await page.query_selector("input[type='file'][accept*='pdf'], input[type='file'][accept*='doc']")
        if not upload_input:
            print("[upload] Could not find resume upload input.")
            return False

        await upload_input.set_input_files(resume_path)
        await page.wait_for_timeout(3000)
        print(f"[upload] Uploaded: {resume_path}")
        return True
    except Exception as e:
        print(f"[upload] Error uploading resume: {e}")
        return False


# ---------------------------------------------------------------------------
# Easy Apply modal handler
# ---------------------------------------------------------------------------

async def _answer_text_question(page: Page, label_text: str, answer: str) -> None:
    """Find a labeled text input and fill it."""
    label_text_lower = label_text.lower()
    inputs = await page.query_selector_all("input[type='text'], input[type='tel'], input[type='email'], input[type='number']")
    for inp in inputs:
        try:
            # Walk up to the form group and check label
            label = await page.evaluate(
                """(el) => {
                    const id = el.id;
                    if (id) {
                        const lbl = document.querySelector('label[for="' + id + '"]');
                        return lbl ? lbl.innerText : '';
                    }
                    return '';
                }""",
                inp,
            )
            if label and label_text_lower in label.lower():
                await inp.triple_click()
                await inp.fill(answer)
                return
        except Exception:
            continue


async def _answer_select_question(page: Page, label_text: str, answer: str) -> None:
    """Find a labeled <select> and choose the best matching option."""
    selects = await page.query_selector_all("select")
    for sel in selects:
        try:
            label = await page.evaluate(
                """(el) => {
                    const id = el.id;
                    if (id) {
                        const lbl = document.querySelector('label[for="' + id + '"]');
                        return lbl ? lbl.innerText : '';
                    }
                    return '';
                }""",
                sel,
            )
            if label and label_text.lower() in label.lower():
                options = await sel.query_selector_all("option")
                for opt in options:
                    opt_text = (await opt.inner_text()).strip().lower()
                    if answer.lower() in opt_text:
                        val = await opt.get_attribute("value")
                        await sel.select_option(value=val)
                        return
        except Exception:
            continue


async def _handle_modal_step(page: Page) -> str:
    """
    Handle one step of the Easy Apply modal.
    Fills known question patterns from PROFILE, then clicks Next or Submit.
    Returns 'next', 'submit', or 'done'.
    """
    await page.wait_for_timeout(1000)

    # --- Phone ---
    await _answer_text_question(page, "phone", PROFILE["phone"])
    await _answer_text_question(page, "mobile", PROFILE["phone"])

    # --- Work authorization ---
    await _answer_select_question(page, "authorized", "yes")
    await _answer_select_question(page, "legally authorized", "yes")
    await _answer_select_question(page, "work authorization", "yes")

    # --- Sponsorship ---
    await _answer_select_question(page, "sponsorship", "no")
    await _answer_select_question(page, "require sponsorship", "no")
    await _answer_select_question(page, "visa sponsorship", "no")

    # --- Education level ---
    await _answer_select_question(page, "education", "bachelor")
    await _answer_select_question(page, "highest degree", "bachelor")
    await _answer_select_question(page, "degree", "bachelor")

    # --- GPA ---
    await _answer_text_question(page, "gpa", PROFILE["gpa"])

    # --- Years of experience ---
    await _answer_text_question(page, "years of experience", str(PROFILE["years_experience"]))
    await _answer_text_question(page, "years experience", str(PROFILE["years_experience"]))

    # --- Yes/No radio buttons (authorized, etc.) ---
    radios = await page.query_selector_all("input[type='radio']")
    for radio in radios:
        try:
            val = (await radio.get_attribute("value") or "").lower()
            label_for = await radio.get_attribute("id")
            label_el = await page.query_selector(f"label[for='{label_for}']") if label_for else None
            label_text = (await label_el.inner_text()).strip().lower() if label_el else ""

            # For authorization-related yes/no questions, pick "yes"
            if val == "yes" and any(x in label_text for x in ("authorized", "eligible", "legally")):
                await radio.check()
            # For sponsorship questions, pick "no"
            if val == "no" and "sponsor" in label_text:
                await radio.check()
        except Exception:
            continue

    # Determine which button to click
    submit_btn = await page.query_selector("button[aria-label='Submit application']")
    if submit_btn:
        await submit_btn.click()
        return "submit"

    next_btn = await page.query_selector(
        "button[aria-label='Continue to next step'], "
        "button[aria-label='Review your application'], "
        "button[aria-label='Next']"
    )
    if next_btn:
        await next_btn.click()
        return "next"

    # Modal might have closed
    return "done"


async def easy_apply(page: Page, job: dict) -> bool:
    """
    Click Easy Apply on the job listing page and work through the full modal.
    Returns True if application was submitted successfully.
    """
    try:
        # Navigate to job page
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        # Click Easy Apply button
        easy_apply_btn = await page.query_selector(
            "button.jobs-apply-button[aria-label*='Easy Apply'], "
            "button[aria-label*='Easy Apply']"
        )
        if not easy_apply_btn:
            print(f"[apply] No Easy Apply button found for: {job['title']} @ {job['company']}")
            return False

        await easy_apply_btn.click()
        await page.wait_for_timeout(2000)

        submitted = False

        # Work through modal steps (up to 10 steps max)
        for step_num in range(10):
            result = await _handle_modal_step(page)
            print(f"[apply] Step {step_num + 1}: {result}")
            if result == "submit":
                submitted = True
                break
            if result == "done":
                break
            await page.wait_for_timeout(1500)

        # Check for success/confirmation message. Do not assume a closed modal
        # means the application was submitted; LinkedIn can close or fail before
        # the final submit step.
        await page.wait_for_timeout(2000)
        body_text = (await page.inner_text("body")).lower()
        success_markers = (
            "application submitted",
            "your application was sent",
            "application was sent",
            "you applied",
        )
        if any(marker in body_text for marker in success_markers):
            print(f"[apply] ✓ Applied to {job['title']} @ {job['company']}")
            return True

        applied_button = await page.query_selector(
            "button[aria-label*='Applied'], "
            "button:has-text('Applied')"
        )
        if submitted and applied_button:
            print(f"[apply] ✓ Applied state detected for {job['title']} @ {job['company']}")
            return True

        if not submitted:
            print(f"[apply] No submit confirmation for {job['title']} @ {job['company']}")
        else:
            print(f"[apply] Submit clicked but no confirmation detected for {job['title']} @ {job['company']}")
        return False

    except Exception as e:
        print(f"[apply] Error during Easy Apply for {job['title']}: {e}")
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Application log
# ---------------------------------------------------------------------------

def _load_log() -> list[dict]:
    return load_json_list(LOG_FILE)


def _save_log(entries: list[dict]) -> None:
    save_json_atomic(LOG_FILE, entries)


def _already_applied(log: list[dict], job_id: str) -> bool:
    return any(e.get("job_id") == job_id for e in log)


def _already_logged(log: list[dict], job: dict) -> bool:
    return already_seen(log, job)


def _title_company_key(job: dict) -> tuple[str, str]:
    return (
        (job.get("title") or "").strip().lower(),
        (job.get("company") or "").strip().lower(),
    )


def _log_application(job: dict, status: str, score: float, resume_path: str) -> None:
    log = _load_log()
    log.append({
        "job_id": job.get("job_id", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "score": score,
        "status": status,                  # "applied" | "skipped" | "error"
        "resume": resume_path,
        "applied_at": datetime.utcnow().isoformat() + "Z",
        "follow_up_sent": False,
        "source": "linkedin",
    })
    _save_log(log)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run() -> None:
    log = _load_log()
    applied_count = 0
    seen_title_company = {
        _title_company_key(entry)
        for entry in log
        if entry.get("title") and entry.get("company")
    }

    # Use a dedicated persistent profile for automation (avoids conflict with running Chrome)
    user_data_dir = str(BASE_DIR / "chrome_profile")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            slow_mo=80,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = await context.new_page()

        # --- Auth ---
        await load_cookies(context)
        await ensure_logged_in(page, context)

        # --- Search each URL ---
        for search_url in SEARCH_URLS:
            if applied_count >= MAX_APPLICATIONS:
                break

            print(f"\n[search] {search_url[:80]}...")
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"[search] Failed to load search URL: {e}")
                continue

            # Handle any challenge that appeared after navigating
            if not await handle_challenge(page):
                print("[search] Could not resolve challenge. Skipping URL.")
                continue

            # Debug: find actual job card HTML structure
            print(f"[debug] Current URL: {page.url[:100]}")
            try:
                title = await page.title()
                print(f"[debug] Page title: {title}")
                snippet = await page.evaluate("""() => {
                    const link = document.querySelector('a[href*="/jobs/view/"]');
                    if (!link) return 'NO JOB LINK FOUND';
                    const li = link.closest('li');
                    const outerHtml = (li || link).outerHTML.substring(0, 600);
                    return outerHtml;
                }""")
                print(f"[debug] First job card HTML:\\n{snippet[:500]}")
            except Exception as e:
                print(f"[debug] Error: {e}")

            # Collect listings from first results page
            listings = await scroll_and_extract_jobs(page)
            print(f"[search] Found {len(listings)} raw listings")

            for card_index, job in enumerate(listings):
                if applied_count >= MAX_APPLICATIONS:
                    break

                # Skip already applied
                title_company_key = _title_company_key(job)
                if _already_logged(log, job) or title_company_key in seen_title_company:
                    print(f"[skip] Already applied: {job['title']} @ {job['company']}")
                    continue

                # Quick pre-filter (no description yet)
                if is_agency_role(job["company"]):
                    print(f"[filter] Agency: {job['company']}")
                    continue
                if is_contractor_role(job["title"], ""):
                    print(f"[filter] Contractor title: {job['title']}")
                    continue

                # Click card to load description in the right panel (no new page navigation)
                detail = await extract_job_detail_from_panel(page, card_index)
                job["description"] = detail["description"]
                job["work_type"] = detail.get("work_type", "")

                if is_fake_posting(job["title"], job["company"], job["description"]):
                    print(f"[filter] Fake posting: {job['title']} @ {job['company']}")
                    continue
                if not is_entry_level_role(job["title"], job["description"]):
                    print(f"[filter] Not entry/junior: {job['title']} @ {job['company']}")
                    _log_application(job, "skipped_not_entry_level", 0.0, "")
                    seen_title_company.add(title_company_key)
                    continue

                score = score_job(
                    job["title"], job["company"], job["location"],
                    job["description"], job["work_type"]
                )
                print(f"[score] {score:.1f} — {job['title']} @ {job['company']}")

                if score < SCORE_THRESHOLD:
                    _log_application(job, "skipped", score, "")
                    seen_title_company.add(title_company_key)
                    continue

                # --- Tailor resume ---
                safe_company = re.sub(r"[^\w]", "_", job["company"])[:30]
                safe_title = re.sub(r"[^\w]", "_", job["title"])[:30]
                resume_filename = f"resume_{safe_title}_{safe_company}.docx"
                resume_path = str(RESUMES_DIR / resume_filename)

                try:
                    tailor_resume(job["title"], job["company"], job["description"], resume_path)
                    print(f"[resume] Tailored resume saved: {resume_filename}")
                except Exception as e:
                    print(f"[resume] Tailoring failed: {e}")
                    resume_path = ""

                # --- Upload resume ---
                if resume_path:
                    await upload_resume_to_linkedin(page, resume_path)
                    # Navigate back to job page after upload
                    await page.goto(job["url"], wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(1500)

                # --- Easy Apply ---
                if job.get("has_easy_apply"):
                    success = await easy_apply(page, job)
                    status = "applied" if success else "error"
                else:
                    print(f"[apply] No Easy Apply — skipping: {job['title']} @ {job['company']}")
                    status = "skipped_no_easy_apply"

                _log_application(job, status, score, resume_path)
                seen_title_company.add(title_company_key)
                if status == "applied":
                    applied_count += 1
                    print(f"[progress] {applied_count}/{MAX_APPLICATIONS} applications submitted")

                await save_cookies(context)
                await page.wait_for_timeout(3000)   # polite delay between applications

        await context.close()

    print(f"\n[done] Applied to {applied_count} jobs. Log: {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(run())
