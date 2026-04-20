# Jira → Telegram Notifier

Runs on GitHub Actions every 3 minutes. Polls a Jira project for activity and sends Telegram notifications to the relevant team members.

## What it does

- Notifies assigned employees when a task is assigned to them (with full task details and Jira link)
- Alerts when a task moves to In Progress, In Review, or Done
- Forwards screenshots/attachments added to tasks
- Nudges team members when tasks go stale with no update
- Sends a weekly summary to the PM review group every Monday morning
- Reports completed milestones (Jira Versions) to a boss group

## How it runs

GitHub Actions triggers the workflow on a cron schedule. Each run is a single poll cycle — no persistent process needed.

Secrets required (set under repo Settings → Secrets → Actions):
- `TELEGRAM_SESSION` — Telethon StringSession for the Telegram account
- `JIRA_TOKEN` — Atlassian API token

## Setup

1. Generate and upload the Telegram session secret:
   ```bash
   python set_session_secret.py <github_token>
   ```

2. Add `JIRA_TOKEN` manually under GitHub repo secrets.

3. Push to `main` — the workflow starts automatically.

## Files

| File | Purpose |
|------|---------|
| `notifier.py` | Main script — Jira polling and Telegram sending |
| `state.json` | Persisted state to avoid duplicate notifications (auto-committed by workflow) |
| `.github/workflows/notify.yml` | GitHub Actions workflow definition |
| `set_session_secret.py` | One-time script to generate and upload Telegram session |
