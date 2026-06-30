"""
google_jobs.py — Google Jobs search + queue builder.

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

from filters import is_contractor_role, is_agency_role, is_entry_level_role, is_fake_posting, score_job
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
OPEN_DIRECT_APPLY_LINKS = True
AUTO_DIRECT_APPLY = True

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
        "?udm=8&q=software+engineer+OR+data+engineer+hybrid+OR+onsite+your+city"
        "&tbs=qdr:d"
    ),
    # Local ML/data/junior, last 24h
    (
        "https://www.google.com/search"
        "?udm=8&q=data+engineer+OR+ml+engineer+junior+your+city+jobs"
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


def _find_queued(queue: list[dict], title: str, company: str) -> dict | None:
    for entry in queue:
        if entry.get("title") == title and entry.get("company") == company:
            return entry
    return None


def _is_ats_url(url: str) -> bool:
    """Return True if the apply URL points to a known ATS requiring an account."""
    url_lower = (url or "").lower()
    return any(ats in url_lower for ats in ATS_BLOCKLIST)


def _filter_apply_urls_for_job(urls: list[str], title: str, company: str) -> list[str]:
    distinctive_words = {
        word
        for word in re.findall(r"[a-z0-9]+", f"{title} {company}".lower())
        if len(word) >= 3 and word not in {"engineer", "developer", "software", "data", "the", "and"}
    }
    if not distinctive_words:
        return urls

    matched = []
    for url in urls:
        url_words = set(re.findall(r"[a-z0-9]+", url.lower()))
        if len(distinctive_words & url_words) >= min(2, len(distinctive_words)):
            matched.append(url)
    return matched or urls


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

    if "google.com/sorry" in page.url:
        print("[google] Google anti-bot page detected. Skipping this query.")
        return jobs

    # Current Google Jobs vertical cards. Each title div lives inside a
    # clickable card whose text contains title/company/location/source/date.
    try:
        modern_jobs = await page.evaluate(
            """() => {
                const titleEls = [...document.querySelectorAll('div.tNxQIb')];
                return titleEls.map((titleEl) => {
                    let card = titleEl;
                    for (let i = 0; i < 5 && card.parentElement; i += 1) {
                        if (card.getAttribute('role') === 'button' && card.innerText.includes(titleEl.innerText)) {
                            break;
                        }
                        card = card.parentElement;
                    }
                    const lines = (card.innerText || '')
                        .split('\\n')
                        .map((line) => line.trim())
                        .filter(Boolean);
                    const title = titleEl.innerText.trim() || lines[0] || '';
                    const titleIndex = lines.findIndex((line) => line === title);
                    const company = lines[titleIndex + 1] || lines.find((line) =>
                        line !== title
                        && !/ via |remote|hybrid|on-site|onsite|, [A-Z]{2}\\b/i.test(line)
                        && !/hour|day|week|month|ago/i.test(line)
                        && !/full-time|part-time|contractor|contract|no degree/i.test(line)
                        && line.length > 1
                    ) || '';
                    const locationLine = lines.find((line) => / via |remote|hybrid|on-site|onsite|, [A-Z]{2}\\b/i.test(line)) || '';
                    const location = locationLine.replace(/\\s+•\\s+via\\s+.*$/i, '').trim();
                    const date = lines.find((line) => /hour|day|week|month|ago/i.test(line)) || '';
                    const description = lines.join(' ');
                    const sourceMatch = locationLine.match(/via\\s+(.+)$/i);
                    const source = sourceMatch ? sourceMatch[1].trim() : '';
                    const localOnsite = /,\\s*[A-Z]{2}\\b|your city/i.test(location);
                    return {
                        card_index: titleEls.indexOf(titleEl),
                        title,
                        company,
                        location,
                        date,
                        description,
                        apply_urls: [],
                        work_type: /remote/i.test(description) ? 'remote'
                            : /hybrid/i.test(description) ? 'hybrid'
                            : /on-site|onsite/i.test(description) ? 'onsite'
                            : localOnsite ? 'onsite'
                            : '',
                        source,
                    };
                }).filter((job) => job.title && job.company);
            }"""
        )
        if modern_jobs:
            title_cards = page.locator("div.tNxQIb")
            for job in modern_jobs:
                try:
                    card_index = job.pop("card_index", None)
                    if card_index is None:
                        continue
                    await title_cards.nth(card_index).click()
                    await page.wait_for_timeout(1200)
                    detail = await page.evaluate(
                        """(source) => {
                            const applyLinks = [...document.querySelectorAll('a[href]')]
                                .map((a) => ({
                                    text: (a.innerText || '').trim(),
                                    href: a.href,
                                }))
                                .filter((link) => /^apply/i.test(link.text));
                            const preferred = applyLinks.filter((link) =>
                                !source || link.text.toLowerCase().includes(source.toLowerCase())
                            );
                            return {
                                applyUrls: (preferred.length ? preferred : applyLinks)
                                    .map((link) => link.href)
                                    .filter((href, index, arr) => href && arr.indexOf(href) === index),
                                detailText: document.body.innerText,
                            };
                        }""",
                        job.get("source", ""),
                    )
                    job["apply_urls"] = _filter_apply_urls_for_job(
                        detail.get("applyUrls", [])[:5],
                        job["title"],
                        job["company"],
                    )
                    detail_text = detail.get("detailText", "")
                    if job["title"] in detail_text:
                        start = detail_text.rfind(job["title"])
                        job["description"] = detail_text[start:start + 3000]
                except Exception as e:
                    print(f"[google] Could not enrich apply URL for {job.get('title', '')}: {e}")
            return modern_jobs
    except Exception as e:
        print(f"[google] Modern card extraction failed: {e}")

    # Older Google Jobs SERP widget fallback.
    cards = await page.query_selector_all("li.iFjolb, div[data-ved] div.BjJfJf")

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

async def attempt_direct_apply(page: Page, job: dict, resume_path: str, submit: bool = False) -> str:
    """
    For jobs with a non-ATS direct apply URL, open the apply page and fill what
    can be matched safely. Only submits the form when submit=True.
    """
    direct_urls = [u for u in job.get("apply_urls", []) if not _is_ats_url(u)]
    if not direct_urls:
        return "no_direct_url"

    apply_url = direct_urls[0]
    print(f"[direct_apply] Navigating to: {apply_url[:80]}")

    try:
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        page_text = (await page.locator("body").inner_text(timeout=5000)).lower()
        if re.search(r"\bjob\b.{0,80}\b(has|is)\s+expired\b", page_text):
            print(f"[direct_apply] Expired posting at {apply_url}")
            return "expired"

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

        if not submit:
            print(f"[direct_apply] Opened/prepared form for {job['title']} @ {job['company']} (not submitted)")
            return "manual_apply_ready"

        # Try clicking the submit button only when explicitly enabled.
        submit_handle = await page.evaluate_handle(
            """() => {
                const banned = /find jobs|search|subscribe|alert|sign in|log in/i;
                const positive = /apply|submit|send application|continue/i;
                const appField = /resume|cv|cover|phone|email|linkedin|first.?name|last.?name|full.?name|candidate|applicant/i;
                const controls = [...document.querySelectorAll("button, input[type='submit'], input[type='button'], a")];
                for (const control of controls) {
                    const text = (control.innerText || control.value || control.getAttribute("aria-label") || "").trim();
                    if (!text || banned.test(text) || !positive.test(text)) continue;
                    const form = control.closest("form");
                    const scope = form || control.closest("main, article, section, div") || document.body;
                    const scopeText = (scope.innerText || "") + " " + [...scope.querySelectorAll("input, textarea, select")]
                        .map((field) => [
                            field.name,
                            field.id,
                            field.placeholder,
                            field.getAttribute("aria-label"),
                            field.type,
                        ].filter(Boolean).join(" "))
                        .join(" ");
                    if (appField.test(scopeText)) return control;
                }
                return null;
            }"""
        )
        submit_btn = submit_handle.as_element()
        if submit_btn:
            before_url = page.url
            await submit_btn.click()
            await page.wait_for_timeout(2500)
            confirmation_text = (await page.locator("body").inner_text(timeout=5000)).lower()
            confirmed = any(
                phrase in confirmation_text
                for phrase in (
                    "application submitted",
                    "application received",
                    "thank you for applying",
                    "thanks for applying",
                    "your application has been submitted",
                    "we received your application",
                )
            )
            if confirmed:
                print(f"[direct_apply] Confirmed submission for {job['title']} @ {job['company']}")
                return "applied"
            print(f"[direct_apply] Clicked apply/submit but could not verify submission for {job['title']} @ {job['company']}")
            return "submission_unverified" if page.url != before_url else "no_confirmation"
        else:
            print(f"[direct_apply] No real application submit button found at {apply_url}")
            return "no_application_form"

    except Exception as e:
        print(f"[direct_apply] Error: {e}")
        traceback.print_exc()
        return "error"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    queue = _load_queue()
    log = _load_log()

    async with async_playwright() as pw:
        user_data_dir = str(BASE_DIR / "chrome_profile")
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            slow_mo=40,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
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
                if job.get("apply_urls") and not job.get("url"):
                    job["url"] = job["apply_urls"][0]
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
                if not is_entry_level_role(job["title"], job["description"]):
                    print(f"[filter] Not entry/junior: {job['title']} @ {job['company']}")
                    continue

                score = score_job(
                    job["title"], job["company"], job["location"],
                    job["description"], job["work_type"]
                )
                print(f"[score] {score:.1f} — {job['title']} @ {job['company']}")

                if score < SCORE_THRESHOLD:
                    continue

                # Add to queue for manual review, or refresh links/description if already queued.
                queue_entry = {**job, "score": score, "queued_at": datetime.utcnow().isoformat() + "Z"}
                existing_queue_entry = _find_queued(queue, job["title"], job["company"])
                if existing_queue_entry:
                    queued_at = existing_queue_entry.get("queued_at", queue_entry["queued_at"])
                    existing_queue_entry.update(queue_entry)
                    existing_queue_entry["queued_at"] = queued_at
                    _save_queue(queue)
                    print(f"[queue] Updated: {job['title']} @ {job['company']}")
                else:
                    queue.append(queue_entry)
                    _save_queue(queue)
                    print(f"[queue] Added: {job['title']} @ {job['company']}")

                # Open direct apply pages and prepare them; submit only when explicitly enabled.
                has_direct = any(not _is_ats_url(u) for u in job.get("apply_urls", []))
                if has_direct and (OPEN_DIRECT_APPLY_LINKS or AUTO_DIRECT_APPLY):
                    safe_company = re.sub(r"[^\w]", "_", job["company"])[:30]
                    safe_title = re.sub(r"[^\w]", "_", job["title"])[:30]
                    resume_path = str(RESUMES_DIR / f"resume_{safe_title}_{safe_company}.docx")
                    try:
                        tailor_resume(job["title"], job["company"], job["description"], resume_path)
                    except Exception as e:
                        print(f"[resume] Failed: {e}")
                        resume_path = ""

                    if resume_path:
                        apply_status = await attempt_direct_apply(
                            page,
                            job,
                            resume_path,
                            submit=AUTO_DIRECT_APPLY,
                        )
                        log.append({
                            "title": job["title"],
                            "company": job["company"],
                            "location": job["location"],
                            "url": job.get("apply_urls", [""])[0],
                            "score": score,
                            "status": apply_status,
                            "resume": resume_path,
                            "applied_at": datetime.utcnow().isoformat() + "Z",
                            "follow_up_sent": False,
                            "source": "google",
                        })
                        _save_log(log)
                        await page.wait_for_timeout(3000)

        await context.close()

    print(f"\n[done] Queue saved to {QUEUE_FILE} ({len(queue)} entries)")


if __name__ == "__main__":
    asyncio.run(run())
