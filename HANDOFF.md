# Content Loop Agent — Session Handoff

**Last updated:** 2026-07-06 (V2 + 1.5.0 onboarding gate + 1.6 image gen + 1.7 design-system pivot)
**Repo:** https://github.com/salutethegenius/Content-Loop (private)
**Live deployment:** https://nova-production-14f6.up.railway.app
**Railway project:** `verityos-agents` (ID `47ab2c83-6fc9-42e7-9a12-92c98552c2ea`), service `nova`, environment `production`

Read this first if you are picking up the project in a new session. It covers what is built, what is live, gotchas already solved, and what to build next (V2).

---

## 1. What this project is

Brand-agnostic content loop. FastAPI on Railway, Postgres on Railway, Slack approval flow, Claude API for drafting. "Nova" is the agent persona. Brands are plug-ins.

Three capabilities are live:
1. **Interactive generation** — admin hits `/generate/start`, Nova posts a Slack brand picker → platform picker → generates targeted drafts. Preferred manual path.
2. **Content loop (cron)** — cron hits `/cron/generate`, generates drafts for all due active brand/platform pairs. Blanket scheduled fallback; respects cadence.
3. **Nova onboarding** — admin hits `/onboard/start`, Nova opens a Slack thread and walks a business through a 6-phase interview, synthesizes `voice.md` + `config.json` via Claude, posts the draft with Approve/Reject/Regenerate buttons. Approve persists the brand to the `brands` table and it is immediately live (no file write, no redeploy).

V1 approval flow: drafts land in Slack with Approve/Reject buttons. On click, the message updates in place (buttons removed, status shown) and `content_items.status` is set to `approved` or `rejected`. Kenneth posts approved copy manually. No Meta publishing, no image generation yet.

---

## 2. Repo layout

```
/app/main.py                      FastAPI app, sys.path shim for `from core/` imports
/app/brands/{kgc,biccu,drewber}/  filesystem seed brands (config.json + voice.md)
/app/core/brand_loader.py         get_active_brands / get_brand_by_id / load_voice / is_due_for_post
/app/core/content_loop.py         shared generate-for-platforms + blanket cron loop
/app/core/generation_flow.py      interactive Slack brand/platform picker + handlers
/app/core/db.py                   psycopg2 helpers: content_items, brands, onboarding_sessions
/app/core/generator.py            Claude draft generation, pillar-based topic selection, Facebook support
/app/core/onboarding.py           6-phase interview script + Claude synthesis of voice/config
/app/core/slack_client.py         post_for_approval, post_message, format_resolved_approval_blocks
/app/routes/cron.py               POST /cron/generate (X-Cron-Secret gated)
/app/routes/generate.py           POST /generate/start (X-Cron-Secret gated)
/app/routes/onboarding.py         POST /onboard/start (X-Cron-Secret gated)
/app/routes/slack_events.py       POST /slack/events (Slack Events API, signed)
/app/routes/slack_interactions.py POST /slack/interactions (all button clicks, signed)
schema.sql                        content_items + brands + onboarding_sessions
```

Key design rule: **nothing in `/app/core` or `/app/routes` references a brand name directly.** Brands are plug-ins. Adding one = onboarding flow → row in `brands` table (or a folder under `app/brands/`).

---

## 3. Endpoints (all live)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/` | none | health check |
| POST | `/generate/start` | `X-Cron-Secret` header | open interactive Slack flow: pick brand → pick platforms → generate |
| POST | `/cron/generate` | `X-Cron-Secret` header | blanket loop for all due active brands/platforms (respects cadence) |
| POST | `/onboard/start` | `X-Cron-Secret` header | open a Nova onboarding thread; body `{brand_id, display_name, channel}` |
| POST | `/slack/events` | Slack HMAC signature | Slack Events API: `url_verification` + `message.groups` (private channel) |
| POST | `/slack/interactions` | Slack HMAC signature | button clicks: content Approve/Reject, **Publish now / Schedule (V2)**, onboarding, interactive generation |
| POST | `/slack/commands` | Slack HMAC signature | V2: `/nova` slash command — posts brand picker to `#nova-agent` from inside Slack |
| POST | `/publish` | `X-Cron-Secret` header | V2: publish or schedule an approved content item to its Facebook page. Body `{item_id, scheduled_for?}` |
| GET | `/meta/verify` | `X-Cron-Secret` header | V2: verify `META_PAGE_ACCESS_TOKEN` works against `META_PAGE_ID` |

All Slack-signed endpoints verify HMAC-SHA256 with `SLACK_SIGNING_SECRET` and reject >5min-old timestamps (replay protection).

---

## 4. Database (Railway Postgres)

**Connection:** `DATABASE_URL` is wired on the `nova` service as a reference variable `${{Postgres.DATABASE_URL}}`. Public psql access for manual ops:

```bash
PGPASSWORD='<see Railway dashboard — do not commit>' \
  psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway
```

**Tables:**
- `content_items` — generated drafts. `status` flows `pending_approval` → `approved`/`rejected` → (V2) `posted`.
- `brands` — DB-backed brand configs + voice_md. Onboarding output lands here. `brand_loader` reads this first, falls back to `app/brands/` on disk.
- `onboarding_sessions` — in-flight onboarding conversations. State survives restarts. `phase` walks `identity → voice → content → compliance → platform → cadence → awaiting_approval → done`.

