# AI Job Search & Application Assistant

A manually-run, human-in-the-loop assistant: it discovers AI/ML jobs in India from official company job boards,
ranks them against your resume with Gemini, drafts personalised emails, and sends **only** what you `APPROVE`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # then put your GEMINI_API_KEY in .env
```

Resumes live in `resumes/` (all PDFs there are used; Gemini picks the best one per job).

## Run

```bash
.venv/bin/python run.py                  # interactive dashboard
.venv/bin/python run.py --check-sources  # validate every board in companies.yaml
.venv/bin/python run.py --gmail-auth     # one-time Gmail login
.venv/bin/python run.py --stats          # tracking DB summary
```

Startup: Gemini key check + live test (stops on failure, never switches provider) → resume analysis →
Gmail status → discovery passes over `companies.yaml` until you type `STOP`.

### Commands
| Command   | Effect |
|-----------|--------|
| `NEXT`    | Leave this job pending (shown again next run) and continue discovering |
| `DETAILS` | Full job description, experience snippet, verified company facts, contact sources |
| `APPROVE` | Send the displayed email (blocked if duplicate, over daily limit, or Gmail not ready) |
| `SKIP`    | Never show this job again |
| `EMAIL`   | Add a recruiter email you found yourself (recorded as *user provided*) and draft an email |
| `REGEN`   | Ask Gemini for a fresh draft |
| `STOP`    | Halt immediately (Ctrl+C works too). Everything is saved in `data/jobs.db` |

## Gmail setup (one time)
1. https://console.cloud.google.com → create/select a project → **APIs & Services → Library** → enable **Gmail API**.
2. **OAuth consent screen** → External → add your Gmail address as a **Test user**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app** → download JSON.
4. Save it as `credentials/credentials.json`, then run `.venv/bin/python run.py --gmail-auth` and log in.
   Scopes requested: `gmail.send` (send only, no inbox access) and your email address.

## Job sources & recruiter policy
* Public ATS APIs only: Greenhouse, Lever, Ashby, SmartRecruiters. Add companies in `companies.yaml`.
  LinkedIn / Naukri / Indeed are **not** scraped (their terms prohibit it).
* Recruiter emails are recorded only if they literally appear in the posting or on the public job page
  (`verified_on_page`, robots.txt respected) or you enter them (`user_provided`). Nothing is guessed.
* Filters: India locations; AI/ML titles; senior titles and postings whose minimum exceeds 2 years are dropped
  (reason shown). Exactly-2-year roles are kept and the gap is addressed honestly in the email.

## Layout
`job_agent/pipeline.py` holds all logic independent of the terminal UI (`ui.py`), so scheduling or a web
front-end can be added later without rewriting. Logs: `logs/job_agent.log`.

## Tests
```bash
.venv/bin/python -m pytest -m "not live"   # offline
.venv/bin/python -m pytest                 # includes live ATS API checks
```
