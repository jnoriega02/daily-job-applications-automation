"""
indeed_jobs.py - Indeed search + direct apply automation.

Flow:
  1. Search Indeed for recent local/remote junior listings.
  2. Filter roles through filters.py.
  3. Queue matching jobs in indeed_jobs_queue.json.
  4. For external direct apply links, try to fill and submit only real
     application forms, then log the verified result.

Run: python indeed_jobs.py
"""

import asyncio
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page

from filters import is_agency_role, is_contractor_role, is_entry_level_role, is_fake_posting, score_job
from job_utils import already_seen, load_json_list, save_json_atomic
from resume_tailor import tailor_resume


BASE_DIR = Path(__file__).parent
QUEUE_FILE = BASE_DIR / "indeed_jobs_queue.json"
LOG_FILE = BASE_DIR / "applications-log.json"
RESUMES_DIR = BASE_DIR / "tailored_resumes"
RESUMES_DIR.mkdir(exist_ok=True)

SCORE_THRESHOLD = 5.5
MAX_APPLICATIONS = int(os.getenv("INDEED_MAX_APPLICATIONS", "25"))
AUTO_DIRECT_APPLY = False
ENRICH_INDEED_DETAILS = os.getenv("INDEED_ENRICH_DETAILS", "0") == "1"

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
    "linkedin": "linkedin.com/in/your-profile",
    "years_experience": "0",
}

SEARCH_URLS = [
    (
        "https://www.indeed.com/jobs"
        "?q=junior+software+engineer"
        "&l=Your+City&fromage=1&sort=date"
    ),
    (
        "https://www.indeed.com/jobs"
        "?q=entry+level+software+engineer"
        "&l=Your+City&fromage=1&sort=date"
    ),
    (
        "https://www.indeed.com/jobs"
        "?q=junior+data+engineer"
        "&l=Your+City&fromage=1&sort=date"
    ),
    (
        "https://www.indeed.com/jobs"
        "?q=junior+software+engineer"
        "&l=Remote&fromage=1&sort=date"
    ),
    (
        "https://www.indeed.com/jobs"
        "?q=junior+data+engineer"
        "&l=Remote&fromage=1&sort=date"
    ),
]


def _load_queue() -> list[dict]:
    return load_json_list(QUEUE_FILE)


def _save_queue(entries: list[dict]) -> None:
    save_json_atomic(QUEUE_FILE, entries)


def _load_log() -> list[dict]:
    return load_json_list(LOG_FILE)


def _save_log(entries: list[dict]) -> None:
    save_json_atomic(LOG_FILE, entries)


def _find_queued(queue: list[dict], title: str, company: str) -> dict | None:
    for entry in queue:
        if entry.get("title") == title and entry.get("company") == company:
            return entry
    return None


