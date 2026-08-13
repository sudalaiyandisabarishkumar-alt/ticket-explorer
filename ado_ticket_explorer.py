"""
Interactive Azure DevOps ticket-activity explorer.

Flow:
  1. Show all organizations your PAT can access -> pick one
  2. Show all projects in that organization -> pick one
  3. Show all users on that project (union of team members) -> pick one
  4. Prompt for a single date or a date range
  5. Print every ticket assigned to that user that had activity in that
     window: status changes, comments added, attachments added -- each
     with a timestamp, sorted into one timeline per ticket.

Env vars required:
  ADO_PAT   - Azure DevOps Personal Access Token.
              Needs scopes: Identity (Read), Project and Team (Read),
              Work Items (Read).
  GROQ_API_KEY - Groq API key (console.groq.com), used to generate the
              natural-language summary paragraph. If unset (or the call
              fails for any reason), the script falls back to a
              deterministic, template-based summary so the report still
              gets produced.
  GROQ_MODEL  - optional, defaults to "llama-3.3-70b-versatile".

Usage:
  ADO_PAT=xxxx GROQ_API_KEY=yyyy python ado_ticket_explorer.py
"""

import os
import sys
import re
import json
import argparse
import base64
import html as html_module
from collections import Counter
from xml.sax.saxutils import escape as xml_escape
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

IST = timezone(timedelta(hours=5, minutes=30))

ADO_PAT = os.environ.get("ADO_PAT", "")
if not ADO_PAT:
    sys.exit("ADO_PAT environment variable is required.")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

ADO_AUTH = base64.b64encode(f":{ADO_PAT}".encode()).decode()
ADO_HEADERS = {
    "Authorization": f"Basic {ADO_AUTH}",
    "Content-Type": "application/json",
}
API_VERSION = "7.1"

PROFILE_URL = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"
ACCOUNTS_URL = "https://app.vssps.visualstudio.com/_apis/accounts"

TERMINAL_STATES = {"closed", "done", "removed", "merged"}


# --- small helpers ----------------------------------------------------------

