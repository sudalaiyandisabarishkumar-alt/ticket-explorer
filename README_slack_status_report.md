# `/status-report` Slack slash command — guided picker

The manager just runs:

```
/status-report
```

No arguments. Slack shows a dropdown of every **organization** the ADO PAT
can see → picking one shows a dropdown of that org's **projects** → picking
one shows a dropdown of that project's **users** → picking one shows
date-range buttons (**Today / Yesterday / This week / Last week / Custom
range**). Once a range is picked, the report generates and the summary +
PDF land in the channel.

Nothing is pinned via env vars — org, project, and person are all resolved
live from Azure DevOps and chosen interactively, every time.

## Files

| File | Purpose |
|---|---|
| `ado_ticket_explorer.py` | Interactive terminal script, plus `run_quiet(user, dates, org=..., project_name=...)` / `--quiet --org --project <user> [dates]` for programmatic use. Org/project are now function arguments, not required env vars (env vars still work as a fallback for quick CLI/cron use only). |
| `slack_status_report_webhook.py` | Flask app with two routes: `/slack/status-report` (starts the flow) and `/slack/interactivity` (handles every dropdown pick, button click, and the custom-range modal). |
| `.github/workflows/status-report.yml` | Optional, separate from the Slack flow: a manual/scheduled way to run a report with org/project as typed workflow inputs. |

## How the flow stays stateless

Each step's context (chosen org, then org+project, then org+project+user)
rides inside the Slack message/interaction itself — embedded as JSON in the
next step's dropdown option values or button values — rather than being
stored server-side. That means the webhook can run as multiple stateless
instances behind a load balancer with no shared session store.

## Slack app setup

1. Create a Slack app → **OAuth & Permissions** → add bot scopes
   `chat:write` and `files:write` → install to workspace → copy the
   **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.
2. **Basic Information** → copy the **Signing Secret** into
   `SLACK_SIGNING_SECRET`.
3. **Slash Commands** → create `/status-report` → Request URL:
   `https://<your-host>/slack/status-report`.
4. **Interactivity & Shortcuts** → turn it on → Request URL:
   `https://<your-host>/slack/interactivity`.
   *(This second URL is new versus a typed-argument version of the command —
   it's what makes the dropdowns and the custom-range modal work.)*

## Azure DevOps PAT requirements

The webhook lists organizations itself now, so the PAT **must** be created
with organization scope set to **"All accessible organizations"** (not a
single org), plus scopes **Identity (Read)**, **User Profile (Read)**,
**Project and Team (Read)**, **Work Items (Read)**. If it's scoped wrong,
the org-picker step will show a warning explaining exactly that.

## Running it

```bash
pip install flask requests reportlab
export SLACK_SIGNING_SECRET=...
export SLACK_BOT_TOKEN=xoxb-...
export ADO_PAT=...
export GROQ_API_KEY=...        # optional -- falls back to a deterministic summary if unset
python slack_status_report_webhook.py
```

Expose it (`ngrok http 3000` while testing, a real host for production) and
point **both** Slack request URLs from step 3–4 at it.

## Testing

1. `/status-report` in any channel the bot is in.
2. Ephemeral reply: *"⏳ Loading organizations..."*, then a dropdown.
3. Pick org → project → person → date range (or Custom range → a modal with
   two date pickers).
4. In-channel message with the summary, followed by the PDF.

If a step shows a `:warning:` instead of moving forward, the message text
explains what failed (bad PAT scope, no projects/users found, an Azure
DevOps request error, etc.) — no separate log-diving needed for the common
cases.

## Limits worth knowing

- Slack `static_select` menus cap at **100 options**. If an org/project has
  more organizations/projects/users than that, the list gets truncated (with
  a visible warning) rather than failing outright. For very large
  organizations, this is the first thing to revisit — e.g. swapping the
  project or user dropdown for an `external_select` with server-side search.
- Each interaction step calls Azure DevOps live, so picking through org →
  project → user takes a few seconds per step (network-bound, not
  artificial delay).

## Optional: manual/cron report without Slack

```bash
python ado_ticket_explorer.py --quiet --org "Your Org" --project "Your Project" Tarun last-week
```

Or trigger `.github/workflows/status-report.yml` manually from the Actions
tab, filling in org/project/user/date-range as inputs — useful for a
one-off report or a scheduled digest, independent of the Slack picker.
