"""
Slack slash-command webhook for /status-report -- guided picker version.

Instead of typing the org/project/user, the manager runs:

    /status-report

and gets a live dropdown of every organization the ADO_PAT can see. Picking
one refreshes the message with a dropdown of that org's projects. Picking a
project refreshes it with a dropdown of that project's users. Picking a user
shows date-range buttons (Today / Yesterday / This week / Last week / Custom
range). Once a range is chosen, the report generates and the summary + PDF
land in the channel.

No ADO_ORG / ADO_PROJECT env vars needed anywhere -- everything is resolved
live from the Azure DevOps API and carried step-to-step inside the Slack
message/interaction itself (stateless: no server-side session store, so this
works fine even behind a load balancer with multiple webhook instances).

How state travels between steps:
  - select_org  -> selected org name becomes visible text in the next
                   message ("Org: <name>") and IS the project option value
                   (short enough to fit Slack's 75-char static_select cap).
  - select_project -> project id/name/org don't fit in 75 chars as JSON, so
                   they're stashed server-side in an in-memory context cache
                   (_ctx_cache) and only a short opaque key is put in each
                   user option's value.
  - select_user -> the resolved context dict is embedded as real JSON in
                   each date button's value (buttons allow up to 2000
                   chars, plenty of room -- no cache needed there).
  - date button / custom-range modal -> full JSON decoded, report generated.

Note: _ctx_cache is in-process memory, so this only works with a single
webhook instance (fine for local/dev use; for multi-instance production
deployments, back it with Redis or similar instead).

Two endpoints:
  POST /slack/status-report   - the slash command itself (starts the flow)
  POST /slack/interactivity   - every dropdown pick / button click / modal
                                 submission from here on

Env vars:
  SLACK_SIGNING_SECRET   - required.
  SLACK_BOT_TOKEN        - required (chat:write, files:write) -- used for
                            in-channel posts, modals, and the PDF upload.
  ADO_PAT                - required. Needs the PAT scoped to "All accessible
                            organizations" (not a single org) plus
                            Identity/User Profile (Read), or org listing will
                            fail -- see the error message that's shown if so.
  GROQ_API_KEY            - optional (falls back to a deterministic summary).
  SCRIPT_MODULE_PATH      - path to ado_ticket_explorer.py if it's not
                             alongside this file.

Run locally:
  pip install flask requests reportlab
  SLACK_SIGNING_SECRET=... SLACK_BOT_TOKEN=xoxb-... ADO_PAT=... \
    python slack_status_report_webhook.py
  # expose with e.g. `ngrok http 3000`, then set BOTH:
  #   Slash command request URL      -> https://<host>/slack/status-report
  #   Interactivity request URL      -> https://<host>/slack/interactivity
"""

import os
import sys
import hmac
import time
import json
import uuid
import hashlib
import threading
import importlib.util

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SCRIPT_MODULE_PATH = os.environ.get(
    "SCRIPT_MODULE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ado_ticket_explorer.py")
)

# --- load ado_ticket_explorer.py as a module so we can call its functions directly
# (list_organizations, list_projects, list_project_users, gather_report_data, ...)
# instead of shelling out -- we need the intermediate listing calls anyway.
_spec = importlib.util.spec_from_file_location("ado_ticket_explorer", SCRIPT_MODULE_PATH)
adx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adx)  # requires ADO_PAT to already be set in the environment

SLACK_API = "https://slack.com/api"
DATE_TOKENS = [("today", "Today"), ("yesterday", "Yesterday"), ("this-week", "This week"), ("last-week", "Last week")]

# --- context cache ---------------------------------------------------
# Slack caps a static_select option's "value" at 75 chars, which is too
# small to hold the org/project/user JSON blob we need to carry between
# picker steps (that's what was causing the JSONDecodeError -- the JSON
# was being silently chopped to 75 chars by _truncate before Slack even
# saw it). Instead we stash the real context dict here under a short
# opaque key and only ever put that key in the option value. Entries
# expire after CTX_TTL_SECONDS so this can't grow unbounded; a picker
# flow left half-finished for that long just needs to be restarted.
CTX_TTL_SECONDS = 15 * 60
_ctx_cache: dict[str, tuple[float, dict]] = {}
_ctx_lock = threading.Lock()