**Schema file:** `schema.sql` at repo root. Run with `psql "$DATABASE_URL" -f schema.sql` (idempotent — all `IF NOT EXISTS`).

---

## 5. Environment variables (Railway `nova` service)

| Var | Notes |
| --- | --- |
| `ANTHROPIC_API_KEY` | set by Kenneth |
| `ANTHROPIC_MODEL` | optional, defaults to `claude-sonnet-4-6` |
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `SLACK_SIGNING_SECRET` | for HMAC verification of events + interactions |
| `SLACK_CONTENT_CHANNEL` | `C0BF8QKP0PL` (private channel named `nova-agent`) |
| `DATABASE_URL` | reference var `${{Postgres.DATABASE_URL}}` — if it ever shows empty, re-set it (`railway variables set 'DATABASE_URL=${{Postgres.DATABASE_URL}}'`) |
| `CRON_SECRET` | `<see Railway dashboard — do not commit>` — required header for `/generate/start`, `/cron/generate`, `/onboard/start`, `/publish`, `/meta/verify` |
| `META_PAGE_ID` | `120368170965` (BICCU Facebook page) — V2 |
| `META_PAGE_ACCESS_TOKEN` | long-lived Page Access Token with `pages_manage_posts` scope — V2 |
| `META_API_VERSION` | `v23.0` (optional) — V2 |

**Gotcha:** setting env vars via the Railway dashboard can wipe the `DATABASE_URL` reference. Always re-check `railway variables | grep DATABASE_URL` after env edits and re-wire if blank.

---

## 6. Slack app configuration (Nova-Agent)

The one-time Slack setup that is already done. Do not redo unless something breaks.

- **App name:** Nova-Agent, bot user `novaagent`
- **Bot Token Scopes:** `chat:write`, `channels:history`, `channels:read`, `groups:history`, `groups:read`
- **Interactivity:** On, Request URL `https://nova-production-14f6.up.railway.app/slack/interactions`
- **Event Subscriptions:** On, Request URL `https://nova-production-14f6.up.railway.app/slack/events`
- **Subscribed bot events:** `message.channels` (public channels) AND `message.groups` (private channels — this one is critical, see gotcha below)
- **Bot membership:** invited to the private channel `C0BF8QKP0PL` (`nova-agent`)

### Critical gotcha: private channels need `groups:*` scopes + `message.groups` event

The approval channel `C0BF8QKP0PL` is **private**. Slack uses a completely different scope/event family for private channels:
- Public: `channels:history` / `channels:read` / `message.channels`
- Private: `groups:history` / `groups:read` / `message.groups`

If thread replies stop driving onboarding, check (a) the bot is still in the channel, (b) `groups:history` is still on the token, (c) `message.groups` is still subscribed. Verify with:
```bash
SLACK_BOT_TOKEN='<from Railway>' curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  "https://slack.com/api/conversations.info?channel=C0BF8QKP0PL" | python3 -m json.tool
```
Should return `ok: true`, `is_member: true`. If it returns `missing_scope` with `needed: groups:read`, the token lost the scope — re-add and reinstall the app.

### Critical gotcha: every button in a message needs a unique `action_id`

Slack rejects block messages where two buttons share the same `action_id` (`invalid_blocks`). The generation pickers use per-brand/per-platform ids like `gen_pick_brand_biccu` and `gen_confirm_biccu_fb_ig`. Do not collapse these back to a single shared id.

---

## 7. What is done and verified live

### Item 1 — Nova Slack onboarding flow: COMPLETE
- 6-phase interview: identity → voice → content territory → compliance → platform behavior → cadence.
- Human replies in thread; types `next` to advance. Nova accumulates replies per phase in `onboarding_sessions.answers` (JSONB).
- After Phase 6 + `next`, Claude synthesizes `voice.md` + `config.json` (with `content_pillars` array) and posts the draft with Approve/Reject/Regenerate buttons.
- Approve → `db.upsert_brand()` → row in `brands` table → immediately live in `get_active_brands()`.
- Regenerate re-runs Claude on accumulated answers (so the human can reply with corrections then click Regenerate).
- **Proven on BICCU.** See section 9 for the onboarding outcome.

### Item 2 — content_pillars generator change: COMPLETE
- `generator.pick_pillar(brand_config, platform)` selects a pillar per run via date-based rotation: `index = date.toordinal(today) % len(pillars)`, with per-platform offsets (facebook +0, instagram +1, linkedin +2) so same-day cross-platform drafts cover different pillars.
- `generate_draft` passes the pillar name + description as the user-prompt topic, with explicit instructions to stay in-voice and not invent products/rates/promotions.
- Brands without `content_pillars` (Drewber, KGC — not yet onboarded) fall back to a strengthened prompt that tells Claude to invent a topic in-voice instead of asking for a brief.
- **Proven:** BICCU drafts anchor to specific pillars instead of "I don't have a topic."

### Item 3 — Interactive targeted generation: COMPLETE (verified 2026-07-05)
- `POST /generate/start` posts a brand picker to `#nova-agent`.
- Button flow: `gen_pick_brand_{brand_id}` → platform picker in thread → `gen_confirm_{brand_id}_{preset}` runs generation in a background task.
- Skips cadence checks (manual runs always generate). Drafts still post to the approval channel via `post_for_approval`.
- Platform defaults: `["facebook", "instagram"]` with LinkedIn opt-in. `generator.py` has Facebook formatting hints.
- **Proven:** BICCU Facebook draft generated and approved end-to-end (content item id 13).

