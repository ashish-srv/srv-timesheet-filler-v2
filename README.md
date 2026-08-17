# SRV Instant Timesheet Filler

Internal timesheet automation tool for SRV Media.

Employees submit time entries (manually or via CSV upload), their reporting
manager approves or rejects them, and approved entries are pushed to ProofHub
under the submitter's own API key.

## Stack

- **FastAPI** + Uvicorn
- **PostgreSQL** (via `DATABASE_URL`)
- **Google Sign-In** (Authlib), access gated by the HR roster
- **Google Sheets** roster sync (service account, read-only)
- Email via **n8n webhook**, with SMTP fallback

## Deployment

Runs on **Replit** (Reserved VM). Setup instructions, required Secrets, Google
OAuth configuration and n8n workflow changes are documented in
[`REPLIT_SETUP.md`](REPLIT_SETUP.md).

Local development: see `run_dev.bat` (not committed — it holds credentials).

## Testing

See [`TEST_PLAN.md`](TEST_PLAN.md).
