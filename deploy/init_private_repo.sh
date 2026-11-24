#!/usr/bin/env bash
# Create the local folder for the PRIVATE data repo used by the daily cloud run.
#   deploy/init_private_repo.sh ~/Documents/job-agent-runner
# Copies: the database (safe online copy), Gemini/resume caches, resume PDFs, the workflow and a .gitignore.
# It only creates a local git repo and commit — pushing is a separate step (see docs/DAILY_CLOUD_RUN.md).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:?usage: $0 /path/to/job-agent-runner}"
py="$here/.venv/bin/python"; [ -x "$py" ] || py=python3

if [ -e "$target/data/jobs.db" ]; then
  echo "✖ $target/data/jobs.db already exists — not overwriting." >&2; exit 1
fi
mkdir -p "$target/data/cache" "$target/resumes" "$target/.github/workflows"
cp "$here/deploy/github-actions/daily.yml" "$target/.github/workflows/daily.yml"
cp "$here/deploy/github-actions/private-repo.gitignore" "$target/.gitignore"
if [ -f "$here/data/jobs.db" ]; then
  # sqlite backup API: a consistent copy even while the dashboard is running
  "$py" -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.execute("PRAGMA journal_mode=DELETE"); d.close()' \
    "$here/data/jobs.db" "$target/data/jobs.db"
fi
cp "$here"/data/cache/*.json "$target/data/cache/" 2>/dev/null || true
cp "$here"/resumes/*.pdf "$target/resumes/"
cd "$target"
[ -d .git ] || git init -q -b main
git config user.name "Debasish Mahapatra"
git config user.email "debasishmahapatra218@gmail.com"
git add -A
git commit -q -m "Initial data for the daily job agent" || true
echo "✔ Private data repo prepared in $target ($(ls resumes | wc -l) resumes, database $(du -h data/jobs.db 2>/dev/null | cut -f1 || echo none))"
echo "  Next: create an EMPTY private repo on GitHub and push this folder (docs/DAILY_CLOUD_RUN.md, step 4)."
