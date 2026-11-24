# Daily cloud run (GitHub Actions, free) — drafts in your Gmail

Every morning (08:00 IST) GitHub runs the agent for you, even with your laptop off:

1. checks which drafts from earlier runs you **sent** or **deleted** in Gmail,
2. searches jobs and recently funded startups, scores them, finds recruiter emails,
3. puts the best emails (up to your daily limit) into your **Gmail → Drafts** folder, resume attached,
4. emails **you** a short summary.

You open Gmail, read each draft, and press **Send** (or delete it). **Nothing is ever sent to a recruiter or
company automatically** — the only email the run sends is the summary to your own address. Deleted drafts are
never drafted again; drafts waiting in Gmail count against the daily limit and the 30-day duplicate check.

**Cost:** free. A run takes ~30–45 min; a private repo on GitHub Free includes 2,000 minutes/month (~1,350 used).
GitHub's default spending limit on the free plan is $0, so runs simply stop if minutes ever run out — no bill.

**Where things live**

| | Public code repo (`Automatic_job_apply`) | Private data repo (`job-agent-runner`) |
|---|---|---|
| Contents | the app code | `data/jobs.db`, caches, `resumes/*.pdf`, the workflow |
| Secrets | none | GitHub Actions secrets: Gemini key, Gmail login |

---

## One-time setup (~15 minutes)

### 1. Stop Gmail logins from expiring every 7 days
Google Cloud Console → **Google Auth Platform → Audience** (older UI: *APIs & Services → OAuth consent screen*)
→ Publishing status **Testing** → **Publish app** → Confirm.
You don't need Google's verification for personal use; you'll just see an *"unverified app"* warning when logging in.

### 2. Log in to Gmail again with the new permissions (laptop)
```bash
.venv/bin/python run.py --gmail-auth
```
On the warning page click **Advanced → Go to … (unsafe)**, then **tick all boxes**:
*Send email on your behalf*, *Manage drafts and send emails*, *View your email message metadata*.
(The run only reads labels — "was it sent?" — never message contents.) The dashboard keeps working as before.

### 3. Prepare the private repo folder (laptop)
Close the dashboard first, then:
```bash
deploy/init_private_repo.sh ~/Documents/job-agent-runner
```
This copies your database, caches, resumes and the workflow into that folder and makes the first commit.

### 4. Create the private repo on GitHub and push
1. <https://github.com/new> → name `job-agent-runner` → **Private** → no README → *Create repository*.
2. Give your laptop push access (a key for this repo only):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/job_agent_runner_deploy -N "" -C job-agent-runner
   cat ~/.ssh/job_agent_runner_deploy.pub
   ```
   Repo → **Settings → Deploy keys → Add deploy key** → Title `laptop`, paste the key, tick **Allow write
   access** → *Add key*.
3. Push:
   ```bash
   cd ~/Documents/job-agent-runner
   git remote add origin git@github.com:debasish218/job-agent-runner.git
   git config core.sshCommand "ssh -i ~/.ssh/job_agent_runner_deploy -o IdentitiesOnly=yes"
   git push -u origin main
   ```

### 5. Add the 3 secrets
Private repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | the key from your `.env` |
| `GMAIL_CREDENTIALS_JSON` | the whole contents of `credentials/credentials.json` |
| `GMAIL_TOKEN_JSON` | the whole contents of `credentials/token.json` (after step 2) |

Copy a file's contents without printing it: `xclip -selection clipboard < credentials/token.json`
(or open the file in a text editor and copy all). GitHub encrypts secrets and hides them in logs.

Optional, same page → **Variables** tab: `DAILY_EMAIL_LIMIT` (default 30), `DAILY_RUN_MINUTES` (default 35).

### 6. Test it
Private repo → **Actions → Daily job agent → Run workflow**. After ~40 min you should have the summary email and
new drafts in Gmail. From then on it runs every morning by itself (GitHub may start it a few minutes late).
The GitHub mobile app has the same *Run workflow* button.

### 7. Point the laptop dashboard at the same data
Add to the code repo's `.env` (adjust the path if you chose another folder):
```bash
DATA_DIR=/home/zadmin/Documents/job-agent-runner/data
RESUMES_DIR=/home/zadmin/Documents/job-agent-runner/resumes
DATA_REPO_SYNC=1
```
Now `run.py` pulls the cloud's latest database when the dashboard starts and pushes your changes when you stop it
(Ctrl+C). The old `data/jobs.db` in the code folder is no longer used (keep it as a backup).

---

## Daily routine
* Read the summary email → open **Gmail → Drafts** → review → **Send** or delete. That's it.
* Want details? Start the dashboard on the laptop: everything the cloud found is there.
* Drafts created by the cloud show *"In Gmail drafts"* in the dashboard — send or edit those in Gmail.

## Good to know / troubleshooting
* **Don't use the dashboard during the morning run (~08:00–08:45 IST).** If it's running, the dashboard opens
  **read-only** with a banner. If laptop and cloud both changed the database, it also opens read-only and the
  banner tells you which command keeps which copy (a database file can't be merged).
* **Rescue branch:** if the laptop pushed while a run was going, the run saves its data to a `rescue-<id>` branch
  instead of losing it. Tell Claude / merge it manually before the next run, otherwise that run's drafts aren't
  tracked.
* **"Gmail login lacks …" in the run log:** redo step 2 and update the `GMAIL_TOKEN_JSON` secret.
* **"Token has been expired or revoked":** step 1 wasn't done (Testing mode) — publish the app, redo step 2 and
  update the secret.
* **Search warnings:** GitHub's servers use data-centre IPs that some search engines block; each run gets a new IP.
  The summary lists such warnings. Gemini free-quota limits are reported the same way; the run continues next day.
* Run logs: private repo → **Actions** → pick a run. Only you can see them.