### Item 4 — Slack approval UX: COMPLETE
- Approve/Reject updates `content_items.status` in Postgres **and** replaces the Slack message in place (buttons removed, ✅ Approved / ❌ Rejected status line with user mention).
- Prior bug: handler returned `{"ok": true}` only — DB updated but Slack looked unchanged. Fixed in `8e87f12`.

### Brand folder renames: COMPLETE
- `app/brands/bcu` → `biccu`, `app/brands/juber` → `drewber`. Configs + voice.md headers updated. `kgc` unchanged.

---

## 8. Current brand state

| brand_id | display_name | source | platforms | has_pillars | notes |
| --- | --- | --- | --- | --- | --- |
| `biccu` | BICCU | DB (onboarded) | facebook, instagram, linkedin | yes, 7 | credit union, cadence 2 days, fully onboarded via Nova |
| `drewber` | Drewber Solutions | filesystem seed | facebook, instagram | no | not yet onboarded (picker now prompts to onboard — see section 16) |
| `kgc` | Kemis Group of Companies | filesystem seed | facebook, instagram | no | not yet onboarded (picker now prompts to onboard — see section 16) |

Drewber and KGC still need to be onboarded through Nova before they generate on-voice, pillar-anchored drafts. Run `/nova` in Slack → click the brand → **Onboard now** (new as of 1.5.0), or hit `/onboard/start` with the right `brand_id` and channel.

### 8.1 Adding a new brand — three options

None of these require a code change to `core/` or `routes/`. Brands are plug-ins.

**Option A — Onboard through Nova (intended path).** Two entry points:
- From Slack: `/nova` → click the brand → **Onboard now** (works only after a seed folder exists, see Option B).
- From the admin endpoint (works with or without a seed folder):
  ```bash
  curl -X POST -H "X-Cron-Secret: <see Railway dashboard — do not commit>" -H "Content-Type: application/json" \
    -d '{"brand_id":"acme","display_name":"Acme Co","channel":"C0BF8QKP0PL"}' \
    https://nova-production-14f6.up.railway.app/onboard/start
  ```
  Nova opens a thread in `#nova-agent`, walks the 6 phases, Claude synthesizes voice.md + config.json (with `content_pillars`), you Approve → row lands in `brands` → immediately live.

**Option B — Drop a filesystem seed folder** (so the brand shows up in the `/nova` picker):
- Create `app/brands/{brand_id}/config.json`:
  ```json
  {
    "brand_id": "acme",
    "display_name": "Acme Co",
    "active": true,
    "platforms": ["facebook", "instagram"],
    "posting_cadence_days": 3,
    "image_style_prompt": "clean, modern, neutral palette"
  }
  ```
- Optionally `app/brands/{brand_id}/voice.md` (stub is fine; onboarding overwrites with the DB version).
- `get_active_brands()` reads the filesystem at request time, so a redeploy is needed for a *new* folder to appear. After redeploy, the brand appears in the `/nova` picker; clicking it triggers the "needs onboarding" prompt → **Onboard now** runs Option A. This is the cleanest workflow: seed folder → `/nova` → onboard → live.

**Option C — Direct DB insert** (not recommended, only if you already have a voice.md from elsewhere):
- `INSERT INTO brands (brand_id, config, voice_md) VALUES (...)` directly. Bypasses Nova's interview and Claude synthesis — you hand-write voice.md + config (including `content_pillars`). The normal onboarding flow does this for you via `db.upsert_brand`.

### 8.2 What you cannot do today (deferred)

