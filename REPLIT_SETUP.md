# SRV Timesheet Tool — Replit Setup

Everything you need to do to get this running on Replit, in order.

---

## STEP 0 — Rotate your credentials first

`run_dev.bat` contains a live Google OAuth client secret and a Gmail app
password in plain text. Before this code goes anywhere near a cloud host:

1. **Google OAuth secret** — Google Cloud Console → APIs & Services →
   Credentials → your OAuth client → **Add secret**, then delete the old one.
2. **Gmail app password** — Google Account → Security → App passwords →
   revoke `zvueulbsmeudznat`, generate a new one.

The new values go into Replit **Secrets**, never into a file.

---

## STEP 1 — Get the code onto Replit

You already have a GitHub repo (`ashish-srv/srv-timesheet-filler-v2`) and it has
never contained secrets — `sa.json`, `run_dev.bat` and `data/app.db` are all
gitignored and were never committed. Use it.

### 1a. Push the changes

Open Command Prompt in the project folder:

```
cd /d "D:\Updated ProofHub Tool 15-08-2026\SRV- timesheet filler - 30-07-2026"
git add -A
git status
```

`git status` should list only these — **if you see `sa.json`, `run_dev.bat` or
`app.db`, stop and tell me:**

```
modified:   Dockerfile
modified:   main.py
modified:   requirements.txt
modified:   src/db.py
modified:   src/emailer.py
modified:   src/roster_sync.py
modified:   templates/index.html
new file:   .replit
new file:   REPLIT_SETUP.md
new file:   TEST_PLAN.md
```

Then:

```
git commit -m "Postgres migration, rejection CSV email, invalid-row download, entry history limit, ProofHub key settings"
git push origin main
```

### 1b. Import into Replit

1. Replit → **Create App** → **Import from GitHub**
2. Authorise GitHub if asked, pick `srv-timesheet-filler-v2`
3. Replit reads `.replit` and configures itself

> **Alternative if you'd rather not use git:** zip the project folder — but first
> delete `venv`, `__pycache__`, `data`, `_backup_tierA_2026-08-05`, `sa.json` and
> `run_dev.bat` from the copy you zip. Then use Replit's "Upload folder" option.
> The GitHub route is better because future updates are one `git push`.

### 1c. Create the database

In the Replit sidebar: **Tools → Database → Create a PostgreSQL database.**

Replit sets `DATABASE_URL` automatically — you do not add that secret yourself.
The app creates its own tables on first startup.

---

## STEP 2 — Add the Secrets

Tools → Secrets. Add each of these:

| Secret | Value | Notes |
|---|---|---|
| `SESSION_SECRET` | a long random string | Generate one. If this changes, everyone is logged out. |
| `ADMIN_EMAILS` | `ashish.kate@srvmedia.com` | Comma-separated. Can sign in before the roster loads. |
| `COMPANY_NAME` | `srvmedia` | Your ProofHub subdomain. |
| `APP_BASE_URL` | `https://<your-app>.replit.app` | **No trailing slash.** Used in the "review" link in manager emails. |
| `GOOGLE_CLIENT_ID` | your client ID | |
| `GOOGLE_CLIENT_SECRET` | your **new** secret | From Step 0. |
| `GOOGLE_SA_JSON_CONTENT` | entire contents of `sa.json` | Open `sa.json`, copy **everything** including the braces, paste as one value. |
| `ROSTER_SHEET_ID` | `1j8H5hdQAvPOB5Tt4xXnfYdIKHIDnryjtjWbcw5gx-bM` | |
| `ROSTER_WORKSHEET` | *(leave unset)* | Blank = first tab. |
| `ROSTER_SYNC_HOURS` | `6` | `0` disables auto-sync. |
| `EMAIL_WEBHOOK_URL` | `https://n8n.srv.media/webhook/srv-timesheet-email` | |
| `EMAIL_FROM` | `internalcommunications@srvmedia.com` | |
| `APP_TIMEZONE` | `Asia/Kolkata` | **Important** — see note below. |
| `MY_ENTRIES_SUBMISSIONS` | `3` | How many recent submissions users see. |
| `ENTRY_RETENTION_DAYS` | `365` | Auto-deletes finished entries older than this. `0` = never. |

**Do NOT set `DEV_LOGIN`.** It creates an unauthenticated login endpoint. It
must never exist in production.

> **Why `APP_TIMEZONE` matters:** Replit servers run on UTC. Without this,
> every timestamp would be 5 hours 30 minutes behind IST, and entries submitted
> after 6:30 PM would show yesterday's date.

