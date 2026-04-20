#!/usr/bin/env python3
"""
Jira → Telegram Notifier — Visibility Logistics
------------------------------------------------
Runs as a single poll cycle (GitHub Actions handles scheduling).
Reads credentials from environment variables / GitHub Secrets.
"""

import asyncio
import io
import json
import logging
import os
import re
import requests
from datetime import datetime, timedelta
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"

# ── Telegram ───────────────────────────────────────────────────────────────────
API_ID               = 31523874
API_HASH             = "4dd5a8b5e01cb5b34d8c720e0df968fb"
TELEGRAM_SESSION     = os.environ["TELEGRAM_SESSION"]
PM_REVIEW_GROUP      = -5145902047
BOSS_MILESTONE_GROUP = -5278864899

# ── Jira ───────────────────────────────────────────────────────────────────────
JIRA_BASE    = "https://codingmanagerratio.atlassian.net"
JIRA_USER    = "codingmanager.ratio@gmail.com"
JIRA_TOKEN   = os.environ["JIRA_TOKEN"]
JIRA_AUTH    = (JIRA_USER, JIRA_TOKEN)
JIRA_PROJECT = "LS"
BOARD_ID     = 35

# ── Employee map ───────────────────────────────────────────────────────────────
EMPLOYEE_MAP = {
    "712020:ddd44a7a-9648-4d5d-865e-48fe3afbbdc2": {"name": "Nkem",           "chat_id": -5256209562},
    "5ecb867e730ec90c197e6015":                    {"name": "Marvellous",      "chat_id": -4052357693},
    "712020:a70b0562-5559-4eb1-b51d-ed5882b13ed4": {"name": "Victor Idam",    "chat_id": -2550394062},
    "712020:3f74454f-ce7d-4446-af5a-551b87c3b611": {"name": "Benjamin",       "chat_id": -2680132560},
    "712020:b976bb69-4751-4f69-82f1-c14489d49974": {"name": "Clement Sampson","chat_id": -5111040099},
}

# ── Settings ───────────────────────────────────────────────────────────────────
POLL_INTERVAL_MINUTES = 10
QUIET_HOURS_START     = 21
QUIET_HOURS_END       = 8
STALE_TASK_DAYS       = 2
WEEKLY_SUMMARY_DAY    = 0
WEEKLY_SUMMARY_HOUR   = 9

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _migrate_state(state: dict) -> dict:
    state.setdefault("notified_events", [])
    state.setdefault("nudged_today", {})
    state.setdefault("last_weekly_summary", None)
    state.setdefault("seen_comments", [])
    state.setdefault("sent_milestone_versions", [])
    state.setdefault("sent_initial_milestone_report", False)
    return state


def already_notified(state, key):
    return key in state["notified_events"]


def mark_notified(state, key):
    state["notified_events"].append(key)
    if len(state["notified_events"]) > 3000:
        state["notified_events"] = state["notified_events"][-3000:]


def was_nudged_today(state, issue_key):
    today = datetime.now().strftime("%Y-%m-%d")
    return state["nudged_today"].get(issue_key) == today


def mark_nudged_today(state, issue_key):
    today = datetime.now().strftime("%Y-%m-%d")
    state["nudged_today"][issue_key] = today
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    state["nudged_today"] = {k: v for k, v in state["nudged_today"].items() if v >= cutoff}


# ── Time helpers ───────────────────────────────────────────────────────────────

def is_quiet_hours():
    h = datetime.now().hour
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END


def is_weekly_summary_time(state):
    now = datetime.now()
    if now.weekday() != WEEKLY_SUMMARY_DAY or now.hour != WEEKLY_SUMMARY_HOUR:
        return False
    return state.get("last_weekly_summary") != now.strftime("%Y-%m-%d")


# ── Jira helpers ───────────────────────────────────────────────────────────────

