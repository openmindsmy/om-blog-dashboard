# OM Blog Dashboard — Cloud Refresh Handover

The dashboard at https://openmindsmy.github.io/om-blog-dashboard/ used to be refreshed
daily by a Claude task on a staff laptop. This replaces that with a **GitHub Actions
workflow inside the repo itself** — no laptop, no personal accounts. It runs every day
at **11:00 AM Kuala Lumpur** and commits the updated `index.html` directly.

## What goes where in the repo (`openmindsmy/om-blog-dashboard`)

```
.github/workflows/refresh.yml   ← the schedule + commit step
scripts/refresh.py              ← the whole data pipeline
index.html                      ← the dashboard (already there; gets rewritten daily)
```

Add the two new files to the repo (upload via GitHub web UI or git push).

## One-time setup (needs the `openmindsmy` account — repo admin)

### 1. Google service account (GA4 + Search Console)

1. Go to https://console.cloud.google.com → create (or reuse) a project.
2. **APIs & Services → Enable APIs**: enable **Google Analytics Data API** and
   **Google Search Console API**.
3. **IAM & Admin → Service Accounts → Create service account** (e.g. `om-dashboard`).
   No project roles needed. Create a **JSON key** and download it.
4. Grant it data access (use the service account's email, e.g.
   `om-dashboard@<project>.iam.gserviceaccount.com`):
   - **GA4**: Google Analytics → Admin → property *277421164* → Property Access
     Management → add the email as **Viewer**.
   - **Search Console**: https://search.google.com/search-console → property
     `https://www.openmindsresources.com/` → Settings → Users and permissions →
     add the email as **Full** user.

### 2. Repo secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value | Required |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the downloaded JSON key | yes |
| `SEMRUSH_API_KEY` | Semrush API key (Semrush → Profile → API) | recommended* |
| `ANTHROPIC_API_KEY` | Claude API key from console.anthropic.com | optional** |

\* Without it the AI-Visibility Semrush card keeps its last values and topic ideas
come from Search Console only.
\** Without it the analysis bullets and weekly-pick titles are auto-generated
templates instead of written prose. Everything else is identical.

### 3. First run + verify

1. Repo → **Actions** tab → enable workflows if prompted.
2. Open **Daily dashboard refresh** → **Run workflow** (manual trigger).
3. When green, check https://openmindsmy.github.io/om-blog-dashboard/ — the
   "Generated" date should be today and every tab should render.
4. The old laptop task can then be deleted (or simply ignored — the workflow's
   version always wins because it commits daily).

## How it works / maintenance notes

- **Schedule**: `cron: "0 3 * * *"` in `refresh.yml` is UTC → 11:00 KL. Edit there
  to change the time.
- **Weekly topic picks are Gap-only** (new topics, never refreshes of existing
  articles) and locked per ISO week — scores refresh daily, the 6 picks change on
  Monday.
- **Safety**: if GA4 auth fails or the HTML data markers are missing, the script
  exits with an error and **nothing is committed** — the live dashboard keeps
  yesterday's data. Failed runs email the repo owner (GitHub default).
- **Data window**: `DAILY_START` in `scripts/refresh.py` is `2026-01-01`. Bump it
  each January if you want the charts to restart at the new year.
- **Workflow auto-disable**: GitHub disables schedules after ~60 days of no repo
  activity. Daily commits keep it alive; if every run fails for 60 days it will
  stop — check the Actions tab if the Generated date looks stale.
- **Semrush cost**: ~25 API calls/day (domain overview ×2, organic ×1, keyword
  expansion ×20). Standard API units cover this easily.
- **Who owns what after handover**: everything runs under the `openmindsmy` org
  account + the secrets above. No personal Google/GitHub/Claude accounts involved.