---

## STEP 3 — Update Google OAuth redirect URIs

Google Cloud Console → Credentials → your OAuth client → **Authorized redirect URIs.**

Add:

```
https://<your-app>.replit.app/auth/callback
```

Keep `http://localhost:8123/auth/callback` so local testing still works.

Also add `https://<your-app>.replit.app` under **Authorized JavaScript origins.**

> **The gotcha this fixes:** `auth.py` builds the redirect URI from the incoming
> request. Behind Replit's proxy, FastAPI sees plain HTTP and would generate an
> `http://` URL that Google rejects with `redirect_uri_mismatch`. The `.replit`
> file now starts uvicorn with `--proxy-headers --forwarded-allow-ips='*'`,
> which makes it read the real `https://` scheme from Replit's headers.

---

## STEP 4 — Share the sheet with the service account

Already done locally, but verify: open the HR Google Sheet → Share → add the
service account's `client_email` (found inside `sa.json`) with **Viewer** access.

---

## STEP 5 — Deploy on Reserved VM (not Autoscale)

Deploy → **Reserved VM.**

This is not optional for this app. Autoscale runs multiple instances that sleep
when idle, which breaks three things:

- **CSV preview → submit.** The validated rows live in an in-memory dict
  (`previews` in `main.py`). If submit lands on a different instance than
  preview did, the user gets "Preview expired."
- **Approve/push progress.** The `jobs` dict and its background thread are
  in-memory too — the progress bar would hang.
- **Roster auto-sync.** The 6-hourly background thread dies when an instance
  sleeps.

Reserved VM keeps one long-lived process, which is what this design assumes.

---

## STEP 6 — Configure n8n for HTML + attachment

Your workflow currently receives `to`, `subject`, `message`, `from`, `reply_to`.
The app now sends **three additional fields**:

| Field | Contains |
|---|---|
| `html` | Full HTML body (manager's name in `<strong>`) |
| `attachment_filename` | `rejected_entries.csv` |
| `attachment_mime` | `text/csv` |
| `attachment_base64` | The CSV, base64-encoded |

The last three are only present on **rejection** emails.

### 6a — Send the HTML body

In your **Send Email** node, set the email format to **HTML** and point the
HTML body at:

```
{{ $json.html }}
```

Keep `{{ $json.message }}` as the plain-text body if the node supports both
(it's the fallback for clients that can't render HTML).

### 6b — Attach the CSV

Add a **Convert to File → Convert base64 to File** node between your Webhook
and Send Email node:

- **Base64 Input Field:** `attachment_base64`
- **Put Output File in Field:** `data`
- **File Name:** `={{ $json.attachment_filename }}`
- **MIME Type:** `={{ $json.attachment_mime }}`

Then in the **Send Email** node, set **Attachments** to `data`.

### 6c — Handle emails with no attachment

Submission emails have no `attachment_base64`. Add an **IF** node checking
whether `attachment_base64` exists, and route:

- **true** → Convert-to-File → Send Email (with attachment)
- **false** → Send Email (no attachment)

Otherwise the convert node will error on every submission notification.

> **Test tip:** reject one entry and check the email. If you see literal
> `<strong>` tags in the message, step 6a isn't applied. If the mail arrives
> with no CSV, step 6b isn't applied.

---

## STEP 7 — Testing locally after these changes

The app no longer uses SQLite, so local runs need a database too. Easiest path:
point your local machine at the Replit Postgres instance.

1. In Replit, open the Database pane and copy the **external connection string.**
2. In `run_dev.bat`, replace the SQLite-era setup with:

```bat
set DATABASE_URL=postgresql://...paste the external URL here...
set APP_TIMEZONE=Asia/Kolkata
```

3. `pip install -r requirements.txt` (psycopg is new).

Everything else in `run_dev.bat` stays as it is.

> Local and Replit will now share one database. That's fine for testing, but be
> aware a local test submission is visible in production. If you'd rather keep
> them separate, create a second Postgres database in Replit for dev.

---

## STEP 8 — First-run checks on Replit

Since you're not testing locally, **this is your test.** Do it before telling
anyone else the URL.

### 8a. Does it boot?

Click **Run** and watch the console.

```
[roster] startup sync: {'rows_in_sheet': ..., 'upserted': ..., 'active': ...}
[roster] auto-sync enabled every 6.0h
Uvicorn running on http://0.0.0.0:5000
```

| Console says | Meaning | Fix |
|---|---|---|
| `DATABASE_URL is not set` | Database not created | Step 1c |
| `GOOGLE_SA_JSON_CONTENT is not valid JSON` | Partial paste | Re-copy the **entire** `sa.json`, braces included |
| `startup sync failed: ... 403` | Sheet not shared | Step 4 |
| `rows_in_sheet: 0` | Wrong sheet or tab | Check `ROSTER_SHEET_ID` |

Roster should be roughly **958 rows** — that's what your local database had.

### 8b. Google sign-in (most likely failure point)

Open your `.replit.app` URL and sign in.

**If you get `redirect_uri_mismatch`:** the URI in Step 3 doesn't exactly match.
Google's error page shows the URI it received — copy that exact string into the
Google Console. Watch for `http` vs `https` and a trailing slash.

If it received an `http://` URI, the proxy-header flag isn't applying — check
`.replit` came across intact.

### 8c. Timezone

Submit one test entry, then open **My Entries**. The submitted time must be
**IST, not UTC**. If it's 5h30m behind, `APP_TIMEZONE` is missing from Secrets.

### 8d. The rest

- [ ] Key card appears on first sign-in; paste your ProofHub key
- [ ] Reload — card is gone, **⚙ Settings** in navbar
- [ ] Add Time: project list loads, submit an entry
- [ ] `/manager`: the entry appears as pending
- [ ] Reject it → check the email (see 8e)
- [ ] Approve one → confirm it lands in ProofHub
- [ ] Bulk Upload a CSV with a bad row → **Download invalid entries** works
- [ ] `/admin`: shows ~958 employees; **Sync roster now** succeeds

### 8e. Email

Reject an entry and look at what arrives:

| What you see | Meaning |
|---|---|
| Bold name + `rejected_entries.csv` attached | n8n is configured correctly |
| Literal `<strong>` tags, no attachment | Step 6 not done yet |
| No email at all | Check n8n execution log; check `EMAIL_WEBHOOK_URL` |

To inspect what the app sent regardless, query the database from Replit's SQL pane:

```sql
SELECT to_email, subject, sent_ok, error FROM email_log ORDER BY id DESC LIMIT 5;
```

### 8f. Then deploy

Everything above runs in the Replit **workspace**. Once it all passes,
**Deploy → Reserved VM** (Step 5) to get the permanent URL.

If your deployed URL differs from the workspace URL, update `APP_BASE_URL` in
Secrets **and** add the deployed URL to Google's redirect URIs.

---

## Rollback

Nothing here touches your local setup. `data/app.db` still has your old SQLite
data, and the previous code is in git history:

```
git log --oneline
git revert <commit>
```

---

## What changed in the code

| File | Change |
|---|---|
| `src/db.py` | Rewritten for PostgreSQL (psycopg3 + connection pool). Added `batch_id` so submissions can be grouped, `count_submissions_for_user()`, and `purge_old_entries()` for age-based retention. Indexes added on the columns actually queried. |
| `src/emailer.py` | Emails are now multipart (plain + HTML). Manager/submitter names bolded. Rejection emails attach `rejected_entries.csv`. Works over both n8n and SMTP. |
| `src/roster_sync.py` | Service account can load from `GOOGLE_SA_JSON_CONTENT` env var instead of a file on disk. |
| `main.py` | `/my-entries` returns only the last N submissions plus counts. Rejection/submission emails now receive the entry rows. Retention purge runs at startup. |
| `templates/index.html` | "Download invalid entries" button on the preview tab. My Entries explains its scope. |
| `.replit` | Reserved VM target, port 5000, proxy headers for OAuth. |
| `requirements.txt` | Added `psycopg[binary]`, `psycopg-pool`; `uvicorn[standard]` for proxy header support. |

### Retention behaviour

`purge_old_entries()` deletes only entries that are **approved AND synced to
ProofHub AND older than `ENTRY_RETENTION_DAYS`**. Pending and rejected entries
are never removed by age — they represent work someone still has to act on.

Note that the existing admin cleanup button (`/admin/cleanup`, scope `synced`)
still deletes **all** synced entries regardless of age. That's a manual action,
so it's left as-is, but be aware of what it does before clicking it.

### On the 3-submission limit

`MY_ENTRIES_SUBMISSIONS=3` hides older submissions from the user's view — the
rows stay in the database. One thing to watch: if someone has entries rejected
and then submits three more batches, the rejected ones drop off their screen and
they lose the rejection reason. If that generates support questions, raise the
number in Secrets (no code change), or ask and I'll make rejected entries always
visible regardless of age.