def _is_indeed_url(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    return "indeed." in host


def _is_bad_indeed_job(job: dict) -> bool:
    url = job.get("url", "")
    description = (job.get("description") or "").lower()
    return (
        "jk=789abcdef0123456" in url
        or "we can\u2019t find this page" in description
        or "we can't find this page" in description
        or "page doesn't exist" in description
        or "additional verification required" in description
    )


def _is_target_role_family(job: dict) -> bool:
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    role_terms = (
        "software", "developer", "engineer", "data", "analytics", "analyst",
        "python", "sql", "java", "javascript", "typescript", "machine learning",
        "ml", "ai", "backend", "full stack", "frontend",
    )
    excluded_terms = (
        "server", "restaurant", "photographer", "drone pilot", "real estate",
        "special agent", "nurse", "sales associate", "cashier", "warehouse",
    )
    return any(term in text for term in role_terms) and not any(term in text for term in excluded_terms)


async def extract_indeed_jobs(page: Page) -> list[dict]:
    if "blocked" in page.url.lower() or "challenge" in page.url.lower() or "bot-detection" in page.url.lower():
        print("[indeed] Challenge/block page detected.")
        return []

    jobs = await page.evaluate(
        """() => {
            const cards = [...document.querySelectorAll('[data-jk], .job_seen_beacon, a[href*="/viewjob"]')];
            const seen = new Set();
            return cards.map((node) => {
                const card = node.closest('[data-jk], .job_seen_beacon, .result') || node;
                const jobKey = card.getAttribute('data-jk') || '';
                const link = card.querySelector('a[href*="/viewjob"], a[data-jk]') || (node.matches('a') ? node : null);
                const href = link ? new URL(link.getAttribute('href'), location.origin).href : '';
                const key = jobKey || href;
                if (!key || seen.has(key)) return null;
                seen.add(key);
                const titleEl = card.querySelector('[data-testid="jobTitle"], h2 a span, h2 span, a[href*="/viewjob"]');
                const companyEl = card.querySelector('[data-testid="company-name"], .companyName, [data-testid="companyName"]');
                const locationEl = card.querySelector('[data-testid="text-location"], .companyLocation');
                const metaText = (card.innerText || '').trim();
                return {
                    job_id: jobKey,
                    title: (titleEl?.innerText || titleEl?.getAttribute('title') || '').trim(),
                    company: (companyEl?.innerText || '').trim(),
                    location: (locationEl?.innerText || '').trim(),
                    url: href,
                    date: '',
                    description: metaText,
                    apply_urls: [],
                    work_type: /remote/i.test(metaText) ? 'remote'
                        : /hybrid/i.test(metaText) ? 'hybrid'
                        : /dallas|plano|irving|fort worth|frisco/i.test(metaText) ? 'onsite'
                        : '',
                    source: 'indeed',
                };
            }).filter((job) => job && job.title && job.company);
        }"""
    )

    if not ENRICH_INDEED_DETAILS:
        return jobs

    for job in jobs:
        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(1500)
            detail = await page.evaluate(
                """() => ({
                    text: document.body.innerText || '',
                    links: [...document.querySelectorAll('a[href], button')]
                        .map((el) => ({
                            text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
                            href: el.href || '',
                        }))
                        .filter((item) => /apply/i.test(item.text) || /apply/i.test(item.href))
                })"""
            )
            job["description"] = detail.get("text", "")[:5000]
            if _is_bad_indeed_job(job):
                print(f"[indeed] Ignoring invalid/degraded listing: {job.get('title', '')} @ {job.get('company', '')}")
                job["invalid"] = True
                continue
            urls = []
            for item in detail.get("links", []):
                href = item.get("href", "")
                if href.startswith("http") and href not in urls:
                    urls.append(href)
            job["apply_urls"] = urls[:5]
        except Exception as exc:
            print(f"[indeed] Could not enrich {job.get('title', '')}: {exc}")

    return [job for job in jobs if not job.get("invalid")]


async def handle_indeed_challenge(page: Page, timeout: int = 180_000) -> bool:
    page_text = ""
    try:
        page_text = (await page.locator("body").inner_text(timeout=5000)).lower()
    except Exception:
        pass

    challenge_detected = (
        "bot-detection" in page.url.lower()
        or "additional verification required" in page_text
        or "just a moment" in (await page.title()).lower()
    )
    if not challenge_detected:
        return True

    print("\n[indeed] Verification required.")
    print("[indeed] Please complete the Indeed verification or sign-in in the browser window.")
    print(f"[indeed] Waiting up to {timeout // 1000}s...\n")
    try:
        await page.wait_for_function(
            """() => {
                const text = (document.body.innerText || '').toLowerCase();
                return !location.href.includes('bot-detection')
                    && !text.includes('additional verification required')
                    && (
                        document.querySelectorAll('[data-jk], .job_seen_beacon, a[href*="/viewjob"]').length > 0
                        || location.href.includes('/jobs')
                    );
            }""",
            timeout=timeout,
        )
        await page.wait_for_timeout(2000)
        print("[indeed] Verification resolved. Continuing.")
        return True
    except Exception:
        print("[indeed] Verification was not resolved in time.")
        return False


async def attempt_direct_apply(page: Page, job: dict, resume_path: str) -> str:
    direct_urls = [url for url in job.get("apply_urls", []) if not _is_indeed_url(url)]
    if not direct_urls:
        return "queued_no_external_direct_apply"

    apply_url = direct_urls[0]
    print(f"[direct_apply] Navigating to: {apply_url[:90]}")
    try:
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=25_000)
        await page.wait_for_timeout(2500)
        body_text = (await page.locator("body").inner_text(timeout=5000)).lower()
        if re.search(r"\bjob\b.{0,80}\b(has|is)\s+expired\b", body_text):
            return "expired"

        field_map = {
            "first": PROFILE["first_name"],
            "last": PROFILE["last_name"],
            "full name": PROFILE["name"],
            "name": PROFILE["name"],
            "email": PROFILE["email"],
            "phone": PROFILE["phone"],
            "city": PROFILE["city"],
            "location": PROFILE["location"],
            "linkedin": PROFILE["linkedin"],
            "years": PROFILE["years_experience"],
        }
        inputs = await page.query_selector_all("input[type='text'], input[type='email'], input[type='tel'], input[type='number'], textarea")
        for inp in inputs:
            try:
                hint = " ".join(
                    filter(
                        None,
                        [
                            await inp.get_attribute("name"),
                            await inp.get_attribute("id"),
                            await inp.get_attribute("placeholder"),
                            await inp.get_attribute("aria-label"),
                        ],
                    )
                ).lower()
                for key, value in field_map.items():
                    if key in hint:
                        await inp.fill(str(value))
                        break
            except Exception:
                continue

        for file_input in await page.query_selector_all("input[type='file']"):
            try:
                await file_input.set_input_files(resume_path)
                break
            except Exception:
                continue

        submit_handle = await page.evaluate_handle(
            """() => {
                const banned = /search|subscribe|alert|sign in|log in/i;
                const positive = /apply|submit|send application|continue/i;
                const appField = /resume|cv|cover|phone|email|linkedin|first.?name|last.?name|candidate|applicant/i;
                for (const el of [...document.querySelectorAll("button, input[type='submit'], input[type='button']")]) {
                    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                    if (!text || el.disabled || banned.test(text) || !positive.test(text)) continue;
                    const scope = el.closest('form') || document.body;
                    const scopeText = (scope.innerText || '') + ' ' + [...scope.querySelectorAll('input, textarea, select')]
                        .map((field) => [field.name, field.id, field.placeholder, field.getAttribute('aria-label'), field.type].filter(Boolean).join(' '))
                        .join(' ');
                    if (appField.test(scopeText)) return el;
                }
                return null;
            }"""
        )
        submit_btn = submit_handle.as_element()
        if not submit_btn:
            return "no_application_form"
        if not AUTO_DIRECT_APPLY:
            return "manual_apply_ready"

        await submit_btn.click()
        await page.wait_for_timeout(3000)
        confirmation = (await page.locator("body").inner_text(timeout=5000)).lower()
        if any(
            phrase in confirmation
            for phrase in (
                "application submitted",
                "application received",
                "thank you for applying",
                "thanks for applying",
                "your application has been submitted",
            )
        ):
            return "applied"
        return "submission_unverified"
    except Exception as exc:
        print(f"[direct_apply] Error: {exc}")
        traceback.print_exc()
        return "error"