def _store_ctx(ctx: dict) -> str:
    key = uuid.uuid4().hex[:12]
    now = time.time()
    with _ctx_lock:
        _ctx_cache[key] = (now, ctx)
        # opportunistic cleanup of expired entries
        expired = [k for k, (ts, _) in _ctx_cache.items() if now - ts > CTX_TTL_SECONDS]
        for k in expired:
            del _ctx_cache[k]
    return key


def _load_ctx(key: str) -> dict:
    with _ctx_lock:
        entry = _ctx_cache.get(key)
    if entry is None:
        raise KeyError(key)
    ts, ctx = entry
    if time.time() - ts > CTX_TTL_SECONDS:
        with _ctx_lock:
            _ctx_cache.pop(key, None)
        raise KeyError(key)
    return ctx


# --- Slack request verification ---------------------------------------------------

def verify_slack_signature(req) -> bool:
    if not SLACK_SIGNING_SECRET:
        return os.environ.get("ALLOW_UNSIGNED_REQUESTS") == "1"
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    slack_signature = req.headers.get("X-Slack-Signature", "")
    if not timestamp or not slack_signature:
        return False
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, slack_signature)


def slack_headers(json_body=True) -> dict:
    h = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def post_response(response_url: str, blocks=None, text: str = "", replace_original=True, in_channel=False):
    payload = {"replace_original": replace_original}
    if in_channel:
        payload["response_type"] = "in_channel"
    if text:
        payload["text"] = text
    if blocks is not None:
        payload["blocks"] = blocks
    requests.post(response_url, json=payload, timeout=10)


