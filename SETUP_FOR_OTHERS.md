# Personal Daily Job Application Automation Setup

This guide is for setting up your own copy of the daily job application automation on a Windows PC with your own LinkedIn account, resumes, profile details, and job preferences.

## 1. Install Required Apps

Install these first:

- Codex desktop app
- Google Chrome
- Python 3.11 or Miniconda
- Microsoft Word or another app that can open `.docx` files

Recommended folder location:

```powershell
C:\Dev\Scheduled\daily-job-applications
```

If you use a different folder, update every path in the scripts and automation prompt.

## 2. Enable Codex Settings And Tools

In Codex, make sure these are enabled:

- Local workspace access for the folder containing the automation
- Automations / scheduled tasks
- Chrome browser plugin or connector
- Computer-use plugin if you want Codex to help with native Windows file pickers
- Network access approval when installing dependencies

In Chrome, install and enable the Codex Chrome Extension.

Then open:

```text
chrome://extensions
```

Find the Codex Chrome Extension, click **Details**, and enable:

```text
Allow access to file URLs
```

This is required so Codex can upload local resume files into LinkedIn.

## 3. Install Python Dependencies

Open PowerShell in the automation folder:

```powershell
cd C:\Dev\Scheduled\daily-job-applications
```

Install dependencies:

```powershell
python -m pip install playwright python-docx
python -m playwright install chromium
```

If your PC has multiple Python installs, use the full Python path in `run_daily.cmd`.

Example:

```cmd
"C:\Users\YOUR_USER\miniconda3\python.exe" "%~dp0run_daily.py" %*
```

## 4. Personalize The Candidate Profile

Edit these files:

- `linkedin_jobs.py`
- `google_jobs.py`
- `follow_up.py`
- `resume_tailor.py`
- `filters.py`

Replace the sample person's information with your own:

- Name
- Email
- Phone number
- City/location
- LinkedIn profile URL
- Work authorization status
- Sponsorship answer
- Education
- GPA, if you want to include it
- Years of experience
- Skills
- Work history
- Projects
- Target job titles
- Locations
- Companies or agencies to avoid

Important places to check:

```python
PROFILE = {...}
CONTACT = {...}
EXPERIENCE = [...]
PROJECTS = [...]
SEARCH_URLS = [...]
SEARCH_QUERIES = [...]
AGENCY_BLACKLIST = {...}
```

## 5. Create Your Resume Variants

Create resume files for the role types you want to apply to. The current automation expects these names:

```text
Resume_SoftwareEngineer.docx
Resume_DataEngineer.docx
Resume_MLEngineer.docx
Resume_SystemsEngineer.docx
Resume_FinTech.docx
Resume_TPM_Solutions.docx
Your_Name_Resume_Updated.docx
```

Put them here:

```powershell
C:\Dev\Scheduled\daily-job-applications\tailored_resumes
```

You can also generate them from `resume_tailor.py`, but review them manually before using them.

## 6. Upload Resumes To LinkedIn

Open Chrome and sign in to LinkedIn.

Go to:

```text
https://www.linkedin.com/jobs/application-settings/
```

Upload each resume variant.

After upload, click **Show more resumes** and verify all expected resume files are visible.

If Codex is doing the upload for you, make sure Chrome has:

```text
Allow access to file URLs
```

enabled for the Codex Chrome Extension.

## 7. First LinkedIn Login For The Automation Profile

Run:

```powershell
C:\Dev\Scheduled\daily-job-applications\run_daily.cmd --check
```

Then run LinkedIn setup once:

```powershell
python linkedin_jobs.py
```

A browser window may open. Sign in manually if LinkedIn asks. If LinkedIn shows a security checkpoint or CAPTCHA, complete it yourself.

The automation saves session data in:

```text
linkedin_cookies.json
chrome_profile\
```

Do not share those files with anyone.

## 8. Test Without Applying

Run the health check:

```powershell
C:\Dev\Scheduled\daily-job-applications\run_daily.cmd --check
```

Expected result:

```text
Health check passed.
```

Do not run the full daily job until your profile details, resumes, and filters are reviewed.

## 9. Create The Codex Daily Automation

In Codex, ask:

```text
Create a daily local automation named "Daily Job Applications" at 7:00 AM.

Workspace:
C:\Dev\Scheduled\daily-job-applications

Task:
Run C:\Dev\Scheduled\daily-job-applications\run_daily.cmd --check first.
If the health check fails, stop and report the missing prerequisite or invalid file.
If it passes, run C:\Dev\Scheduled\daily-job-applications\run_daily.cmd.
Watch for LinkedIn login, CAPTCHA, or security challenge issues and report them clearly.
After the run, summarize follow-ups sent, jobs applied to, jobs queued or skipped, any errors, and the run-log.json status.

Use a local execution environment.
Keep the automation active.
```

Recommended Codex automation settings:

- Kind: cron / scheduled automation
- Time: daily at 7:00 AM local time
- Environment: local
- Workspace: `C:\Dev\Scheduled\daily-job-applications`
- Reasoning effort: medium
- Status: active

## 10. Daily Review

After each run, review:

```text
applications-log.json
run-log.json
google_jobs_queue.json
tailored_resumes\
```

Check for:

- Jobs actually applied to
- Jobs skipped
- Jobs queued for manual review
- Resume files created
- LinkedIn login or CAPTCHA errors
- Duplicate applications

## 11. Common Problems

### Codex cannot upload files to LinkedIn

Enable this in Chrome:

```text
Codex Chrome Extension > Details > Allow access to file URLs
```

### LinkedIn logs out or blocks automation

Open LinkedIn manually in Chrome, sign in, complete any checkpoint, then rerun:

```powershell
python linkedin_jobs.py
```

### Health check fails

Install missing packages:

```powershell
python -m pip install playwright python-docx
python -m playwright install chromium
```

### Wrong Python is being used

Edit `run_daily.cmd` and point it to the correct Python executable.

### Resumes are not selected correctly

Make sure the resume names in LinkedIn match the names used in the automation instructions and scripts.

## 12. Safety Notes

Before turning on the daily schedule:

- Review all personal data in the scripts.
- Review every resume variant.
- Confirm job filters match your actual goals.
- Keep maximum applications per day at a number you are comfortable with.
- Do not share cookies, browser profile folders, or application logs.
- Watch the first few runs manually.
