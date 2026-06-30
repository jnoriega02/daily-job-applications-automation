---
name: daily-job-applications
description: Run a personalized daily job search, resume tailoring, application, and follow-up workflow
---

You are running a daily job application task for the candidate configured in this repository.

Before running:

- Confirm the candidate has replaced all placeholder profile details.
- Confirm LinkedIn resumes have been uploaded.
- Confirm `run_daily.cmd --check` passes.
- Do not expose cookies, logs, browser profiles, or resume files.

## Daily Workflow

1. Run:

```powershell
run_daily.cmd --check
```

2. If the health check passes, run:

```powershell
run_daily.cmd
```

3. Watch for LinkedIn login, CAPTCHA, checkpoint, or upload issues.

4. Report:

- follow-ups sent
- jobs applied to
- jobs queued for manual review
- skipped jobs and reasons
- errors or login issues
- run-log status

## Setup Requirements

Use the instructions in `README.md` and `SETUP_FOR_OTHERS.md`.

The automation must run locally in the configured workspace folder.
