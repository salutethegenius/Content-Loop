# Content Loop Agent

Brand-agnostic content loop. FastAPI on Railway, Postgres on Railway, Slack approval flow, Claude API for drafting. V1 is text-only: drafts land in Slack for Approve/Reject, approved items sit in `content_items` with `status = 'approved'` and you post them manually. No Meta publishing, no image generation yet.

## Architecture

Brands are plug-ins under `app/brands/{brand}/`. Adding a brand means adding a folder with `config.json` and `voice.md`. Removing one means deleting the folder or setting `active: false`. Nothing in `app/core/` or `app/routes/` references a brand name directly.

```mermaid
flowchart LR
    Cron["Railway cron POST /cron/generate (X-Cron-Secret)"] --> Loader["get_active_brands()"]
    Loader --> Due{"is_due_for_post()"}
    Due -- yes --> Gen["generate_draft() via Claude"]
    Due -- no --> Skip[skip]
    Gen --> DB["save_draft() -> item_id"]
    DB --> Slack["post_for_approval() -> ts"]
    Slack --> UpdateDB["update_status(pending_approval, ts)"]
    Slack --> User["Reviewer clicks Approve/Reject"]
    User --> Interact["POST /slack/interactions (Slack sig verified)"]
    Interact --> UpdateDB2["update_status(approved|rejected)"]
```

## Layout

```
app/main.py
app/brands/{kgc,juber,bcu}/{config.json,voice.md}
app/core/{brand_loader,db,generator,slack_client}.py
app/routes/{cron,slack_interactions}.py
schema.sql
```

## Environment variables

Copy `.env.example` to `.env` locally and set in Railway variables.

| Var | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude API key |
| `SLACK_BOT_TOKEN` | Slack bot token with `chat:write` |
| `SLACK_SIGNING_SECRET` | Slack app signing secret, used to verify interaction payloads |
| `SLACK_CONTENT_CHANNEL` | Channel where drafts are posted for approval |
| `DATABASE_URL` | Railway Postgres URL |
| `CRON_SECRET` | Shared secret. Railway cron must send `X-Cron-Secret: <value>` on `/cron/generate`. Generate this yourself, do not let Cursor pick one. |
| `ANTHROPIC_MODEL` | Optional. Defaults to `claude-sonnet-4-6`. |

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in values
psql "$DATABASE_URL" -f schema.sql
uvicorn app.main:app --reload --port 8000
```

## Manual setup checklist (steps the code does not do for you)

1. **Database** - run `schema.sql` on Railway Postgres: `psql "$DATABASE_URL" -f schema.sql`.
2. **Slack app** - create a Slack app, give the bot `chat:write`, invite the bot to the approval channel, enable Interactivity, and point the Request URL at `https://{railway-domain}/slack/interactions`. Copy the signing secret into `SLACK_SIGNING_SECRET`.
3. **Railway deploy** - connect this repo, set all env vars, deploy.
4. **Smoke test** - manually POST `/cron/generate` with header `X-Cron-Secret: <value>` and confirm a draft appears in Slack and Approve/Reject update `content_items.status`.
5. **Railway cron** - in the Railway dashboard, add a cron trigger that hits `POST /cron/generate` with the `X-Cron-Secret` header on your chosen schedule.

## V1 done state

Approved items sit in `content_items` with `status = 'approved'`. You post them by hand. That is the whole V1 loop.

## V2 hooks (not built)

- `/publish` endpoint that selects approved items and pushes to Meta Graph API or the Pipeboard Meta Ads MCP tool.
- Image generation wired through Auto mode (routes between Grok, Gemini, ChatGPT) into `generator.generate_image`.

## Open items for Kenneth

- Confirm posting cadence per brand. Currently defaulted to `3` days in each `config.json`.
- Decide whether Edit reopens a Slack modal or picks up a thread reply. V1 ships Approve/Reject only.
- Image generation: stub now, wire in a later pass.