def _clean_html_text(raw: str) -> str:
    """
    ADO comment bodies are rich HTML (e.g. '<div><a data-vss-mention="...">
    @user</a></div>'). Strip tags down to plain, readable text -- this runs
    BEFORE any XML-escaping, so the output here should be plain text with no
    markup left at all.
    """
    if not raw:
        return ""
    text = raw
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(div|p|li)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)          # strip all remaining tags
    text = html_module.unescape(text)             # decode &amp; etc.
    text = re.sub(r"\n\s*\n+", "\n", text)         # collapse blank lines
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _get(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, headers=ADO_HEADERS, params=params, timeout=20)
    if resp.status_code >= 400:
        print(f"\nRequest failed [{resp.status_code}] {url}\n{resp.text}\n", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


def choose(label: str, items: list[dict], display_key) -> dict:
    """Print a numbered menu of items and return the chosen one."""
    if not items:
        sys.exit(f"No {label} found for this PAT — nothing to select.")
    print(f"\n{label}:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {display_key(item)}")
    while True:
        raw = input(f"Select a {label[:-1] if label.endswith('s') else label} (1-{len(items)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print("Invalid selection, try again.")


def prompt_date_range() -> tuple[datetime, datetime]:
    print("\nDate selection (IST):")
    print("  1. Single date")
    print("  2. Date range")
    mode = input("Choose 1 or 2: ").strip()
    fmt = "%Y-%m-%d"
    if mode == "1":
        raw = input("Enter date (YYYY-MM-DD): ").strip()
        day = datetime.strptime(raw, fmt).replace(tzinfo=IST)
        since = day
        until = day + timedelta(days=1) - timedelta(seconds=1)
    else:
        raw_since = input("Enter start date (YYYY-MM-DD): ").strip()
        raw_until = input("Enter end date (YYYY-MM-DD): ").strip()
        since = datetime.strptime(raw_since, fmt).replace(tzinfo=IST)
        until = datetime.strptime(raw_until, fmt).replace(tzinfo=IST) + timedelta(days=1) - timedelta(seconds=1)
    return since, until


# --- step 1: organizations ---------------------------------------------------

def list_organizations() -> list[dict]:
    """
    Requires an account-wide PAT (created with "All accessible organizations",
    not scoped to a single org) plus Identity/User Profile (Read). If the PAT
    is org-scoped, this 401s -- that's expected and handled by the caller,
    which falls back to manual org entry.
    """
    profile = _get(PROFILE_URL, params={"api-version": API_VERSION})
    member_id = profile["id"]
    accounts = _get(ACCOUNTS_URL, params={"memberId": member_id, "api-version": API_VERSION})
    # sort alphabetically for a stable, readable menu
    return sorted(accounts.get("value", []), key=lambda a: a["accountName"].lower())


def resolve_organization() -> str:
    try:
        orgs = list_organizations()
        return choose("organizations", orgs, lambda a: a["accountName"])["accountName"]
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print(
                "\nCouldn't list organizations for this PAT (401) -- it's likely scoped to a "
                "single org rather than 'All accessible organizations', or is missing the "
                "'User Profile (Read)' scope. Falling back to manual entry.\n"
                "(Tip: to fix this permanently, generate a new PAT and set its organization "
                "to 'All accessible organizations'.)\n",
                file=sys.stderr,
            )
            org = input("Enter your Azure DevOps organization name: ").strip()
            if not org:
                sys.exit("Organization name is required.")
            return org
        raise


# --- step 2: projects ---------------------------------------------------

def list_projects(org: str) -> list[dict]:
    url = f"https://dev.azure.com/{quote(org)}/_apis/projects"
    data = _get(url, params={"api-version": API_VERSION, "$top": 200})
    return sorted(data.get("value", []), key=lambda p: p["name"].lower())


# --- step 3: project users (union of team members) ---------------------------------------------------



def list_project_users(org: str, project_id: str) -> list[dict]:
    """
    Get all users visible to the PAT in the Azure DevOps organization.
    """

    users = []
    GRAPH_URL = f"https://vssps.dev.azure.com/{quote(org)}/_apis/graph/users"
    continuation_token = None

    while True:
        params = {
            "api-version": "7.1-preview.1",
            "$top": 500,
        }

        if continuation_token:
            params["continuationToken"] = continuation_token

        resp = requests.get(
            GRAPH_URL,
            headers=ADO_HEADERS,
            params=params,
            timeout=20,
        )

        resp.raise_for_status()

        data = resp.json()

        for user in data.get("value", []):
            display_name = user.get("displayName")
            unique_name = (
                user.get("mail")
                or user.get("principalName")
                or user.get("descriptor")
            )

            if not display_name or not unique_name:
                continue

            users.append({
                "displayName": display_name,
                "uniqueName": unique_name,
            })

        continuation_token = resp.headers.get("x-ms-continuationtoken")

        if not continuation_token:
            break

    # Remove duplicates
    users_by_unique_name = {
        user["uniqueName"].lower(): user
        for user in users
    }

    return sorted(
        users_by_unique_name.values(),
        key=lambda u: u["displayName"].lower()
    )


# --- step 4/5: tickets + per-ticket activity timeline ---------------------------------------------------

def _wiql_ticket_ids(org: str, project: str, email: str, since: datetime, until: datetime) -> list[str]:
    url = f"https://dev.azure.com/{quote(org)}/{quote(project)}/_apis/wit/wiql"
    # WIQL's date comparison is coarse (effectively UTC, date-granularity), so widen
    # by a day on each side to avoid clipping IST-boundary activity -- the precise,
    # timezone-aware filtering happens afterward in build_ticket_timeline.
    query_since = since - timedelta(days=1)
    query_until = until + timedelta(days=1)
    wiql = f"""
        SELECT [System.Id] FROM WorkItems
        WHERE [System.TeamProject] = '{project}'
          AND [System.AssignedTo] = '{email}'
          AND [System.ChangedDate] >= '{query_since.strftime('%Y-%m-%d')}'
          AND [System.ChangedDate] <= '{query_until.strftime('%Y-%m-%d')}'
        ORDER BY [System.ChangedDate] DESC
    """
    resp = requests.post(
        url, headers=ADO_HEADERS, params={"api-version": API_VERSION}, json={"query": wiql}, timeout=20
    )
    if resp.status_code >= 400:
        print(f"\nWIQL query failed [{resp.status_code}]\n{resp.text}\n")
    resp.raise_for_status()
    return [str(item["id"]) for item in resp.json().get("workItems", [])]


def _ticket_details(org: str, ids: list[str], fields: str | None = None) -> dict[str, dict]:
    if not ids:
        return {}
    url = f"https://dev.azure.com/{quote(org)}/_apis/wit/workitems"
    data = _get(url, params={
        "ids": ",".join(ids),
        "fields": fields or "System.Title,System.State,System.WorkItemType,System.ChangedDate,"
                             "System.AreaPath,System.Parent",
        "api-version": API_VERSION,
    })
    return {str(wi["id"]): wi["fields"] for wi in data.get("value", [])}


def _ticket_updates(org: str, project: str, ticket_id: str) -> list[dict]:
    url = f"https://dev.azure.com/{quote(org)}/{quote(project)}/_apis/wit/workitems/{ticket_id}/updates"
    return _get(url, params={"api-version": API_VERSION, "$top": 200}).get("value", [])


def _ticket_comments(org: str, project: str, ticket_id: str) -> list[dict]:
    url = f"https://dev.azure.com/{quote(org)}/{quote(project)}/_apis/wit/workItems/{ticket_id}/comments"
    try:
        return _get(url, params={"api-version": "7.1-preview.3"}).get("comments", [])
    except requests.HTTPError:
        return []  # comments API can 404 on some process templates/older items


def build_ticket_timeline(org: str, project: str, ticket_id: str,
                           since: datetime, until: datetime) -> list[dict]:
    events = []

    for rev in _ticket_updates(org, project, ticket_id):
        revised_raw = rev.get("revisedDate")
        if not revised_raw:
            continue
        # ADO returns 9999-01-01 for the *current* revision's "revisedDate" sentinel
        if revised_raw.startswith("9999"):
            revised_raw = rev.get("fields", {}).get("System.ChangedDate", {}).get("newValue")
            if not revised_raw:
                continue
        revised = datetime.fromisoformat(revised_raw.replace("Z", "+00:00"))
        if not (since <= revised <= until):
            continue

        fields = rev.get("fields", {})
        revised_by = rev.get("revisedBy", {}) or {}
        author_display = revised_by.get("displayName", "unknown")
        author_unique = revised_by.get("uniqueName", "")

        if "System.State" in fields:
            old = fields["System.State"].get("oldValue", "New")
            new = fields["System.State"].get("newValue")
            if new:
                events.append({
                    "timestamp": revised,
                    "type": "status_change",
                    "detail": f"Status changed: {old} -> {new}",
                    "author_display_name": author_display,
                    "author_unique_name": author_unique,
                })

        for rel in rev.get("relations", {}).get("added", []):
            rel_type = rel.get("rel", "")
            if rel_type == "AttachedFile":
                name = rel.get("attributes", {}).get("name", "attachment")
                events.append({
                    "timestamp": revised,
                    "type": "attachment_added",
                    "detail": f"Attachment added: {name}",
                    "author_display_name": author_display,
                    "author_unique_name": author_unique,
                })

    for comment in _ticket_comments(org, project, ticket_id):
        created_raw = comment.get("createdDate")
        if not created_raw:
            continue
        created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        if not (since <= created <= until):
            continue
        created_by = comment.get("createdBy", {}) or {}
        author = created_by.get("displayName", "unknown")
        text = _clean_html_text(comment.get("text") or "").replace("\n", " ")
        if len(text) > 140:
            text = text[:137] + "..."
        events.append({
            "timestamp": created,
            "type": "comment_added",
            "detail": f"Comment by {author}: {text}",
            "author_display_name": author,
            "author_unique_name": created_by.get("uniqueName", ""),
        })

    events.sort(key=lambda e: e["timestamp"])
    return events


def _event_matches_user(event: dict, user: dict) -> bool:
    """True if this event was actually performed by the selected user, not
    just recorded on a ticket assigned to them. Matches on uniqueName
    (email) when available since it's the stable identifier; falls back to
    a case-insensitive displayName comparison for the rare update record
    that's missing uniqueName."""
    user_unique = (user.get("uniqueName") or "").strip().lower()
    user_display = (user.get("displayName") or "").strip().lower()
    event_unique = (event.get("author_unique_name") or "").strip().lower()
    event_display = (event.get("author_display_name") or "").strip().lower()
    if user_unique and event_unique:
        return user_unique == event_unique
    return bool(user_display) and user_display == event_display


def gather_report_data(org: str, project: str, user: dict, since: datetime, until: datetime) -> list[dict]:
    """Fetch and shape everything needed for both the console print and the PDF."""
    ticket_ids = _wiql_ticket_ids(org, project, user["uniqueName"], since, until)
    details = _ticket_details(org, ticket_ids)

    # collect parent (user story) IDs so we can resolve their titles in one batch call
    parent_ids = sorted({
        str(meta["System.Parent"])
        for meta in details.values()
        if meta.get("System.Parent") is not None
    })
    parent_details = _ticket_details(org, parent_ids, fields="System.Title,System.WorkItemType") if parent_ids else {}

    tickets = []
    for ticket_id in ticket_ids:
        meta = details.get(ticket_id, {})
        parent_id = meta.get("System.Parent")
        parent_id_str = str(parent_id) if parent_id is not None else None
        parent_meta = parent_details.get(parent_id_str, {}) if parent_id_str else {}
        full_timeline = build_ticket_timeline(org, project, ticket_id, since, until)
        # Tickets come back from WIQL because they're *assigned* to the user, but
        # the timeline includes every teammate's edits on that ticket. Keep only
        # the events the selected user actually performed -- their own status
        # changes, comments, attachments -- not everyone else's activity.
        own_timeline = [e for e in full_timeline if _event_matches_user(e, user)]
        tickets.append({
            "id": ticket_id,
            "title": meta.get("System.Title", "(untitled)"),
            "type": meta.get("System.WorkItemType", "Work Item"),
            "state": meta.get("System.State", "Unknown"),
            "area_path": meta.get("System.AreaPath", ""),
            "parent_id": parent_id_str,
            "parent_title": parent_meta.get("System.Title"),
            "timeline": own_timeline,
        })
    return tickets


def _module_from_area_path(area_path: str, project: str) -> str | None:
    # Area paths look like "Vgro - Rq devops\Vgro\Payments" -- take the leaf
    # segment as the "module", unless it's just the project root itself.
    if not area_path:
        return None
    parts = [p for p in area_path.split("\\") if p.strip()]
    if not parts:
        return None
    leaf = parts[-1]
    return None if leaf.strip().lower() == project.strip().lower() else leaf


# --- manager-facing metrics ---------------------------------------------------

def compute_metrics(tickets: list[dict], project: str) -> dict:
    """
    Numeric, manager-facing metrics derived from the same ticket/timeline data
    as the narrative summary -- deterministic, no LLM involved, so these numbers
    are always exact even when the AI summary is in use.
    """
    total = len(tickets)
    closed = [t for t in tickets if t["state"].lower() in TERMINAL_STATES]
    active = [t for t in tickets if t["state"].lower() not in TERMINAL_STATES]

    type_counts = Counter(t["type"] for t in tickets)
    module_counts = Counter(
        m for t in tickets if (m := _module_from_area_path(t.get("area_path", ""), project))
    )

    status_changes = sum(1 for t in tickets for e in t["timeline"] if e["type"] == "status_change")
    comments = sum(1 for t in tickets for e in t["timeline"] if e["type"] == "comment_added")
    attachments = sum(1 for t in tickets for e in t["timeline"] if e["type"] == "attachment_added")
    total_events = status_changes + comments + attachments

    stale = [t for t in tickets if not t["timeline"]]  # touched by WIQL match but no recorded events

    return {
        "total_tickets": total,
        "closed_count": len(closed),
        "active_count": len(active),
        "type_counts": dict(type_counts.most_common()),
        "module_counts": dict(module_counts.most_common()),
        "status_changes": status_changes,
        "comments": comments,
        "attachments": attachments,
        "total_events": total_events,
        "stale_count": len(stale),
        "stale_ids": [t["id"] for t in stale],
    }


# --- deterministic fallback summary ---------------------------------------------------


def build_summary_paragraph_fallback(tickets: list[dict], user: dict, project: str,
                                      since: datetime, until: datetime) -> str:
    """
    Deterministic, data-driven summary of the user's activity -- no LLM call,
    built entirely from the same ticket/timeline data as the rest of the report.
    Used as a fallback when GROQ_API_KEY is unset or the Groq call fails, so the
    report never comes up short a summary.
    """
    date_label = (since.date().isoformat() if since.date() == until.date()
                  else f"{since.date().isoformat()} to {until.date().isoformat()}")

    if not tickets:
        return (f"{user['displayName']} had no recorded ticket activity between "
                f"{date_label}.")

    total = len(tickets)
    type_counts = Counter(t["type"] for t in tickets)
    type_desc = ", ".join(
        f"{count} {wtype.lower()}{'s' if count != 1 else ''}"
        for wtype, count in type_counts.items()
    )

    status_changes = sum(1 for t in tickets for e in t["timeline"] if e["type"] == "status_change")
    comments = sum(1 for t in tickets for e in t["timeline"] if e["type"] == "comment_added")
    attachments = sum(1 for t in tickets for e in t["timeline"] if e["type"] == "attachment_added")

    closed = [t for t in tickets if t["state"].lower() in TERMINAL_STATES]
    active = [t for t in tickets if t["state"].lower() not in TERMINAL_STATES]

    def _id_list(items: list[dict], limit: int = 5) -> str:
        ids = ", ".join(f"#{t['id']}" for t in items[:limit])
        if len(items) > limit:
            ids += f", and {len(items) - limit} more"
        return ids

    def _module_name(area_path: str) -> str | None:
        return _module_from_area_path(area_path, project)

    sentences = [
        f"Between {date_label}, {user['displayName']} was active on {total} "
        f"ticket{'s' if total != 1 else ''} ({type_desc})."
    ]
    if status_changes:
        sentences.append(
            f"{status_changes} status transition{'s' if status_changes != 1 else ''} "
            f"{'were' if status_changes != 1 else 'was'} recorded across these items, "
            f"reflecting movement through the workflow."
        )
    if comments:
        sentences.append(
            f"{comments} comment{'s' if comments != 1 else ''} "
            f"{'were' if comments != 1 else 'was'} added, showing "
            f"active discussion and collaboration on the assigned work."
        )
    if attachments:
        sentences.append(
            f"{attachments} attachment{'s' if attachments != 1 else ''} "
            f"{'were' if attachments != 1 else 'was'} uploaded in support of these tickets."
        )
    if closed:
        sentences.append(
            f"{len(closed)} ticket{'s' if len(closed) != 1 else ''} reached a closed or "
            f"completed state, including {_id_list(closed)}."
        )
    if active:
        sentences.append(
            f"{len(active)} ticket{'s' if len(active) != 1 else ''} remain active or in "
            f"progress, including {_id_list(active)}."
        )

    # --- project context: modules (area paths) ---
    module_counts = Counter(m for t in tickets if (m := _module_name(t.get("area_path", ""))))
    if len(module_counts) == 1:
        module_name = next(iter(module_counts))
        sentences.append(f"Most of this work fell under the {module_name} module.")
    elif len(module_counts) > 1:
        top = ", ".join(f"{name} ({count})" for name, count in module_counts.most_common(4))
        sentences.append(f"Work spanned multiple modules within the project, including {top}.")

    # --- project context: parent user stories ---
    parent_map: dict[str, dict] = {}
    for t in tickets:
        if t.get("parent_id") and t.get("parent_title"):
            entry = parent_map.setdefault(t["parent_id"], {"title": t["parent_title"], "ticket_ids": []})
            entry["ticket_ids"].append(t["id"])

    if len(parent_map) == 1:
        (pid, info), = parent_map.items()
        sentences.append(
            f"These tickets were primarily linked to the '{info['title']}' user story (#{pid})."
        )
    elif len(parent_map) > 1:
        items = list(parent_map.items())[:4]
        parts = [f"'{info['title']}' (#{pid})" for pid, info in items]
        more = f", and {len(parent_map) - 4} more" if len(parent_map) > 4 else ""
        sentences.append(
            f"Tickets were linked to multiple parent user stories, including {', '.join(parts)}{more}."
        )

    unlinked = total - sum(len(info["ticket_ids"]) for info in parent_map.values())
    if parent_map and unlinked:
        sentences.append(
            f"{unlinked} ticket{'s' if unlinked != 1 else ''} "
            f"{'were' if unlinked != 1 else 'was'} not linked to a parent user story."
        )

    activity_score = status_changes + comments + attachments
    pace = "steady" if activity_score >= 4 else "light"
    sentences.append(f"Overall, this reflects a period of {pace} engagement on assigned work.")

    return " ".join(sentences)


# --- Groq-powered summary ---------------------------------------------------

def _tickets_to_prompt_payload(tickets: list[dict], project: str,
                                max_events_per_ticket: int = 6,
                                max_tickets: int = 20) -> tuple[list[dict], int, int]:
    """Compact, LLM-friendly representation of the ticket data -- keeps the
    prompt small (and within free-tier TPM limits) by capping how many
    tickets and events per ticket get included. Returns (payload, tickets_omitted,
    events_omitted) so the caller can tell the model what was left out, rather
    than silently under-reporting activity."""
    tickets_omitted = max(0, len(tickets) - max_tickets)
    included_tickets = tickets[:max_tickets]

    payload = []
    events_omitted = 0
    for t in included_tickets:
        events = t["timeline"]
        if len(events) > max_events_per_ticket:
            events_omitted += len(events) - max_events_per_ticket
            # keep the most recent events -- most relevant for a status summary
            events = events[-max_events_per_ticket:]

        payload.append({
            "id": t["id"],
            "title": t["title"],
            "type": t["type"],
            "state": t["state"],
            "module": _module_from_area_path(t.get("area_path", ""), project),
            "parent_user_story": (
                f"#{t['parent_id']} {t['parent_title']}" if t.get("parent_title") else None
            ),
            "total_event_count": len(t["timeline"]),
            "events": [
                {
                    "time": e["timestamp"].astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
                    "type": e["type"],
                    "detail": e["detail"][:100],
                }
                for e in events
            ],
        })
    return payload, tickets_omitted, events_omitted


def build_summary_paragraph_ai(tickets: list[dict], user: dict, project: str,
                                since: datetime, until: datetime) -> str:
    """
    Calls the Groq API to generate a natural-language summary of the user's
    ticket activity. Falls back to the deterministic summary if GROQ_API_KEY
    is unset or the request fails for any reason -- the report generation
    should never hard-fail just because the AI call didn't work.
    """
    if not GROQ_API_KEY:
        return build_summary_paragraph_fallback(tickets, user, project, since, until)

    date_label = (since.date().isoformat() if since.date() == until.date()
                  else f"{since.date().isoformat()} to {until.date().isoformat()}")

    if not tickets:
        return (f"{user['displayName']} had no recorded ticket activity between "
                f"{date_label}.")

    import json
    ticket_payload, tickets_omitted, events_omitted = _tickets_to_prompt_payload(tickets, project)

    system_prompt = (
        "You write short, factual status-report summaries for engineering managers "
        "based on Azure DevOps ticket activity data. Write ONE paragraph (4-8 sentences), "
        "plain prose, no headings or bullet points. Only state facts present in the data -- "
        "never invent ticket IDs, counts, module names, or dates. Each ticket includes a "
        "'total_event_count' field which is the true total even if fewer events are shown in "
        "'events' -- use total_event_count when describing activity volume, not the length of "
        "the events list. Similarly, use the 'Total tickets with activity' figure given in the "
        "prompt for the overall ticket count, not the number of entries in the JSON array, since "
        "some may have been omitted for space. If the data notes that some tickets or events were "
        "omitted for space, mention this once in passing rather than ignoring it. Mention: how "
        "many tickets and of what types, key status transitions, notable comments/collaboration, "
        "closed vs still-active work (reference a few ticket IDs), which modules/user stories the "
        "work concentrated in, and an overall read on the pace of activity. Keep it dense but readable."
    )
    omission_note = ""
    if tickets_omitted or events_omitted:
        parts = []
        if tickets_omitted:
            parts.append(f"{tickets_omitted} additional ticket(s) with activity were omitted from this data for space")
        if events_omitted:
            parts.append(f"{events_omitted} additional event(s) across the included tickets were omitted for space")
        omission_note = "\n\nNote: " + "; ".join(parts) + "."

    user_prompt = (
        f"Person: {user['displayName']} ({user['uniqueName']})\n"
        f"Project: {project}\n"
        f"Window: {date_label}\n"
        f"Total tickets with activity in this window: {len(tickets)}\n\n"
        f"Ticket activity data (JSON):\n{json.dumps(ticket_payload, ensure_ascii=False)}"
        f"{omission_note}"
    )

    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            # Groq puts the actual reason in the response body (bad model slug,
            # unsupported param, context length exceeded, etc.) -- surface it
            # instead of letting raise_for_status() swallow it into a generic
            # "400 Client Error" message.
            print(f"\n[warn] Groq API error body: {resp.text}\n", file=sys.stderr)
        resp.raise_for_status()
        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
        if not summary:
            raise ValueError("Groq returned an empty summary")
        return summary
    except Exception as e:
        print(f"\n[warn] Groq summary generation failed ({e}); using fallback summary.\n", file=sys.stderr)
        return build_summary_paragraph_fallback(tickets, user, project, since, until)


def print_report(tickets: list[dict], user: dict, project: str, since: datetime, until: datetime,
                  summary: str, metrics: dict):
    print(f"\nTickets for {user['displayName']} ({user['uniqueName']}) "
          f"in '{project}' between {since.date()} and {until.date()}...\n")

    print(summary)
    print()

    if not tickets:
        return

    print("-" * 70)
    print("METRICS")
    print("-" * 70)
    print(f"  Total tickets with activity : {metrics['total_tickets']}")
    print(f"  Closed / completed          : {metrics['closed_count']}")
    print(f"  Active / in progress        : {metrics['active_count']}")
    if metrics["stale_count"]:
        stale_ids = ", ".join(f"#{i}" for i in metrics["stale_ids"][:8])
        more = f" (+{len(metrics['stale_ids']) - 8} more)" if len(metrics["stale_ids"]) > 8 else ""
        print(f"  Assigned, no own activity   : {metrics['stale_count']} ({stale_ids}{more})")
    print(f"  Status transitions          : {metrics['status_changes']}")
    print(f"  Comments                    : {metrics['comments']}")
    print(f"  Attachments                 : {metrics['attachments']}")
    if metrics["type_counts"]:
        by_type = ", ".join(f"{k}: {v}" for k, v in metrics["type_counts"].items())
        print(f"  By type                     : {by_type}")
    if metrics["module_counts"]:
        by_module = ", ".join(f"{k}: {v}" for k, v in metrics["module_counts"].items())
        print(f"  By module                   : {by_module}")
    print()

    for t in tickets:
        is_terminal = t["state"].lower() in TERMINAL_STATES
        print("=" * 70)
        print(f"#{t['id']} [{t['type']}] {t['title']}")
        print(f"Current state: {t['state']}{' (closed)' if is_terminal else ''}")
        print("-" * 70)
        if not t["timeline"]:
            print("  (no field/comment/attachment changes recorded in this window)")
            continue
        for event in t["timeline"]:
            ts = event["timestamp"].astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
            print(f"  [{ts}] {event['detail']}")
    print("=" * 70)


# --- PDF report ---------------------------------------------------

EVENT_LABELS = {
    "status_change": "Status",
    "comment_added": "Comment",
    "attachment_added": "Attachment",
}


def generate_pdf_report(tickets: list[dict], user: dict, org: str, project: str,
                         since: datetime, until: datetime, summary: str, metrics: dict) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable
    )

    EVENT_COLORS = {
        "status_change": "#1d4ed8",   # blue
        "comment_added": "#15803d",   # green
        "attachment_added": "#b45309", # amber
    }
    STATE_COLOR_TERMINAL = "#6b7280"   # gray
    STATE_COLOR_ACTIVE = "#1d4ed8"     # blue

    date_label = (since.date().isoformat() if since.date() == (until.date())
                  else f"{since.date().isoformat()} to {until.date().isoformat()}")
    safe_name = user["displayName"].replace(" ", "_")
    filename = f"ticket_report_{safe_name}_{since.date()}_{until.date()}.pdf"
    path = os.path.join(os.getcwd(), filename)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], fontSize=20, spaceAfter=2)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=10,
                                     textColor=colors.HexColor("#4b5563"), spaceAfter=4)
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], fontSize=9,
                                 textColor=colors.HexColor("#6b7280"))
    ticket_title_style = ParagraphStyle("TicketTitle", parent=styles["Heading2"], fontSize=13,
                                         spaceBefore=18, spaceAfter=2, textColor=colors.HexColor("#111827"))
    ticket_meta_style = ParagraphStyle("TicketMeta", parent=styles["Normal"], fontSize=9,
                                        spaceAfter=2, textColor=colors.HexColor("#4b5563"))
    ticket_context_style = ParagraphStyle("TicketContext", parent=styles["Normal"], fontSize=8.5,
                                           spaceAfter=8, textColor=colors.HexColor("#6b7280"))
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9, leading=12)
    empty_style = ParagraphStyle("Empty", parent=styles["Normal"], fontSize=9,
                                  textColor=colors.HexColor("#9ca3af"), leftIndent=4)
    summary_heading_style = ParagraphStyle("SummaryHeading", parent=styles["Heading3"], fontSize=11,
                                            spaceAfter=4, textColor=colors.HexColor("#111827"))
    summary_style = ParagraphStyle("Summary", parent=styles["Normal"], fontSize=10, leading=15,
                                    textColor=colors.HexColor("#1f2937"))

    story = [
        Paragraph("Ticket Activity Report", title_style),
        Paragraph(f"{xml_escape(user['displayName'])} &lt;{xml_escape(user['uniqueName'])}&gt;", subtitle_style),
        Paragraph(f"{xml_escape(org)} / {xml_escape(project)}  |  {date_label}", meta_style),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e5e7eb")),
        Spacer(1, 12),
        Paragraph("Summary", summary_heading_style),
        Paragraph(xml_escape(summary), summary_style),
        Spacer(1, 14),
    ]

    if tickets:
        story.append(Paragraph("Metrics", summary_heading_style))

        kpi_rows = [
            ["Total tickets", str(metrics["total_tickets"]), "Status transitions", str(metrics["status_changes"])],
            ["Closed / completed", str(metrics["closed_count"]), "Comments", str(metrics["comments"])],
            ["Active / in progress", str(metrics["active_count"]), "Attachments", str(metrics["attachments"])],
        ]
        if metrics["stale_count"]:
            kpi_rows.append(["Assigned, no own activity", str(metrics["stale_count"]), "", ""])

        kpi_table = Table(kpi_rows, colWidths=[1.7 * inch, 0.9 * inch, 1.7 * inch, 0.9 * inch])
        kpi_table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#4b5563")),
            ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#4b5563")),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1d4ed8")),
            ("TEXTCOLOR", (3, 0), (3, -1), colors.HexColor("#1d4ed8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(kpi_table)
        story.append(Spacer(1, 8))

        if metrics["type_counts"]:
            by_type = "  &bull;  ".join(f"{xml_escape(k)}: <b>{v}</b>" for k, v in metrics["type_counts"].items())
            story.append(Paragraph(f"By type: {by_type}", ticket_meta_style))
        if metrics["module_counts"]:
            by_module = "  &bull;  ".join(f"{xml_escape(k)}: <b>{v}</b>" for k, v in metrics["module_counts"].items())
            story.append(Paragraph(f"By module: {by_module}", ticket_meta_style))

        story.append(Spacer(1, 14))

    # Flatten to one row per event the selected user actually performed
    # (t["timeline"] is already filtered to their own actions -- see
    # gather_report_data), each still carrying a reference back to its ticket.
    activity_items = []
    for t in tickets:
        for event in t["timeline"]:
            local_ts = event["timestamp"].astimezone(IST)
            activity_items.append({"date": local_ts.date(), "timestamp": local_ts, "ticket": t, "event": event})
    activity_items.sort(key=lambda item: item["timestamp"])

    if not tickets:
        story.append(Paragraph("No tickets assigned in this window.", styles["Normal"]))
    elif not activity_items:
        story.append(Paragraph(
            f"{len(tickets)} ticket(s) were assigned to {xml_escape(user['displayName'])} in this window, "
            "but no status changes, comments, or attachments were personally recorded by them.",
            styles["Normal"],
        ))
    else:
        distinct_tickets = len({item["ticket"]["id"] for item in activity_items})
        story.append(Paragraph(
            f"{len(activity_items)} action{'s' if len(activity_items) != 1 else ''} by "
            f"{xml_escape(user['displayName'])} across {distinct_tickets} ticket(s)",
            meta_style,
        ))

        grouped: dict = {}
        for item in activity_items:
            grouped.setdefault(item["date"], []).append(item)

        for day in sorted(grouped.keys()):
            items = grouped[day]
            block = [
                Paragraph(day.strftime("%A, %d %B %Y"), ticket_title_style),
                Paragraph(f"{len(items)} action{'s' if len(items) != 1 else ''}", ticket_meta_style),
                Spacer(1, 4),
            ]

            rows = [["Time", "Type", "Ticket", "Detail"]]
            for item in items:
                event, t = item["event"], item["ticket"]
                ts = item["timestamp"].strftime("%H:%M IST")
                label = EVENT_LABELS.get(event["type"], event["type"])
                color = EVENT_COLORS.get(event["type"], "#000000")
                type_cell = Paragraph(f'<font color="{color}"><b>{xml_escape(label)}</b></font>', cell_style)

                is_terminal = t["state"].lower() in TERMINAL_STATES
                state_color = STATE_COLOR_TERMINAL if is_terminal else STATE_COLOR_ACTIVE
                ticket_cell = Paragraph(
                    f'#{t["id"]} {xml_escape(t["title"])}<br/>'
                    f'<font size="7.5" color="#6b7280">{xml_escape(t["type"])} &bull; '
                    f'<font color="{state_color}">{xml_escape(t["state"])}</font></font>',
                    cell_style,
                )
                detail_cell = Paragraph(xml_escape(event["detail"]), cell_style)
                rows.append([Paragraph(ts, cell_style), type_cell, ticket_cell, detail_cell])

            table = Table(rows, colWidths=[0.75 * inch, 0.75 * inch, 1.8 * inch, 3.55 * inch], repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#374151")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ]))
            block.append(table)
            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

    SimpleDocTemplate(
        path, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
    ).build(story)
    return path


# --- non-interactive (--quiet) support ---------------------------------------------------

_RELATIVE_DATE_TOKENS = {"today", "yesterday", "this-week", "last-week"}


def _parse_quiet_date_range(tokens: list[str]) -> tuple[datetime, datetime]:
    """
    Resolve the date argument(s) passed on the command line in --quiet mode.
    Accepts:
      []                          -> defaults to "today"
      ["today"]                   -> today, IST
      ["yesterday"]               -> yesterday, IST
      ["this-week"]               -> Monday of this week through now
      ["last-week"]               -> Monday-Sunday of the previous ISO week
      ["YYYY-MM-DD"]              -> that single day
      ["YYYY-MM-DD", "YYYY-MM-DD"]-> explicit since/until (inclusive)
    """
    fmt = "%Y-%m-%d"
    now = datetime.now(tz=IST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if len(tokens) == 0:
        token = "today"
    else:
        token = tokens[0].strip().lower()

    if token in _RELATIVE_DATE_TOKENS:
        if token == "today":
            since = today
            until = today + timedelta(days=1) - timedelta(seconds=1)
        elif token == "yesterday":
            since = today - timedelta(days=1)
            until = today - timedelta(seconds=1)
        elif token == "this-week":
            since = today - timedelta(days=today.weekday())  # Monday
            until = now
        else:  # last-week
            this_monday = today - timedelta(days=today.weekday())
            since = this_monday - timedelta(days=7)
            until = this_monday - timedelta(seconds=1)
        return since, until

    # Otherwise treat token(s) as explicit YYYY-MM-DD date(s).
    try:
        since = datetime.strptime(tokens[0], fmt).replace(tzinfo=IST)
    except (ValueError, IndexError) as e:
        raise ValueError(
            f"Couldn't parse date argument {tokens!r}. Expected 'today', 'yesterday', "
            f"'this-week', 'last-week', a single YYYY-MM-DD date, or a YYYY-MM-DD "
            f"YYYY-MM-DD range."
        ) from e

    if len(tokens) >= 2:
        until = datetime.strptime(tokens[1], fmt).replace(tzinfo=IST) + timedelta(days=1) - timedelta(seconds=1)
    else:
        until = since + timedelta(days=1) - timedelta(seconds=1)

    return since, until


def _resolve_project_by_name(org: str, name: str) -> dict:
    projects = list_projects(org)
    exact = [p for p in projects if p["name"].lower() == name.lower()]
    if exact:
        return exact[0]
    partial = [p for p in projects if name.lower() in p["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    available = ", ".join(p["name"] for p in projects)
    if partial:
        candidates = ", ".join(p["name"] for p in partial)
        raise ValueError(f"Project name '{name}' is ambiguous. Matches: {candidates}")
    raise ValueError(f"No project found matching '{name}'. Available projects: {available}")


def _find_user(users: list[dict], query: str) -> dict:
    q = query.strip().lower()
    exact = [u for u in users if u["displayName"].lower() == q or u["uniqueName"].lower() == q]
    if exact:
        return exact[0]
    partial = [u for u in users if q in u["displayName"].lower() or q in u["uniqueName"].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        candidates = ", ".join(f"{u['displayName']} <{u['uniqueName']}>" for u in partial[:10])
        raise ValueError(f"User '{query}' is ambiguous. Matches: {candidates}")
    raise ValueError(f"No user found on this project matching '{query}'.")


def run_quiet(user_query: str, date_tokens: list[str], org: str | None = None, project_name: str | None = None) -> dict:
    """
    Fully non-interactive run for automation (Slack slash command, cron, CI, etc.).
    `org` and `project_name` are normally supplied explicitly by the caller
    (e.g. a Slack picker flow where the manager selected them from a live
    list). If omitted, falls back to ADO_ORG / ADO_PROJECT env vars for
    simple cron/CLI use -- but neither is required to be preset; there's no
    fixed "the" org/project baked into this script.
    Resolves the user by name/email substring match, generates the report +
    PDF, and returns a JSON-serializable dict. Never raises for "expected"
    failures (bad user, bad project, no data) -- those come back as
    {"ok": False, "error": ...} so a calling webhook can relay a clean
    message instead of a stack trace.
    """
    org = (org or os.environ.get("ADO_ORG", "")).strip()
    project_name = (project_name or os.environ.get("ADO_PROJECT", "")).strip()
    if not org or not project_name:
        return {"ok": False, "error": "An organization and project are required (pass --org/--project, or set ADO_ORG/ADO_PROJECT)."}

    try:
        project = _resolve_project_by_name(org, project_name)
        users = list_project_users(org, project["id"])
        user = _find_user(users, user_query)
        since, until = _parse_quiet_date_range(date_tokens)
    except (ValueError, requests.HTTPError) as e:
        return {"ok": False, "error": str(e)}

    try:
        tickets = gather_report_data(org, project["name"], user, since, until)
        metrics = compute_metrics(tickets, project["name"])
        summary = build_summary_paragraph_ai(tickets, user, project["name"], since, until)
        pdf_path = generate_pdf_report(tickets, user, org, project["name"], since, until, summary, metrics)
    except requests.HTTPError as e:
        return {"ok": False, "error": f"Azure DevOps request failed: {e}"}

    date_label = (since.date().isoformat() if since.date() == until.date()
                  else f"{since.date().isoformat()} to {until.date().isoformat()}")

    return {
        "ok": True,
        "user": user["displayName"],
        "user_unique_name": user["uniqueName"],
        "org": org,
        "project": project["name"],
        "date_label": date_label,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "summary": summary,
        "metrics": metrics,
        "ticket_count": len(tickets),
        "pdf_path": pdf_path,
    }


# --- main ---------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--quiet", action="store_true",
        help="Non-interactive mode: take org/project/user/dates as arguments (or fall "
             "back to ADO_ORG/ADO_PROJECT env vars if --org/--project are omitted), "
             "print a single JSON line to stdout and nothing else. For use from "
             "scripts/webhooks (e.g. the Slack /status-report handler) rather than a terminal.",
    )
    parser.add_argument("--org", default=None, help="[--quiet only] Azure DevOps organization name.")
    parser.add_argument("--project", default=None, help="[--quiet only] Azure DevOps project name.")
    parser.add_argument(
        "user", nargs="?", default=None,
        help="[--quiet only] Display name or email (or a substring of either) to match "
             "a single project user, e.g. 'Tarun'.",
    )
    parser.add_argument(
        "dates", nargs="*", default=[],
        help="[--quiet only] 'today' | 'yesterday' | 'this-week' | 'last-week' | "
             "'YYYY-MM-DD' | 'YYYY-MM-DD YYYY-MM-DD'. Defaults to 'today' if omitted.",
    )
    return parser


def main():
    args = build_arg_parser().parse_args()

    if args.quiet:
        if not args.user:
            print(json.dumps({"ok": False, "error": "Usage: --quiet <user> [date|since until]"}))
            sys.exit(2)
        result = run_quiet(args.user, args.dates, org=args.org, project_name=args.project)
        print(json.dumps(result))
        sys.exit(0 if result.get("ok") else 1)

    # --- interactive mode (unchanged) ---
    if not GROQ_API_KEY:
        print("\n[info] GROQ_API_KEY not set -- summary will use the deterministic fallback.\n")

    org = resolve_organization()

    projects = list_projects(org)
    project = choose("projects", projects, lambda p: p["name"])
    project_id, project_name = project["id"], project["name"]

    users = list_project_users(org, project_id)
    user = choose("users", users, lambda u: f"{u['displayName']} <{u['uniqueName']}>")

    since, until = prompt_date_range()

    print(f"\nFetching activity for {user['displayName']} in '{project_name}'...")
    tickets = gather_report_data(org, project_name, user, since, until)

    metrics = compute_metrics(tickets, project_name)

    print("Generating summary...")
    summary = build_summary_paragraph_ai(tickets, user, project_name, since, until)

    print_report(tickets, user, project_name, since, until, summary, metrics)

    pdf_path = generate_pdf_report(tickets, user, org, project_name, since, until, summary, metrics)
    print(f"\nPDF report saved to: {pdf_path}")


if __name__ == "__main__":
    main()