- **Add a brand purely from Slack with no seed folder and no curl.** The `/nova` picker only lists brands that already exist in the DB or on disk — there is no "create new brand" button. A future `/nova onboard <brand_id> <display_name>` subcommand (or a button on the picker that opens a "what's the brand id + display name?" dialog) calling `onboarding.start_session` would close this gap. ~20 lines on top of 1.5.0.
- **Per-brand Facebook page.** `META_PAGE_ID` is a single env var (BICCU's page). To publish for a second brand's Facebook page, store `meta_page_id` in each brand's config and pass it through `meta_publisher` (already flagged in section 12).

---

## 9. BICCU onboarding outcome (reference example)

Onboarded via Nova in the `nova-agent` Slack channel. Session id 1, status `approved`, phase `done`. Stored in `brands` table:

- **display_name:** BICCU
- **platforms:** facebook, instagram, linkedin
- **posting_cadence_days:** 2
- **image_style_prompt:** "clean, professional and community-focused imagery using BICCU's corporate color palette of cyan blue, deep navy, warm earth tones, high visibility orange and clean white, featuring authentic Bahamian people and everyday life whenever possible."
- **content_pillars (7):** Member Success Stories, Financial Wellness and Education, Products That Solve Everyday Problems, Life Milestones, Community and Member Engagement, Empowering Membership, Announcements and Service Updates.
- **voice.md:** credit-union voice, "trusted neighbor who happens to know a lot about money," formality 3/5, warm humor, em-dashes banned, banned phrases list (revolutionary, game changing, get rich, etc.), no competitor mentions, no politics/religion, compliance-adjacent content flagged for approval. ~1081 chars.

Sample pillar-anchored drafts are in `content_items`. First Facebook draft approved live: item id **13** (`biccu` / `facebook`, status `approved`, 2026-07-05).

---

## 10. Common operations

```bash
# Link Railway to the nova service (if cwd link is stale)
cd /Users/ghost/Desktop/ORG/agents/nova/content-loop
railway link --project 47ab2c83-6fc9-42e7-9a12-92c98552c2ea --service nova --environment production

# Run schema updates on Postgres
PGPASSWORD='<see Railway dashboard — do not commit>' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway -f schema.sql

# Trigger the interactive generation flow (preferred manual path)
curl -X POST -H "X-Cron-Secret: <see Railway dashboard — do not commit>" \
  https://nova-production-14f6.up.railway.app/generate/start

# Trigger the blanket cron loop (all due brands/platforms)
curl -X POST -H "X-Cron-Secret: <see Railway dashboard — do not commit>" https://nova-production-14f6.up.railway.app/cron/generate

# Start an onboarding
curl -X POST -H "X-Cron-Secret: <see Railway dashboard — do not commit>" -H "Content-Type: application/json" \
  -d '{"brand_id":"drewber","display_name":"Drewber Solutions","channel":"C0BF8QKP0PL"}' \
  https://nova-production-14f6.up.railway.app/onboard/start

# Check deploy status + logs
railway deployment list
railway logs

# Check onboarding session state
PGPASSWORD='<see Railway dashboard — do not commit>' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
  -c "SELECT brand_id, phase, status FROM onboarding_sessions;"

# Check DB-backed brands + platforms
PGPASSWORD='<see Railway dashboard — do not commit>' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
  -c "SELECT brand_id, config->>'display_name', config->'platforms' FROM brands;"

# Check recent drafts + approval status
PGPASSWORD='<see Railway dashboard — do not commit>' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
  -c "SELECT id, brand, platform, status, approved_at FROM content_items ORDER BY id DESC LIMIT 10;"
```

Auto-deploy is on — pushing to `main` triggers a Railway rebuild. Setting env vars also triggers a redeploy.

**Recent commits (2026-07-05 session):**
- `63e9b1c` — interactive generation flow + Facebook platform support
- `92a8d5b` — fix unique Slack `action_id`s in generation pickers
- `8e87f12` — fix Approve/Reject to update Slack message in place

---

## 11. Interactive targeted generation — COMPLETE (verified 2026-07-05)

Kenneth's ask: Nova should ask who to generate for and which platforms, instead of blindly running all brands. Facebook + Instagram are primary; LinkedIn is optional.

### How it works

1. **`POST /generate/start`** posts a brand picker to `#nova-agent` (`C0BF8QKP0PL`).
2. User clicks a brand → Nova replies in-thread with platform buttons (Facebook + Instagram preset, individual platforms, all configured).
3. User confirms → background task generates drafts → each draft posts to the approval channel with Approve/Reject.
4. **`/cron/generate`** still exists for scheduled blanket runs (respects cadence).

### Slack interaction action ids

| action_id pattern | value | effect |
| --- | --- | --- |
| `gen_pick_brand_{brand_id}` | `brand_id` | post platform picker in thread |
| `gen_confirm_{brand_id}_fb_ig` | `brand_id:facebook,instagram` | generate primary pair |
| `gen_confirm_{brand_id}_{platform}` | `brand_id:platform` | generate one platform |
| `gen_confirm_{brand_id}_all` | `brand_id:facebook,instagram,...` | generate all configured |

### Decisions locked in

| Question | Choice |
| --- | --- |
| Replace cron or alongside? | **Alongside** — cron stays, interactive for manual runs |
| Multi-brand per run? | **One brand at a time** via buttons |
| Platform menu source? | **Brand config drives options** |
| Which channel? | **`SLACK_CONTENT_CHANNEL`** (`nova-agent`) |

### Optional follow-up

- Remove the Railway cron schedule if Kenneth wants interactive-only generation.

---

## 12. NEXT STEP — V2.1 (not built yet)

- **Image generation** — `generator.generate_image` is a stub returning `None`. Wire via Auto mode across Grok, Gemini, ChatGPT when ready. **Next session.**
- Instagram publishing — 2-step Graph flow (`POST /{ig-user-id}/media` then `POST /{ig-user-id}/media_publish`). Needs the IG business account id linked to the BICCU page. Deferred from V2.
- LinkedIn publishing — deferred from V2.
- Per-brand `meta_page_id` — currently a single `META_PAGE_ID` env var (BICCU only). To support multiple brands' Facebook pages, store `meta_page_id` in each brand's `config.json` / `brands` table row and pass it through `meta_publisher`.

---

## 13. Known small issues / polish for later

- The `content_items` table has no `pillar` column — currently you cannot tell which pillar a draft was anchored to. Consider adding `pillar TEXT` to `content_items` and having `save_draft` record it, for analytics/content planning.
- `onboarding_sessions.answers` accumulates raw message text per phase; if a human posts many messages, the synthesis prompt grows. Fine for now (6 phases, modest volume), but worth a length guard eventually.
- No per-session lock — two rapid replies in the same onboarding thread could race. Low risk at current volume.
- `append_onboarding_answer` uses `answers || jsonb_build_object(...)` which is safe but worth noting if answers ever get nested.
- Onboarding Phase 6 still asks cadence "per platform" in prose but config stores a single `posting_cadence_days` — fine for V1, may need per-platform cadence later.
- Drafts approved before `8e87f12` may still show stale Approve/Reject buttons in Slack even though the DB status is correct. Re-approve not needed; generate new drafts to see the updated UX.

---

## 14. V2 — Meta Graph API publish + schedule (COMPLETE 2026-07-05, verified live)

Approved Facebook drafts can be published immediately or scheduled to the brand's Facebook Page directly via the Meta Graph API. No MCP plugin — direct Graph calls. Instagram + LinkedIn are deferred (V2.1).

**Verified live 2026-07-05:**
- `/meta/verify` → `{"page_name": "Bahama Islands Co-operative Credit Union Limited"}`
- **Schedule test:** content item 13 scheduled via `POST /publish` with `scheduled_for` 25 min out. Meta returned post id `120368170965_1702865025176333`; confirmed present in the page's `scheduled_posts` edge. DB `status='scheduled'`, `meta_post_id` + `scheduled_for` populated.
- **Publish-now test (from Slack):** content item 15 published via the "Publish now" button on the approved draft message. DB `status='posted'`, `meta_post_id` set, `posted_at` set. Post live on the BICCU page.
- **Schedule test (from Slack):** content item 16 scheduled via "Schedule" → datetimepicker → "Confirm schedule". DB `status='scheduled'`, `meta_post_id` + `scheduled_for` set.

### Slack `/nova` slash command (V2)

`/nova` triggers the interactive generation flow from inside Slack (no curl needed). Configure in Slack app settings → Slash Commands → `/nova` → Request URL `https://nova-production-14f6.up.railway.app/slack/commands`.

| Command | Effect |
| --- | --- |
| `/nova` or `/nova generate` | post brand picker to `#nova-agent` |
| `/nova help` | ephemeral help text |

The brand picker lands in `SLACK_CONTENT_CHANNEL` so the rest of the flow (platform picker, drafts, approvals, publish buttons) stays in one channel.

### Bug fixes applied during V2 verification

1. **Approve/Reject silent failure** — the approve handler returned `replace_original` from an `async` endpoint doing synchronous DB calls. If the DB call pushed past Slack's 3s window, Slack dropped the message update (DB flipped to approved but Slack kept showing Approve/Reject). Fixed by switching to background-task + `chat.update` pattern.
2. **Publish button value parsing** — `publish_now_15` has two underscores but the handler used `split("_", 1)` (splits on first), giving `["publish", "now_15"]` → `int("now_15")` → ValueError → 400. Fixed with `rsplit("_", 1)` (splits on last).
3. **Schedule silent failure** — `publish_schedule` and `publish_confirm` used `replace_original` synchronously. Same timing issue as #1. Fixed by converting both to the background-task + `chat.update` pattern.
4. **Publish-now race condition** — handler returned `replace_original` with "Publishing..." AND started a background task with `chat.update` for the final status. The two could race, leaving the message stuck on "Publishing...". Fixed by converting to pure background-task pattern (no `replace_original`).
5. **Schedule picker hint persisted** — the "Pick a time to schedule..." section block stayed in the message after scheduling completed. Fixed by tagging it with `block_id: "schedule_picker_hint"` and stripping that block in `format_publish_result_blocks`.

**Pattern locked in:** all Slack interaction handlers now ack with `{"ok": True}` immediately and do DB + Graph API + message updates in `BackgroundTasks`. No handler relies on `replace_original` for critical state changes.

### Endpoints (new)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/publish` | `X-Cron-Secret` header | Publish (or schedule) an approved content item to its Facebook page. Body `{item_id, scheduled_for?}`. |
| GET | `/meta/verify` | `X-Cron-Secret` header | Sanity check that `META_PAGE_ACCESS_TOKEN` works against `META_PAGE_ID`. Returns the page name. |

### Slack UX

After clicking **Approve** on a facebook draft, the message now shows two extra buttons:
- **Publish now** → background task calls Meta Graph `POST /{page_id}/feed`, sets `status='posted'`, `posted_at`, `meta_post_id`. Message flips to ":rocket: Published to Facebook. Meta post id: `...`".
- **Schedule** → swaps in a Slack `datetimepicker` + **Confirm schedule** button. On confirm, background task calls Meta with `published=false` + `scheduled_publish_time`, sets `status='scheduled'`, `scheduled_for`, `meta_post_id`. Meta publishes the post automatically at the requested time (10 min - 30 days out, enforced by `meta_publisher.MIN_SCHEDULE_OFFSET_SEC` / `MAX_SCHEDULE_OFFSET_SEC`). No cron needed.

Non-facebook approved drafts keep the pre-V2 "ready for manual posting" line (no publish buttons). The publish endpoints/handlers refuse non-facebook items with a clear error.

### Files added/changed (V2)

```
schema.sql                              + meta_post_id TEXT, published_via TEXT DEFAULT 'meta_graph'
app/core/meta_publisher.py              publish_page_post / schedule_page_post / verify_token
app/core/slack_verify.py                shared HMAC-SHA256 signature verifier (used by slash commands)
app/core/db.py                          get_content_item; update_status extended (meta_post_id, scheduled_for)
app/core/slack_client.py                format_resolved_approval_blocks now appends Publish/Schedule buttons
                                        + format_schedule_picker_blocks, format_publish_result_blocks, update_message
app/routes/publish.py                   POST /publish + GET /meta/verify (X-Cron-Secret gated)
app/routes/slack_commands.py            POST /slack/commands — /nova slash command
app/routes/slack_interactions.py        approve/reject + publish_now / publish_schedule / publish_confirm handlers
                                        (all background-task + chat.update pattern)
app/main.py                             publish_router + slack_commands_router registered; version 1.4.0
```

### Environment variables (new in V2)

| Var | Notes |
| --- | --- |
| `META_PAGE_ID` | `120368170965` (Bahama Islands Co-operative Credit Union Limited — the BICCU page) |
| `META_PAGE_ACCESS_TOKEN` | long-lived Page Access Token with `pages_manage_posts` + `pages_read_engagement` scope. Kenneth generates via Graph API Explorer (see section 15). Effectively permanent. |
| `META_API_VERSION` | `v23.0` (optional, defaults to v23.0) |

### Status flow

`pending_approval` → `approved` (Slack Approve) → `posted` (Publish now) **or** `scheduled` (Schedule). Meta publishes scheduled posts automatically at `scheduled_publish_time`; no follow-up cron required.

### Brand plug-in rule still holds

Nothing in `app/core/meta_publisher.py` hardcodes BICCU. The page id comes from `META_PAGE_ID`. To support a second brand's Facebook page later, store the page id in the brand's `config.json` (DB `brands` table) under e.g. `meta_page_id` and pass it through. The current single-env-var approach is a V2 shortcut for the one live brand.

---

## 15. Generating the META_PAGE_ACCESS_TOKEN (one-time setup)

The Pipeboard Meta Ads MCP connection is scoped to `ads_management` only — it cannot create organic page posts. For V2 publishing we use a long-lived **Page Access Token** generated directly via the Graph API Explorer.

1. Open https://developers.facebook.com/tools/explorer/ and pick your app.
2. **User or Page** dropdown → **User Token** → click **Generate Access Token**.
3. Check scopes: `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`. Authorize, picking the **Bahama Islands Co-operative Credit Union Limited** page.
4. Exchange the short-lived user token for a long-lived one:
   ```
   GET /oauth/access_token?grant_type=fb_exchange_token
       &client_id={APP_ID}&client_secret={APP_SECRET}
       &fb_exchange_token={SHORT_USER_TOKEN}
   ```
5. Switch the **User or Page** dropdown to **Page Token** and select the BICCU page. The token shown is the long-lived Page Access Token (effectively permanent — does not expire unless the app is removed or the page unlinks).
6. Set on Railway:
   ```bash
   railway variables set 'META_PAGE_ACCESS_TOKEN=<paste>'
   railway variables | grep DATABASE_URL   # gotcha: env edits can wipe the reference var
   ```
7. Verify: `curl -H "X-Cron-Secret: <see Railway dashboard — do not commit>" https://nova-production-14f6.up.railway.app/meta/verify` should return `{"ok": true, "page_name": "Bahama Islands Co-operative Credit Union Limited"}`.

### Token lifecycle note (2026-07-05)

The `META_PAGE_ACCESS_TOKEN` on Railway is **long-lived (never expires, `expires_at: 0`)** — generated via the `fb_exchange_token` flow + page-token re-fetch. App used is "Nova-Agent" (app id `27311849091810327`), token type PAGE, scopes `pages_show_list` + `pages_read_engagement` + `pages_manage_posts`. It only stops working if the app is removed from the page or the page unlinks the app.

To regenerate from scratch (e.g. for a second brand's page): see the steps in section 15.

---

## 16. Onboarding gate on the `/nova` brand picker (1.5.0, 2026-07-06)

**Bug fixed:** picking a not-yet-onboarded brand (Drewber, KGC) from the `/nova` brand picker used to silently fall through to the platform picker and generate with the degraded fallback prompt — no prompt to onboard. KGC had the same behavior. Now the picker intercepts non-onboarded brands and offers to onboard instead.

### What changed

- `db.is_onboarded(brand_id)` — True only if the brand has a row in `brands` (i.e. completed Nova onboarding and was approved). Filesystem-only seeds return False.
- `onboarding.start_session(brand_id, display_name, channel)` — extracted from the `/onboard/start` route so the Slack picker can kick off onboarding without an HTTP call. Posts the welcome message as a top-level message in the channel, posts phase 1 as the first threaded reply, persists the session row. The `/onboard/start` route now just calls this.
- `generation_flow.handle_pick_brand` — gated on `db.is_onboarded`. Non-onboarded brands get the new prompt (see below) instead of the platform picker. DB failures fall through to the platform picker so a DB outage doesn't block generation entirely.
- New action ids in `slack_interactions.py`: `gen_onboard_{brand_id}` (start onboarding from the picker) and `gen_force_pick_{brand_id}` (bypass the gate, used by the "Generate anyway" button). Per-brand suffixes keep action_ids unique (Slack rejects duplicates).

### New Slack UX for non-onboarded brands

Clicking a non-onboarded brand in the `/nova` picker posts in-thread:

> *{display_name}* hasn't been onboarded yet.
> Without onboarding, Nova has no voice.md or content pillars for this brand, so drafts will be generic and off-voice. Onboard now to set the voice, pillars, compliance rules, and cadence.
>
> **[Onboard now]**  **[Generate anyway]**

- **Onboard now** → opens a fresh top-level Nova onboarding thread in `#nova-agent` and posts a pointer reply in the picker thread.
- **Generate anyway** → posts the platform picker directly (preserves the previous fallback-prompt path behind an explicit click).
- BICCU still flows straight to the platform picker (it has a `brands` row).
- The cron loop (`/cron/generate`) is untouched — it still generates for all active brands including seed brands using the fallback prompt. Gating cron is a separate decision.

### Files changed (1.5.0)

```
app/core/db.py                      + is_onboarded(brand_id)
app/core/onboarding.py              + start_session(...) extracted from /onboard/start
app/core/generation_flow.py         handle_pick_brand gated; + format_onboard_prompt_blocks,
                                    + handle_force_pick_brand, + _post_platform_picker
app/routes/onboarding.py            refactored to call onboarding.start_session
app/routes/slack_interactions.py    + gen_onboard_* + gen_force_pick_* handlers,
                                    + _handle_onboard_from_picker background task
app/main.py                         version 1.5.0
```

### Open follow-ups

- Drewber and KGC still need to actually be onboarded (run `/nova` → click the brand → **Onboard now**, or `/onboard/start`).
- Optionally gate `/cron/generate` on onboarding too (currently still uses fallback prompt for seed brands).
- Optionally add `/nova onboard <brand_id> <display_name>` subcommand for zero-touch brand creation from Slack with no seed folder or curl (see section 8.2).

---

## 17. Image generation — design-system pivot (1.6 → 1.7, 2026-07-06)

### Why a design system instead of free generation

V1.6 asked Claude to write a full SVG per post. Output was inconsistent and mediocre (one test post rendered as a basic announcement card, not the Pentagram-style premium financial brand reference). V1.7 replaces free generation with a **fixed SVG template + curated illustration library**. Claude's job is reduced to filling structured JSON slots (headline lines, supporting paragraph, info card, CTA, illustration id), which it does reliably. Python composes the slots into the template, cairosvg rasterizes to PNG.

The template IS the brand visual identity. Iterating on the look = editing one SVG file + redeploying, not re-prompting Claude.

### Architecture

```
Draft text + brand config
   ↓
Claude slot-fill call (structured JSON output)
   ↓
JSON slots: headline_lines, headline_emphasis, supporting_paragraph,
            info_card_text, cta_text, illustration_id
   ↓
compose_svg() injects slots + selected illustration + footer data
into app/brands/{brand_id}/template.svg
   ↓
cairosvg.svg2png() → PNG bytes
   ↓
Save to /data/images, public URL → Slack + Meta /photos
```

### Files (1.7)

```
app/brands/biccu/template.svg             1080x1080 fixed layout, named tokens
app/brands/biccu/illustrations/*.svg      8 hand-coded <g> groups (piggy_bank,
                                          coins, shield, wallet, growth_arrow,
                                          house, savings_jar, dollar_icon)
app/brands/biccu/config.json              + design block (template path,
                                          illustrations_dir, default_illustration)
app/core/design_loader.py                 load_template, list_illustrations,
                                          load_illustration, get_footer_data
app/core/image_generator.py               rewritten: build_slot_prompt,
                                          generate_slots, compose_svg,
                                          generate_and_save orchestrates
app/core/onboarding.py                    Phase 7 + footer-data question;
                                          synthesis prompt emits footer +
                                          design blocks in config_json
app/main.py                               version 1.7.0
```

### Brand config schema additions

```json
{
  "visual_identity": {
    "colors": ["#0079C8", "#003C71", "#47B8E8", "#FFFFFF", "#F47A20"],
    "typography_style": "geometric sans-serif, bold for headlines, regular for body",
    "layout": "logo top-left, huge headline left, info card, CTA, illustration right, footer",
    "wordmark_text": "BICCU",
    "avoid": ["stock photos", "clipart", "people", "bevels", "glossy effects"]
  },
  "footer": {
    "website": "biccu.org",
    "phone": "(242) 601-5900",
    "tagline": "Building Stronger Together",
    "hashtag": "#CommunityFirst",
    "social": {"facebook": "BICCU", "instagram": "biccu", "linkedin": "biccu"}
  },
  "design": {
    "template": "template.svg",
    "illustrations_dir": "illustrations",
    "default_illustration": "piggy_bank"
  }
}
```

BICCU's DB row was hand-patched with all three blocks (one nested-`jsonb_set` UPDATE). Drewber and KGC will get them automatically when onboarded (the synthesis prompt now emits them).

### Template token scheme

The template uses Python `str.replace` tokens (double-brace). The composer in `image_generator.compose_svg` does the substitution:

| Token | Replaced with |
| --- | --- |
| `{{HEADLINE_LINE_1}}` .. `{{HEADLINE_LINE_4}}` | headline text (line 4 may be empty) |
| `{{HEADLINE_LINE_1_COLOR}}` .. `{{HEADLINE_LINE_4_COLOR}}` | `#0079C8` (emphasis) or `#003C71` (regular) |
| `{{SUPPORTING_PARA}}` | 1-2 sentence supporting paragraph |
| `{{INFO_CARD_TEXT}}` | one-sentence info card copy |
| `{{CTA_TEXT}}` | short CTA / question |
| `{{ILLUSTRATION_SVG}}` | inline `<g>` from the selected illustration file |
| `{{FOOTER_WEBSITE}}` `{{FOOTER_PHONE}}` `{{FOOTER_TAGLINE}}` `{{FOOTER_HASHTAG}}` | footer data |

### Illustration library

Each `app/brands/biccu/illustrations/{id}.svg` is a bare `<g>...</g>` (no `<svg>` wrapper) sized for a ~360x360 zone. The template's illustration zone wraps it: `<g transform="translate(620, 280) scale(1.0)">{{ILLUSTRATION_SVG}}</g>`. To add a new illustration: drop a new `.svg` file in the folder. Claude picks from the available ids automatically (with fallback to `default_illustration` if Claude's choice isn't found).

### Adding a new brand's design system (when Drewber/KGC onboard)

1. Run `/nova` → click the brand → **Onboard now** (or `/onboard/start`).
2. The onboarding synthesis now emits `visual_identity`, `footer`, and `design` blocks in `config_json`. Approve lands them in the `brands` table.
3. Hand-craft `app/brands/{brand_id}/template.svg` (clone BICCU's as a starting point and rebrand).
4. Hand-code `app/brands/{brand_id}/illustrations/*.svg` (clone BICCU's and recolor).
5. Redeploy. The brand's `Generate image` button now produces on-brand templated graphics.

### Cost

Per Generate image click: one Claude Sonnet call, ~500-1000 tokens output. ~$0.015-0.03 with Sonnet, ~$0.005 with Haiku. Override globally via `IMAGE_LLM_MODEL` env var or per-brand via `config.image_model`.

### Operational setup (carried over from 1.6)

- Railway volume `nova-volume` mounted at `/data/images` (50GB).
- `aptfile` at repo root installs `libcairo2`, `libpango-1.0-0`, `libpangocairo-1.0-0`, `libgdk-pixbuf2.0-0`, `libffi-dev` for cairosvg.
- `requirements.txt` includes `cairosvg>=2.7`.
- `IMAGE_BASE_URL` set to `https://nova-production-14f6.up.railway.app`.
- `GEMINI_API_KEY` left set on Railway in case the Gemini quota is topped up later (not currently used; the V1.6 Gemini path was removed in V1.7).

### Out of scope for V1.7

- ~~Real BICCU logo SVG~~ — DONE in V1.7. The real BICCU logo (globe + family + hands emblem) is base64-embedded in `template.svg`'s logo zone as a 240x240 optimized PNG rendered at 190x190. The source PNG is versioned at `app/brands/biccu/logo.png`. To update the logo: replace `logo.png`, re-encode to base64, and swap the data URI in `template.svg`.
- Per-brand templates for Drewber/KGC (architecture supports it; only BICCU ships in V1.7).
- Reference-image-as-prompt (the template IS the reference now).
- Auto-wrapping for the supporting paragraph (Claude is instructed to keep it to 1-2 sentences that fit the foreignObject zone; if it overflows, the foreignObject clips).

### Known V1.7 risks to watch

- ~~cairosvg + foreignObject~~ — CONFIRMED broken in the first live test; fixed in 1.7.1. All text zones now use `<text>` + `<tspan>` with Python-side wrapping (`_wrap_to_lines` / `_lines_to_tspans` in `image_generator.py`).
- **Font availability**: the template uses `Arial, Helvetica, sans-serif`. Railway's NIXPACKS base image has DejaVu Sans (a Helvetica-ish fallback) but not Arial itself. The text will render in DejaVu Sans — clean and professional, but not exactly Helvetica. To get closer to the reference, add `fonts-dejavu` or `ttf-mscorefonts-installer` to the aptfile.

### 1.7.1 layout hardening (post-first-live-test fixes)

The first live render exposed three bugs, all fixed:

1. **Headline overlapped logo + illustration.** Fixed 96px font blew past the left column with long lines like "SAVINGS MOMENT". Now `_fit_headline()` in `image_generator.py` computes the font size per render so the longest line always fits the 560px column (caps: 48-100px), and the template takes `HEADLINE_FONT_SIZE` / `HEADLINE_LINE_HEIGHT` / `HEADLINE_START_Y` as tokens. `_normalize_slots` also hard-splits any line a model returns over 16 chars, keeping the emphasis flag on both halves — so ANY model's slot output renders correctly.
2. **Footer text was blank on live.** Root cause: psycopg2 auto-decodes JSONB columns to dicts, so `json.loads(row[0])` in `db.get_brand_from_db` / `list_db_brands` threw `TypeError`, `get_active_brands` swallowed it and silently fell back to the filesystem seed config — which has no `footer` block. Fixed with `db._config_dict()` accepting both dicts and strings. (This also means live generation had been using the seed config, not the onboarded DB config — the fix restores DB-config precedence everywhere.)
3. **Dead vertical space + amateur footer.** Supporting paragraph now anchors bottom-up above the info card (`SUPPORTING_PARA_Y` token) so the gap is constant. Footer redesigned as a solid navy bar with an orange top rule, white text, light-blue glyphs — reads as a deliberate brand band instead of a floating white strip. CTA is single-line with auto-fitted font size (`CTA_FONT_SIZE` token) and ellipsis truncation at 46 chars. Supporting paragraph and info card get ellipsis when they exceed their line caps. Headlines are forced UPPERCASE at compose time for consistency.

The logo zone also switched from the full square logo PNG (which had baked-in text rendering too small) to the emblem-only crop + live SVG text for "BICCU / BAHAMA ISLANDS CO-OPERATIVE / CREDIT UNION LIMITED" — crisp at any size.