def _jira_headers(extra=None):
    import base64
    creds = base64.b64encode(f"{JIRA_USER}:{JIRA_TOKEN}".encode()).decode()
    h = {"Authorization": f"Basic {creds}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    if extra:
        h.update(extra)
    return h


def jira_get(path, params=None):
    r = requests.get(f"{JIRA_BASE}{path}", headers=_jira_headers(), params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def jira_post(path, body):
    r = requests.post(f"{JIRA_BASE}{path}", headers=_jira_headers({"Content-Type": "application/json"}),
                      json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def get_active_sprint_id():
    data = jira_get(f"/rest/agile/1.0/board/{BOARD_ID}/sprint", params={"state": "active"})
    sprints = data.get("values", [])
    return sprints[0]["id"] if sprints else None


def fetch_sprint_issues(sprint_id):
    data = jira_get(f"/rest/agile/1.0/sprint/{sprint_id}/issue", params={
        "maxResults": 200,
        "fields": "summary,status,assignee,issuetype,parent,subtasks,updated,created,duedate,description,comment,attachment",
        "expand": "changelog",
    })
    return data.get("issues", [])


def fetch_issue_full(issue_key):
    return jira_get(f"/rest/api/3/issue/{issue_key}",
                    params={"fields": "summary,description,subtasks,duedate,assignee,status"})


def fetch_subtask_full(issue_key):
    return jira_get(f"/rest/api/3/issue/{issue_key}", params={"fields": "summary,description"})


def download_attachment(url):
    try:
        r = requests.get(url, headers=_jira_headers(), timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.error(f"Failed to download attachment {url}: {e}")
        return None


def fetch_all_versions():
    return jira_get(f"/rest/api/3/project/{JIRA_PROJECT}/versions")


def fetch_version_issues(version_name):
    data = jira_post("/rest/api/3/search/jql", {
        "jql": f'project = "{JIRA_PROJECT}" AND fixVersion = "{version_name}" ORDER BY key ASC',
        "fields": ["summary", "status", "assignee", "issuetype", "key"],
        "maxResults": 100,
    })
    return data.get("issues", [])


def fetch_backlog_epics():
    data = jira_post("/rest/api/3/search/jql", {
        "jql": f'project = "{JIRA_PROJECT}" AND issuetype = Epic AND status != Done ORDER BY key ASC',
        "fields": ["summary", "status", "key"],
        "maxResults": 50,
    })
    return data.get("issues", [])


def fetch_inprogress_issues():
    data = jira_post("/rest/api/3/search/jql", {
        "jql": f'project = "{JIRA_PROJECT}" AND status = "In Progress" AND issuetype != Subtask ORDER BY key ASC',
        "fields": ["summary", "assignee", "key"],
        "maxResults": 50,
    })
    return data.get("issues", [])


# ── ADF → plain text ───────────────────────────────────────────────────────────

def adf_to_text(node):
    if not node: return ""
    if isinstance(node, str): return node
    t = node.get("type", "")
    if t == "text": return node.get("text", "")
    if t in ("paragraph", "heading", "blockquote", "listItem"):
        return "".join(adf_to_text(c) for c in node.get("content", [])) + "\n"
    if t == "hardBreak": return "\n"
    return "".join(adf_to_text(c) for c in node.get("content", []))


# ── Message builders ───────────────────────────────────────────────────────────

def build_assignment_message(parent_key, assignee_name):
    try:
        issue  = fetch_issue_full(parent_key)
        fields = issue["fields"]
        title  = fields.get("summary", parent_key)
        due    = fields.get("duedate") or "TBD"
        if due != "TBD":
            try:
                due = datetime.strptime(due, "%Y-%m-%d").strftime("%-d %B %Y")
            except Exception:
                pass
        subtasks = fields.get("subtasks", [])
        lines = [
            "TASK 📌📌📌",
            f"Assigned to {assignee_name}",
            "",
            title,
            "—" * 45,
            "",
        ]
        if subtasks:
            for i, sub in enumerate(subtasks, 1):
                sub_key     = sub["key"]
                sub_summary = sub["fields"]["summary"]
                clean = re.sub(r"^\[[^\]]+\]\s*", "", sub_summary)
                try:
                    sub_full = fetch_subtask_full(sub_key)
                    desc     = adf_to_text(sub_full["fields"].get("description")).strip()
                    if len(desc) > 300:
                        desc = desc[:297] + "..."
                except Exception:
                    desc = ""
                lines.append(f"({i}). {clean} ⏳")
                if desc:
                    lines.append(desc)
                lines.append("")
        else:
            desc = adf_to_text(fields.get("description")).strip()
            if desc:
                lines.append(desc)
                lines.append("")
        lines.append(f"JIRA WORK ITEM LINK: {JIRA_BASE}/browse/{parent_key}")
        lines.append("")
        lines.append(f"Delivery Timeline ⏰: {due}")
        lines.append("")
        lines.append("Please go to Jira to view the full task, update your status as you work, and drop screenshots/comments when done.")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"Failed to build assignment message for {parent_key}: {e}")
        return (f"TASK 📌📌📌\nAssigned to {assignee_name}\n\n[{parent_key}]\n\n"
                f"JIRA WORK ITEM LINK: {JIRA_BASE}/browse/{parent_key}\n\n"
                f"Please go to Jira to view the full details and update your status.")


def msg_in_progress(key, summary, name):
    return (f"🔵 {name} started work\n\n[{key}] {summary}\n\n"
            f"Keep Jira updated. Move to In Review when ready.\n👉 {JIRA_BASE}/browse/{key}")


def msg_in_review(key, summary, name):
    return (f"👀 Ready for your review\n\n[{key}] {summary}\nSubmitted by: {name}\n\n"
            f"👉 {JIRA_BASE}/browse/{key}")


def msg_done(key, summary):
    return (f"✅ Marked as Done\n\n[{key}] {summary}\n\nGood work. 👍\n"
            f"👉 {JIRA_BASE}/browse/{key}")


def msg_stale(key, summary, status, days):
    return (f"⏳ Update needed\n\n[{key}] {summary}\n"
            f"Status: {status} — no update in {days} day{'s' if days != 1 else ''}\n\n"
            f"Please drop a comment or update the status in Jira.\n👉 {JIRA_BASE}/browse/{key}")


def msg_screenshot(key, summary, author, comment_text):
    preview = comment_text[:200].strip() if comment_text else ""
    return (f"🖼 Screenshot/file added\n\n[{key}] {summary}\nBy: {author}\n"
            + (f'"{preview}"\n' if preview else "")
            + f"\n👉 {JIRA_BASE}/browse/{key}")


def _comment_has_media(body):
    if not body: return False
    body_str = json.dumps(body)
    return any(t in body_str for t in ('"mediaSingle"', '"media"', '"image"'))


# ── Weekly summary ─────────────────────────────────────────────────────────────

def build_weekly_summary(issues):
    now      = datetime.now()
    week_ago = now - timedelta(days=7)
    done, in_prog, not_started = [], [], []
    for issue in issues:
        f = issue["fields"]
        if f.get("issuetype", {}).get("subtask", False):
            continue
        key      = issue["key"]
        summary  = f.get("summary", "")
        status   = f.get("status", {}).get("name", "")
        assignee = (f.get("assignee") or {}).get("displayName", "Unassigned")
        try:
            upd_dt = datetime.fromisoformat(f.get("updated","").replace("Z","+00:00")).replace(tzinfo=None)
        except Exception:
            upd_dt = None
        if status == "Done" and upd_dt and upd_dt >= week_ago:
            done.append((key, summary, assignee))
        elif status == "In Progress":
            in_prog.append((key, summary, assignee))
        elif status == "To Do":
            not_started.append((key, summary, assignee))
    lines = [f"Work Summary — Week ending {now.strftime('%-d %B %Y')}", "=" * 42, ""]
    if done:
        lines += [f"✅ Completed this week ({len(done)})"]
        for k, s, a in done:
            lines.append(f"  · [{k}] {s[:55]} — {a}")
        lines.append("")
    if in_prog:
        lines += [f"🔵 In progress ({len(in_prog)})"]
        for k, s, a in in_prog:
            lines.append(f"  · [{k}] {s[:55]} — {a}")
        lines.append("")
    if not_started:
        lines += [f"⚪ Not yet started ({len(not_started)})"]
        for k, s, a in not_started[:10]:
            lines.append(f"  · [{k}] {s[:55]} — {a}")
        if len(not_started) > 10:
            lines.append(f"  ... and {len(not_started) - 10} more")
        lines.append("")
    lines.append(f"👉 {JIRA_BASE}/jira/software/projects/{JIRA_PROJECT}/boards/{BOARD_ID}")
    return "\n".join(lines)


# ── Milestone helpers ──────────────────────────────────────────────────────────

def _format_release_date(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%-d %B %Y")
    except Exception:
        return raw


def _assignees_str(issues):
    names = sorted({(i["fields"].get("assignee") or {}).get("displayName","") for i in issues} - {""})
    return ", ".join(names) if names else "Team"


def build_milestone_completed_msg(version, issues):
    name      = version.get("name", "")
    date      = _format_release_date(version.get("releaseDate", ""))
    assignees = _assignees_str(issues)
    lines = ["🏁 MILESTONE REACHED", "", name, f"📅 Completed: {date}", "", "What shipped:"]
    for issue in issues:
        lines.append(f"• {issue['fields']['summary']} ({issue['key']})")
    lines += ["", f"Built by: {assignees}", f"Tasks completed: {len(issues)}", "",
              f"👉 {JIRA_BASE}/jira/software/projects/{JIRA_PROJECT}/boards/{BOARD_ID}"]
    return "\n".join(lines)


def build_full_project_report():
    versions           = fetch_all_versions()
    released           = sorted([v for v in versions if v.get("released")], key=lambda v: v.get("releaseDate",""))
    in_progress_issues = fetch_inprogress_issues()
    backlog_epics      = fetch_backlog_epics()
    messages = []
    messages.append(
        "📊 LIVETRADER SAAS — PROJECT STATUS\n" + "=" * 38 + "\n\n"
        + f"{len(released)} milestones completed. Full breakdown below 👇"
    )
    for version in released:
        name      = version.get("name", "")
        date      = _format_release_date(version.get("releaseDate", ""))
        issues    = fetch_version_issues(name)
        assignees = _assignees_str(issues)
        lines = ["✅ MILESTONE COMPLETED", "", name, f"📅 Completed: {date}", "", "What shipped:"]
        for issue in issues:
            lines.append(f"  • {issue['fields']['summary']} ({issue['key']})")
        lines += ["", f"Built by: {assignees} | Tasks: {len(issues)}"]
        messages.append("\n".join(lines))
    if in_progress_issues:
        lines = ["🔄 CURRENTLY IN PROGRESS", ""]
        for issue in in_progress_issues:
            assignee = (issue["fields"].get("assignee") or {}).get("displayName", "Unassigned")
            lines.append(f"  • {issue['fields']['summary']} ({issue['key']}) — {assignee}")
        messages.append("\n".join(lines))
    if backlog_epics:
        lines = ["📋 UPCOMING — NEXT MILESTONES", ""]
        for epic in backlog_epics:
            lines.append(f"  ◦ {epic['fields']['summary']}")
        lines += ["", f"👉 {JIRA_BASE}/jira/software/projects/{JIRA_PROJECT}/boards/{BOARD_ID}"]
        messages.append("\n".join(lines))
    return messages


# ── Core processing ────────────────────────────────────────────────────────────

def process_issues(issues, state):
    outbox      = {}
    attachments = []
    now         = datetime.now()

    for issue in issues:
        f          = issue["fields"]
        key        = issue["key"]
        summary    = f.get("summary", "")[:60]
        status     = f.get("status", {}).get("name", "")
        assignee   = f.get("assignee") or {}
        account_id = assignee.get("accountId", "")
        name       = assignee.get("displayName", "Unassigned")
        is_subtask = f.get("issuetype", {}).get("subtask", False)
        emp        = EMPLOYEE_MAP.get(account_id)

        for history in issue.get("changelog", {}).get("histories", []):
            for item in history.get("items", []):
                if item.get("field") == "status":
                    to_status = item.get("toString", "")
                    ekey = f"status_{key}_{to_status}"
                    if already_notified(state, ekey):
                        continue
                    mark_notified(state, ekey)
                    if to_status == "In Progress" and emp:
                        outbox.setdefault(emp["chat_id"], []).append(msg_in_progress(key, summary, name))
                    elif to_status == "In Review":
                        outbox.setdefault(PM_REVIEW_GROUP, []).append(msg_in_review(key, summary, name))
                    elif to_status == "Done" and emp:
                        outbox.setdefault(emp["chat_id"], []).append(msg_done(key, summary))

                elif item.get("field") == "assignee":
                    new_id  = item.get("to", "")
                    ekey    = f"assign_{key}_{new_id}"
                    if already_notified(state, ekey):
                        continue
                    mark_notified(state, ekey)
                    new_emp = EMPLOYEE_MAP.get(new_id)
                    if not new_emp:
                        continue
                    if is_subtask:
                        parent_key     = (f.get("parent") or {}).get("key", "")
                        parent_summary = (f.get("parent") or {}).get("fields", {}).get("summary", "")
                        url            = f"{JIRA_BASE}/browse/{key}"
                        msg = (f"TASK 📌📌📌\nAssigned to {new_emp['name']}\n\n[{key}] {summary}\n"
                               + (f"Part of: {parent_key} — {parent_summary[:50]}\n" if parent_key else "")
                               + f"\nJIRA WORK ITEM LINK: {url}\n\n"
                               "Please go to Jira to view details and update your status as you work.")
                        outbox.setdefault(new_emp["chat_id"], []).append(msg)
                    else:
                        outbox.setdefault(new_emp["chat_id"], []).append(
                            build_assignment_message(key, new_emp["name"]))

        # Catch tasks created already-assigned
        if emp and not is_subtask:
            ekey = f"assign_{key}_{account_id}"
            if not already_notified(state, ekey):
                mark_notified(state, ekey)
                created_str = f.get("created", "")
                try:
                    created_dt  = datetime.fromisoformat(created_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    age_minutes = (now - created_dt).total_seconds() / 60
                    is_new      = age_minutes <= POLL_INTERVAL_MINUTES * 2
                except Exception:
                    is_new = False
                if is_new:
                    outbox.setdefault(emp["chat_id"], []).append(
                        build_assignment_message(key, emp["name"]))

        # All subtasks done nudge
        subtasks = f.get("subtasks", [])
        if subtasks and status == "In Progress" and emp:
            all_done = all(s.get("fields",{}).get("status",{}).get("name") == "Done" for s in subtasks)
            nudge_key = f"all_subtasks_done_{key}"
            if all_done and not already_notified(state, nudge_key):
                mark_notified(state, nudge_key)
                url = f"{JIRA_BASE}/browse/{key}"
                outbox.setdefault(emp["chat_id"], []).append(
                    f"🎉 All subtasks are Done!\n\n[{key}] {summary}\n\n"
                    f"All {len(subtasks)} subtasks are complete. Please review, then move to In Review.\n👉 {url}")

        # Comments
        PM_ACCOUNT_ID = "712020:117e1bc9-4cb0-4fa1-b8b9-7d0e10121c22"
        comments = f.get("comment", {}).get("comments", [])
        for comment in comments:
            comment_id  = comment.get("id", "")
            author_id   = comment.get("author", {}).get("accountId", "")
            author_name = comment.get("author", {}).get("displayName", "Someone")
            body_text   = adf_to_text(comment.get("body")).strip()
            has_media   = _comment_has_media(comment.get("body"))

            if author_id == PM_ACCOUNT_ID and emp:
                seen_key = f"pm_comment_{key}_{comment_id}"
                if not already_notified(state, seen_key) and body_text:
                    mark_notified(state, seen_key)
                    outbox.setdefault(emp["chat_id"], []).append(
                        f"💬 Note added to your task\n\n[{key}] {summary}\n\n{body_text[:400]}\n\n"
                        f"👉 {JIRA_BASE}/browse/{key}")

            if has_media:
                seen_key = f"comment_media_{key}_{comment_id}"
                if not already_notified(state, seen_key):
                    mark_notified(state, seen_key)
                    targets = [PM_REVIEW_GROUP]
                    if emp and emp["chat_id"] != PM_REVIEW_GROUP:
                        targets.append(emp["chat_id"])
                    for att in f.get("attachment", []) or []:
                        att_id  = att.get("id", "")
                        att_key = f"attachment_{att_id}"
                        if already_notified(state, att_key):
                            continue
                        mark_notified(state, att_key)
                        if att.get("mimeType", "").startswith("image/"):
                            data = download_attachment(att["content"])
                            if data:
                                fname = att.get("filename", "image.png")
                                caption_pm  = msg_screenshot(key, summary, author_name, body_text)
                                caption_emp = (f"🖼 Screenshot added to your task\n\n[{key}] {summary}\n"
                                               f"By: {author_name}\n\n👉 {JIRA_BASE}/browse/{key}")
                                for i, chat_id in enumerate(targets):
                                    cap = caption_pm if chat_id == PM_REVIEW_GROUP else caption_emp
                                    attachments.append((chat_id, cap, data, fname))

        # Stale nudge
        if status in ("To Do", "In Progress") and emp and not was_nudged_today(state, key):
            try:
                upd_dt     = datetime.fromisoformat(f.get("updated","").replace("Z","+00:00")).replace(tzinfo=None)
                days_stale = (now - upd_dt).days
                if days_stale >= STALE_TASK_DAYS:
                    outbox.setdefault(emp["chat_id"], []).append(msg_stale(key, summary, status, days_stale))
                    mark_nudged_today(state, key)
            except Exception:
                pass

    return outbox, attachments


# ── Milestone check ────────────────────────────────────────────────────────────

async def check_milestones(client, state):
    if not state["sent_initial_milestone_report"]:
        try:
            messages = build_full_project_report()
            for msg in messages:
                await client.send_message(BOSS_MILESTONE_GROUP, msg)
                await asyncio.sleep(1)
            state["sent_initial_milestone_report"] = True
            for v in fetch_all_versions():
                if v.get("released"):
                    vid = str(v["id"])
                    if vid not in state["sent_milestone_versions"]:
                        state["sent_milestone_versions"].append(vid)
            log.info(f"Initial milestone report sent ({len(messages)} messages)")
        except Exception as e:
            log.error(f"Failed to send milestone report: {e}")
        return

    try:
        versions = fetch_all_versions()
    except Exception as e:
        log.error(f"Failed to fetch versions: {e}")
        return

    for version in versions:
        if not version.get("released"):
            continue
        vid = str(version["id"])
        if vid in state["sent_milestone_versions"]:
            continue
        try:
            issues = fetch_version_issues(version["name"])
            msg    = build_milestone_completed_msg(version, issues)
            await client.send_message(BOSS_MILESTONE_GROUP, msg)
            state["sent_milestone_versions"].append(vid)
            log.info(f"Milestone sent: {version['name']}")
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Failed to send milestone {vid}: {e}")


# ── Telegram sending ───────────────────────────────────────────────────────────

async def send_all(client, outbox, attachments):
    for chat_id, messages in outbox.items():
        try:
            text = messages[0] if len(messages) == 1 else ("\n\n" + ("─" * 30) + "\n\n").join(messages)
            await client.send_message(chat_id, text)
            log.info(f"Sent to {chat_id}")
            await asyncio.sleep(2)
        except FloodWaitError as e:
            log.warning(f"Flood wait {e.seconds}s — skipped {chat_id}")
        except Exception as e:
            log.error(f"Text send failed for {chat_id}: {e}")

    for chat_id, caption, data, filename in attachments:
        try:
            file = io.BytesIO(data)
            file.name = filename
            await client.send_file(chat_id, file, caption=caption or "")
            log.info(f"Sent image {filename} to {chat_id}")
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Image send failed for {chat_id}: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    if is_quiet_hours():
        log.info("Quiet hours — skipping")
        return

    state     = _migrate_state(load_state())
    sprint_id = get_active_sprint_id()
    outbox, attachments = {}, []

    if sprint_id:
        issues              = fetch_sprint_issues(sprint_id)
        outbox, attachments = process_issues(issues, state)
        if not outbox and not attachments:
            log.info("No new notifications")
    else:
        issues = []
        log.info("No active sprint")

    async with TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH) as client:
        if outbox or attachments:
            await send_all(client, outbox, attachments)
        if sprint_id and is_weekly_summary_time(state):
            summary_text = build_weekly_summary(issues)
            await send_all(client, {PM_REVIEW_GROUP: [summary_text]}, [])
            state["last_weekly_summary"] = datetime.now().strftime("%Y-%m-%d")
            log.info("Weekly summary sent")
        await check_milestones(client, state)

    save_state(state)
    log.info("Poll cycle complete")


if __name__ == "__main__":
    asyncio.run(main())
