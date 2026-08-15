# Local Test Plan — SRV Timesheet Tool

Work through these in order. Stop at the first failure and tell me what you saw.

---

## PART A — One-time setup

### A1. Install PostgreSQL
Download PostgreSQL 16 from postgresql.org/download/windows. During install:
- Set a password for the `postgres` user — **write it down**
- Keep the default port **5432**
- Accept all other defaults

### A2. Create the database
Open Command Prompt:

```
"C:\Program Files\PostgreSQL\16\bin\psql" -U postgres -c "CREATE DATABASE srvtimesheet;"
```

Enter the password from A1. You should see `CREATE DATABASE`.

### A3. Point the app at it
Open `run_dev.bat`, find line 22, replace `YOURPASSWORD` with your real password:

```bat
set DATABASE_URL=postgresql://postgres:MyRealPassword@localhost:5432/srvtimesheet
```

### A4. Install the new dependency

```
cd /d "D:\Updated ProofHub Tool 15-08-2026\SRV- timesheet filler - 30-07-2026"
venv\Scripts\activate
pip install -r requirements.txt
```

`psycopg` and `psycopg-pool` are the new ones.

---

## PART B — Does it start?

Double-click `run_dev.bat`.

**Expect in the console:**
```
[roster] startup sync: {'rows_in_sheet': ..., 'upserted': ..., 'active': ...}
Uvicorn running on http://127.0.0.1:8123
```

| Problem | Cause |
|---|---|
| `STOP: DATABASE_URL still contains YOURPASSWORD` | Redo A3 |
| `connection refused` | Postgres service isn't running — check Services → postgresql-x64-16 |
| `password authentication failed` | Wrong password in A3 |
| `database "srvtimesheet" does not exist` | Redo A2 |

**Verify the roster loaded:**
```
"C:\Program Files\PostgreSQL\16\bin\psql" -U postgres -d srvtimesheet -c "SELECT COUNT(*) FROM employees;"
```
Should be around **958**.

---

## PART C — Feature tests

Sign in at http://localhost:8123 with Google.

---

### TEST 1 — Key card hides after first connect

| # | Do this | Expect |
|---|---|---|
| 1.1 | Sign in for the first time | Card **"1 · Connect your ProofHub key"** is visible. No ⚙ Settings in the navbar. |
| 1.2 | Paste your ProofHub key, click **Save & connect** | Card disappears. Green banner at top: "✓ Connected to ProofHub. Loaded N project(s)." **⚙ Settings** appears in the navbar. Project dropdown is populated. |
| 1.3 | Press F5 to reload | **No key card at all.** Page opens straight to Add Time with projects loaded. |
| 1.4 | Sign out, sign in again | Still no key card. This is the main thing being tested. |

---

### TEST 2 — Settings panel

| # | Do this | Expect |
|---|---|---|
| 2.1 | Click **⚙ Settings** | Card reappears titled **"ProofHub connection"**, with a green "✓ Connected. A ProofHub key is saved for your account (last updated ...)" line. Field reads "Paste a new key to replace the saved one". Cancel button present. |
| 2.2 | Click **Cancel** | Card closes, page scrolls to top, nothing changed. |
| 2.3 | Settings → paste **garbage** (e.g. `abc123`) → Replace key | Red error: connection failed. Card **stays open**. Your real key is still saved. |
| 2.4 | Reload the page | Still works — projects load. The bad key was never stored. |
| 2.5 | Settings → paste your **real** key again → Replace key | Card closes, banner says "✓ ProofHub key replaced." |

---

### TEST 3 — Recovery when the saved key dies

This is the failure mode that would lock users out, so test it properly.

| # | Do this | Expect |
|---|---|---|
| 3.1 | Settings → **"Remove the stored key"** → confirm | Card returns to first-run mode. ⚙ Settings disappears. Tabs are locked with "🔒 Connect your key above to start." |
| 3.2 | Reload the page | Key card shows (you have no key now) — correct. |
| 3.3 | Paste your real key, save | Back to normal. |

*Optional, if you're willing:* regenerate your API key inside ProofHub so the stored one goes dead, then reload the app. You should see **"Reconnect your ProofHub key — your saved key is no longer working."** rather than a silently broken page.

---

### TEST 4 — Download invalid entries

Make a CSV with a mix of good and bad rows. Something like:

```csv
project_name,timesheet_title,date,logged_hours,logged_mins,status,description
<a real project name>,,2026-08-14,2,30,billable,Good row
Fake Project Name,,2026-08-14,1,0,billable,Bad project
<a real project name>,,2026-08-14,99,0,billable,Too many hours
<a real project name>,,2030-01-01,1,0,billable,Future date
```

| # | Do this | Expect |
|---|---|---|
| 4.1 | Bulk Upload → choose the file → **Validate & preview** | Summary chips show a rejected count. |
| 4.2 | Look below the table | Button **"Download invalid entries (3)"** — count matches the rejected chip. |
| 4.3 | Click it | Downloads `invalid_entries_2026-08-15.csv`. |
| 4.4 | Open in Excel | Opens cleanly (no mangled characters). Has an **`error_reason`** column explaining each failure. |
| 4.5 | Fix the errors in that same file, save, re-upload it | Validates with **0 rejected**. The extra `error_reason` column is ignored. |
| 4.6 | Upload a CSV with **no** bad rows | Download button does **not** appear. |

---

### TEST 5 — My Entries shows only last 3 submissions

| # | Do this | Expect |
|---|---|---|
| 5.1 | Submit **4 separate batches** (Add Time → add entries → Submit, four times; use different descriptions like "batch 1", "batch 2"…) | Four success messages. |
| 5.2 | Open **My Entries** | Only batches 2, 3 and 4 appear. **Batch 1 is not shown.** |
| 5.3 | Read the note above the table | "Showing your last 3 submissions… (3 of 4 submissions shown — older ones are archived.)" |
| 5.4 | Confirm nothing was deleted | `psql -U postgres -d srvtimesheet -c "SELECT COUNT(*) FROM entries;"` — all rows still there, including batch 1. Hidden, not deleted. |

---

### TEST 6 — Rejection email with CSV attachment + bold name

**Important:** your `run_dev.bat` has `EMAIL_WEBHOOK_URL` set, so mail goes to n8n. Until you make the n8n changes (step 6 of REPLIT_SETUP.md), rejection emails will arrive with **visible `<strong>` tags and no attachment** — that is expected, not a bug.

To test the finished email format **before** touching n8n, temporarily disable the webhook:

```bat
REM set EMAIL_WEBHOOK_URL=https://n8n.srv.media/webhook/srv-timesheet-email
```

Put the `REM` in front, save, restart. Mail now goes over SMTP with your Gmail app password.

| # | Do this | Expect |
|---|---|---|
| 6.1 | Submit a few entries | They land as pending. |
| 6.2 | Go to **/manager** (you're an admin, so you see all pending) | Entries listed. |
| 6.3 | Select them → **Reject** with a reason | Success. |
| 6.4 | Check the submitter's inbox | Email arrives with the manager's name in **bold** and `rejected_entries.csv` attached. |
| 6.5 | Open the attachment | Contains the rejected rows plus `rejected_by` and `rejection_reason` columns. |
| 6.6 | Re-upload that attachment to Bulk Upload | Validates fine — extra columns ignored. |

Remember to remove the `REM` afterwards.

**If you'd rather not send real email:** comment out `SMTP_USER` and `SMTP_PASS` too. The app then runs in capture mode and writes the message to the database instead:

```
psql -U postgres -d srvtimesheet -c "SELECT to_email, subject, error FROM email_log ORDER BY id DESC LIMIT 5;"
```

---

### TEST 7 — Nothing else broke

Quick pass over things you already had working:

- [ ] Add Time: pick project → timesheets load → add several entries → submit
- [ ] Manager page: approve entries → progress bar → they reach ProofHub
- [ ] Admin page: stats show ~958 employees
- [ ] Admin → Sync roster now: succeeds
- [ ] Admin → upload roster CSV: succeeds
- [ ] Sign out / sign in

---

## PART D — What you can't test locally

| Thing | Why | Test where |
|---|---|---|
| Google login over HTTPS | The proxy-header fix only matters behind Replit's proxy | After deploying — this is the most likely first-deploy problem |
| n8n HTML + attachment | Needs the workflow changes | After step 6 of REPLIT_SETUP.md |
| Timezone handling | Your PC is already IST; Replit is UTC | After deploying — check a submitted entry's timestamp is IST |
| Retention purge | Needs entries over a year old | Optional: manually backdate a row's `submitted_at` and restart |

---

## Report back

Tell me the test number and what you saw. Console output helps if it crashed.