async def run() -> None:
    queue = _load_queue()
    log = _load_log()
    applied_count = 0

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(BASE_DIR / "chrome_profile"),
            headless=False,
            slow_mo=60,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = await context.new_page()

        for search_url in SEARCH_URLS:
            if applied_count >= MAX_APPLICATIONS:
                break
            print(f"\n[search] {search_url[:90]}...")
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=25_000)
                await page.wait_for_timeout(2500)
            except Exception as exc:
                print(f"[search] Failed: {exc}")
                continue
            if not await handle_indeed_challenge(page):
                continue

            jobs = await extract_indeed_jobs(page)
            print(f"[search] Extracted {len(jobs)} listings")

            for job in jobs:
                if applied_count >= MAX_APPLICATIONS:
                    break
                if _is_bad_indeed_job(job):
                    print(f"[filter] Invalid Indeed listing: {job['title']} @ {job['company']}")
                    continue
                if not _is_target_role_family(job):
                    print(f"[filter] Unrelated Indeed listing: {job['title']} @ {job['company']}")
                    continue
                if already_seen(log, job):
                    print(f"[skip] Already logged: {job['title']} @ {job['company']}")
                    continue
                if is_agency_role(job["company"]):
                    print(f"[filter] Agency: {job['company']}")
                    continue
                if is_contractor_role(job["title"], job["description"]):
                    print(f"[filter] Contractor: {job['title']}")
                    continue
                if is_fake_posting(job["title"], job["company"], job["description"]):
                    print(f"[filter] Fake: {job['title']} @ {job['company']}")
                    continue
                if not is_entry_level_role(job["title"], job["description"]):
                    print(f"[filter] Not entry/junior: {job['title']} @ {job['company']}")
                    log.append({**job, "score": 0.0, "status": "skipped_not_entry_level", "resume": "", "applied_at": datetime.utcnow().isoformat() + "Z", "follow_up_sent": False})
                    _save_log(log)
                    continue

                score = score_job(job["title"], job["company"], job["location"], job["description"], job["work_type"])
                print(f"[score] {score:.1f} - {job['title']} @ {job['company']}")
                if score < SCORE_THRESHOLD:
                    log.append({**job, "score": score, "status": "skipped", "resume": "", "applied_at": datetime.utcnow().isoformat() + "Z", "follow_up_sent": False})
                    _save_log(log)
                    continue

                queue_entry = {**job, "score": score, "queued_at": datetime.utcnow().isoformat() + "Z"}
                existing = _find_queued(queue, job["title"], job["company"])
                if existing:
                    queued_at = existing.get("queued_at", queue_entry["queued_at"])
                    existing.update(queue_entry)
                    existing["queued_at"] = queued_at
                else:
                    queue.append(queue_entry)
                _save_queue(queue)

                safe_company = re.sub(r"[^\w]", "_", job["company"])[:30]
                safe_title = re.sub(r"[^\w]", "_", job["title"])[:30]
                resume_path = str(RESUMES_DIR / f"resume_{safe_title}_{safe_company}.docx")
                try:
                    tailor_resume(job["title"], job["company"], job["description"], resume_path)
                except Exception as exc:
                    print(f"[resume] Failed: {exc}")
                    resume_path = ""

                status = "queued"
                if resume_path:
                    status = await attempt_direct_apply(page, job, resume_path)
                    if status == "applied":
                        applied_count += 1

                log.append({
                    **job,
                    "score": score,
                    "status": status,
                    "resume": resume_path,
                    "applied_at": datetime.utcnow().isoformat() + "Z",
                    "follow_up_sent": False,
                })
                _save_log(log)

        await context.close()

    print(f"\n[done] Applied to {applied_count} Indeed jobs. Queue: {QUEUE_FILE}")


if __name__ == "__main__":
    asyncio.run(run())