# --- Block Kit builders ---------------------------------------------------

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def blocks_select(header: str, breadcrumb_lines: list[str], action_id: str, options: list[tuple[str, str]]):
    """options: list of (label, value). Slack caps option value at 75 chars and
    a static_select at 100 options -- both enforced here."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{header}*"}}]
    for line in breadcrumb_lines:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line}]})
    truncated = len(options) > 100
    options = options[:100]
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "static_select",
            "action_id": action_id,
            "placeholder": {"type": "plain_text", "text": "Select..."},
            "options": [
                {"text": {"type": "plain_text", "text": _truncate(label, 75)}, "value": _truncate(value, 75)}
                for label, value in options
            ],
        }],
    })
    if truncated:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ":warning: List truncated to 100 -- ask an admin if your pick is missing."}]})
    return blocks


def blocks_date_buttons(breadcrumb_lines: list[str], context_json: str):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Pick a date range*"}}]
    for line in breadcrumb_lines:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line}]})
    elements = [
        {"type": "button", "action_id": f"date_{token}", "text": {"type": "plain_text", "text": label},
         "value": json.dumps({**json.loads(context_json), "date_token": token})}
        for token, label in DATE_TOKENS
    ]
    elements.append({"type": "button", "action_id": "custom_range", "text": {"type": "plain_text", "text": "Custom range…"}, "value": context_json})
    blocks.append({"type": "actions", "elements": elements})
    return blocks


# --- step handlers ---------------------------------------------------

def start_flow(response_url: str):
    """Background: list orgs, then replace the ack message with the org picker."""
    try:
        orgs = adx.list_organizations()
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        post_response(response_url, text=(
            f":warning: Couldn't list organizations ({status}). This PAT is likely scoped to a single "
            "org rather than 'All accessible organizations', or is missing 'User Profile (Read)'. "
            "Generate a new PAT with organization set to 'All accessible organizations' and try again."
        ))
        return
    if not orgs:
        post_response(response_url, text=":warning: No organizations found for this PAT.")
        return
    options = [(o["accountName"], o["accountName"]) for o in orgs]
    post_response(response_url, blocks=blocks_select("Select an organization", [], "select_org", options))


def handle_select_org(org: str, response_url: str):
    try:
        projects = adx.list_projects(org)
    except requests.HTTPError as e:
        post_response(response_url, text=f":warning: Couldn't list projects for '{org}': {e}")
        return
    if not projects:
        post_response(response_url, text=f":warning: No projects found in '{org}'.")
        return
    options = [(p["name"], _store_ctx({"org": org, "project_id": p["id"], "project_name": p["name"]})) for p in projects]
    post_response(response_url, blocks=blocks_select(
        "Select a project", [f":office: Org: *{org}*"], "select_project", options,
    ))


def handle_select_project(ctx: dict, response_url: str):
    org, project_id, project_name = ctx["org"], ctx["project_id"], ctx["project_name"]
    try:
        users = adx.list_project_users(org, project_id)
    except requests.HTTPError as e:
        post_response(response_url, text=f":warning: Couldn't list users for '{project_name}': {e}")
        return
    if not users:
        post_response(response_url, text=f":warning: No users found in '{project_name}'.")
        return
    options = [
        (u["displayName"], _store_ctx({"org": org, "project_name": project_name, "unique_name": u["uniqueName"], "display_name": u["displayName"]}))
        for u in users
    ]
    post_response(response_url, blocks=blocks_select(
        "Select a person", [f":office: Org: *{org}*", f":file_folder: Project: *{project_name}*"],
        "select_user", options,
    ))


def handle_select_user(ctx: dict, response_url: str):
    org, project_name, display_name = ctx["org"], ctx["project_name"], ctx["display_name"]
    breadcrumb = [f":office: Org: *{org}*", f":file_folder: Project: *{project_name}*", f":bust_in_silhouette: Person: *{display_name}*"]
    post_response(response_url, blocks=blocks_date_buttons(breadcrumb, json.dumps(ctx)))


def generate_and_post(ctx: dict, response_url: str, channel_id: str):
    """Background: run the report for a fully-resolved context and post the result."""
    date_tokens = [ctx["date_token"]] if "date_token" in ctx else [ctx["since"], ctx["until"]]
    result = adx.run_quiet(ctx["unique_name"], date_tokens, org=ctx["org"], project_name=ctx["project_name"])

    if not result.get("ok"):
        post_response(response_url, text=f":warning: Status report failed: {result.get('error', 'unknown error')}", replace_original=True)
        return

    header = (
        f"*Status report — {result['user']} · {result['project']} · {result['date_label']}*\n"
        f"{result['ticket_count']} ticket(s) with activity."
    )
    post_response(response_url, text=f"{header}\n\n{result['summary']}", replace_original=True, in_channel=True)

    pdf_path = result.get("pdf_path")
    if SLACK_BOT_TOKEN and channel_id and pdf_path:
        try:
            upload_pdf_to_slack(channel_id, pdf_path, title=f"Ticket report - {result['user']} - {result['date_label']}")
        except Exception as e:
            post_response(response_url, text=f":warning: Report generated, but PDF upload failed: {e}", replace_original=False, in_channel=True)


def upload_pdf_to_slack(channel_id: str, pdf_path: str, title: str):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    filename = os.path.basename(pdf_path)
    filesize = os.path.getsize(pdf_path)

    step1 = requests.post(f"{SLACK_API}/files.getUploadURLExternal", headers=headers,
                           data={"filename": filename, "length": filesize}, timeout=15).json()
    if not step1.get("ok"):
        raise RuntimeError(f"files.getUploadURLExternal failed: {step1}")

    with open(pdf_path, "rb") as f:
        requests.post(step1["upload_url"], files={"file": f}, timeout=60).raise_for_status()

    step3 = requests.post(f"{SLACK_API}/files.completeUploadExternal", headers=slack_headers(),
                           json={"files": [{"id": step1["file_id"], "title": title}], "channel_id": channel_id},
                           timeout=15).json()
    if not step3.get("ok"):
        raise RuntimeError(f"files.completeUploadExternal failed: {step3}")


def open_custom_range_modal(trigger_id: str, ctx: dict, response_url: str, channel_id: str):
    view = {
        "type": "modal",
        "callback_id": "custom_range_submit",
        "private_metadata": json.dumps({**ctx, "response_url": response_url, "channel_id": channel_id}),
        "title": {"type": "plain_text", "text": "Custom date range"},
        "submit": {"type": "plain_text", "text": "Generate"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "since_block", "label": {"type": "plain_text", "text": "From"},
             "element": {"type": "datepicker", "action_id": "since", "placeholder": {"type": "plain_text", "text": "Select date"}}},
            {"type": "input", "block_id": "until_block", "label": {"type": "plain_text", "text": "To"},
             "element": {"type": "datepicker", "action_id": "until", "placeholder": {"type": "plain_text", "text": "Select date"}}},
        ],
    }
    requests.post(f"{SLACK_API}/views.open", headers=slack_headers(), json={"trigger_id": trigger_id, "view": view}, timeout=10)


# --- endpoints ---------------------------------------------------

@app.route("/slack/status-report", methods=["POST"])
def status_report():
    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 401
    response_url = request.form.get("response_url", "")
    threading.Thread(target=start_flow, args=(response_url,), daemon=True).start()
    return jsonify({"response_type": "ephemeral", "text": ":hourglass_flowing_sand: Loading organizations..."})


@app.route("/slack/interactivity", methods=["POST"])
def interactivity():
    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 401

    payload = json.loads(request.form.get("payload", "{}"))
    ptype = payload.get("type")

    if ptype == "block_actions":
        action = payload["actions"][0]
        action_id = action["action_id"]
        response_url = payload.get("response_url", "")
        channel_id = (payload.get("channel") or {}).get("id", "")

        if action_id == "select_org":
            org = action["selected_option"]["value"]
            threading.Thread(target=handle_select_org, args=(org, response_url), daemon=True).start()

        elif action_id == "select_project":
            try:
                ctx = _load_ctx(action["selected_option"]["value"])
            except KeyError:
                post_response(response_url, text=":warning: This picker expired -- run `/status-report` again.")
                return "", 200
            threading.Thread(target=handle_select_project, args=(ctx, response_url), daemon=True).start()

        elif action_id == "select_user":
            try:
                ctx = _load_ctx(action["selected_option"]["value"])
            except KeyError:
                post_response(response_url, text=":warning: This picker expired -- run `/status-report` again.")
                return "", 200
            threading.Thread(target=handle_select_user, args=(ctx, response_url), daemon=True).start()

        elif action_id == "custom_range":
            ctx = json.loads(action["value"])
            # Must call views.open synchronously -- trigger_id expires in ~3s.
            open_custom_range_modal(payload["trigger_id"], ctx, response_url, channel_id)

        elif action_id.startswith("date_"):
            ctx = json.loads(action["value"])
            post_response(response_url, text=f":hourglass_flowing_sand: Generating report for *{ctx['display_name']}*...", replace_original=True)
            threading.Thread(target=generate_and_post, args=(ctx, response_url, channel_id), daemon=True).start()

        return "", 200

    if ptype == "view_submission" and payload["view"]["callback_id"] == "custom_range_submit":
        meta = json.loads(payload["view"]["private_metadata"])
        values = payload["view"]["state"]["values"]
        since = values["since_block"]["since"]["selected_date"]
        until = values["until_block"]["until"]["selected_date"]
        if not since or not until:
            return jsonify({"response_action": "errors", "errors": {"since_block": "Pick both dates."}})
        response_url = meta.pop("response_url")
        channel_id = meta.pop("channel_id", "")
        ctx = {**meta, "since": since, "until": until}
        post_response(response_url, text=f":hourglass_flowing_sand: Generating report for *{ctx['display_name']}*...", replace_original=True)
        threading.Thread(target=generate_and_post, args=(ctx, response_url, channel_id)).start()
        return jsonify({"response_action": "clear"})

    return "", 200


if __name__ == "__main__":
    if not os.environ.get("ADO_PAT"):
        sys.exit("ADO_PAT must be set (the webhook lists orgs/projects/users live).")
    app.run(port=int(os.environ.get("PORT", 3000)))