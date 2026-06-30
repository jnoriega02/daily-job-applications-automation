# Daily Job Application Automation

Personal job-search automation for Windows using Python, Playwright, LinkedIn, Google Jobs, and Codex scheduled automations.

This repository is a template. Before running it, replace all placeholder candidate details, target locations, resume content, search URLs, and follow-up text with your own information.

## What It Does

- Checks yesterday's applications and attempts LinkedIn follow-ups.
- Searches LinkedIn for fresh jobs matching your target roles.
- Scores and filters jobs by location, skills, seniority, agency/contract status, and recency.
- Generates tailored `.docx` resumes.
- Attempts LinkedIn Easy Apply where possible.
- Searches Google Jobs and queues qualifying external listings for review.
- Writes run history and application records locally.

## Important Safety Note

This automation can submit job applications and upload resumes. Watch the first few runs manually and keep the daily application limit conservative.

Never commit or share:

- `linkedin_cookies.json`
- `chrome_profile/`
- `applications-log.json`
- `run-log.json`
- `google_jobs_queue.json`
- real resumes or generated application resumes

The included `.gitignore` blocks those files by default.

## Requirements

- Windows
- Python 3.11+ or Miniconda
- Google Chrome
- Codex desktop app
- Codex Chrome Extension

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Configure Your Profile

Edit these files before running:

- `linkedin_jobs.py`
- `google_jobs.py`
- `follow_up.py`
- `resume_tailor.py`
- `filters.py`

Replace placeholders such as:

- `Your Name`
- `you@example.com`
- `5551234567`
- `Your City, ST`
- `linkedin.com/in/your-profile`

Also update:

- target job titles
- target locations
- skills
- education
- work history
- projects
- agencies/companies to avoid
- resume selection rules

## Resume Setup

Create your own resume variants and upload them to LinkedIn:

- `Resume_SoftwareEngineer.docx`
- `Resume_DataEngineer.docx`
- `Resume_MLEngineer.docx`
- `Resume_SystemsEngineer.docx`
- `Resume_FinTech.docx`
- `Resume_TPM_Solutions.docx`
- `Your_Name_Resume_Updated.docx`

Store local copies in:

```powershell
tailored_resumes\
```

Do not commit actual resume files.

## LinkedIn Setup

1. Open Chrome and sign in to LinkedIn.
2. Go to `https://www.linkedin.com/jobs/application-settings/`.
3. Upload your resume variants.
4. Click **Show more resumes** and verify all expected names are visible.
5. Run the automation once manually so it can save a local LinkedIn session:

```powershell
python linkedin_jobs.py
```

If LinkedIn shows a checkpoint or CAPTCHA, complete it manually.

## Codex Setup

In Codex, enable:

- Local workspace access for this repo folder
- Automations / scheduled tasks
- Chrome plugin or connector
- Codex Chrome Extension in Chrome
- `Allow access to file URLs` for the Codex Chrome Extension
- Network approval when installing dependencies

Chrome file upload setting:

```text
chrome://extensions > Codex Chrome Extension > Details > Allow access to file URLs
```

## Health Check

Run this before the full job:

```powershell
run_daily.cmd --check
```

Expected:

```text
Health check passed.
```

## Daily Run

```powershell
run_daily.cmd
```

Steps:

1. `follow_up.py`
2. `linkedin_jobs.py`
3. `google_jobs.py`

Each step is isolated. If one fails, the next still runs and the failure is logged.

## Schedule In Codex

Ask Codex:

```text
Create a daily local automation named "Daily Job Applications" at 8:30 AM.

Workspace:
C:\Path\To\daily-job-applications

Task:
Run C:\Path\To\daily-job-applications\run_daily.cmd --check first.
If the health check fails, stop and report the missing prerequisite or invalid file.
If it passes, run C:\Path\To\daily-job-applications\run_daily.cmd.
Watch for LinkedIn login, CAPTCHA, or security challenge issues and report them clearly.
After the run, summarize follow-ups sent, jobs applied to, jobs queued or skipped, any errors, and the run-log.json status.

Use a local execution environment.
Keep the automation active.
```

Recommended automation settings:

- Kind: cron / scheduled automation
- Time: daily at 8:30 AM local time
- Environment: local
- Workspace: this repo folder
- Reasoning effort: medium
- Status: active

## Review Outputs

Review these after each run:

- `applications-log.json`
- `run-log.json`
- `google_jobs_queue.json`
- `tailored_resumes/`

## Troubleshooting

If uploads fail in Chrome, enable **Allow access to file URLs** for the Codex Chrome Extension.

If LinkedIn logs out, rerun:

```powershell
python linkedin_jobs.py
```

If dependencies are missing:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

If the wrong Python runs, edit `run_daily.cmd` and use a full path to your preferred Python executable.
