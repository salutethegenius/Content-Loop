# Content Loop Agent — Session Handoff

**Last updated:** 2026-07-05
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
| POST | `/publish` | `X-Cron-Secret` header | V2: publish or schedule an approved content item to its Facebook page. Body `{item_id, scheduled_for?}` |
| GET | `/meta/verify` | `X-Cron-Secret` header | V2: verify `META_PAGE_ACCESS_TOKEN` works against `META_PAGE_ID` |

All Slack-signed endpoints verify HMAC-SHA256 with `SLACK_SIGNING_SECRET` and reject >5min-old timestamps (replay protection).

---

## 4. Database (Railway Postgres)

**Connection:** `DATABASE_URL` is wired on the `nova` service as a reference variable `${{Postgres.DATABASE_URL}}`. Public psql access for manual ops:

```bash
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' \
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
| `CRON_SECRET` | `massive-music-tech-issues` — required header for `/generate/start`, `/cron/generate`, `/onboard/start`, `/publish`, `/meta/verify` |
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
| `drewber` | Drewber Solutions | filesystem seed | facebook, instagram | no | uses fallback prompt until onboarded |
| `kgc` | Kemis Group of Companies | filesystem seed | facebook, instagram | no | uses fallback prompt until onboarded |

To onboard Drewber or KGC, hit `/onboard/start` with the right `brand_id` and channel, same flow as BICCU.

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
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway -f schema.sql

# Trigger the interactive generation flow (preferred manual path)
curl -X POST -H "X-Cron-Secret: massive-music-tech-issues" \
  https://nova-production-14f6.up.railway.app/generate/start

# Trigger the blanket cron loop (all due brands/platforms)
curl -X POST -H "X-Cron-Secret: massive-music-tech-issues" https://nova-production-14f6.up.railway.app/cron/generate

# Start an onboarding
curl -X POST -H "X-Cron-Secret: massive-music-tech-issues" -H "Content-Type: application/json" \
  -d '{"brand_id":"drewber","display_name":"Drewber Solutions","channel":"C0BF8QKP0PL"}' \
  https://nova-production-14f6.up.railway.app/onboard/start

# Check deploy status + logs
railway deployment list
railway logs

# Check onboarding session state
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
  -c "SELECT brand_id, phase, status FROM onboarding_sessions;"

# Check DB-backed brands + platforms
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
  -c "SELECT brand_id, config->>'display_name', config->'platforms' FROM brands;"

# Check recent drafts + approval status
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
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

## 12. NEXT STEP — V2 (not built yet)

- ~~`/publish` endpoint — select approved items, push to Meta Graph API or Pipeboard Meta Ads MCP tool, set `status='posted'` + `posted_at`.~~ **DONE — see section 14.**
- Image generation — `generator.generate_image` is a stub returning `None`. Wire via Auto mode across Grok, Gemini, ChatGPT when ready.

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

**Verified live 2026-07-05:** content item 13 (approved BICCU facebook draft) scheduled via `POST /publish` with `scheduled_for` 25 min out. Meta returned post id `120368170965_1702865025176333`; confirmed present in the page's `scheduled_posts` edge with the correct message + `scheduled_publish_time`. DB updated to `status='scheduled'`, `meta_post_id` + `scheduled_for` populated. `/meta/verify` returns `{"page_name": "Bahama Islands Co-operative Credit Union Limited"}`.

### Endpoints (new)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/publish` | `X-Cron-Secret` header | Publish (or schedule) an approved content item to its Facebook page. Body `{item_id, scheduled_for?}`. |
| GET | `/meta/verify` | `X-Cron-Secret` header | Sanity check that `META_PAGE_ACCESS_TOKEN` works against `META_PAGE_ID`. Returns the page name. |

### Slack UX

After clicking **Approve** on a facebook draft, the message now shows two extra buttons:
- **Publish now** → background task calls Meta Graph `POST /{page_id}/feed`, sets `status='posted'`, `posted_at`, `meta_post_id`. Message flips to ":rocket: Published to Facebook. Meta post id: `...`".
- **Schedule** → swaps in a Slack `datetimepicker` + **Confirm schedule** button. On confirm, background task calls Meta with `published=false` + `scheduled_publish_time`, sets `status='scheduled'`, `scheduled_for`, `meta_post_id`. Meta publishes the post automatically at the requested time (10 min - 6 months out). No cron needed.

Non-facebook approved drafts keep the pre-V2 "ready for manual posting" line (no publish buttons). The publish endpoints/handlers refuse non-facebook items with a clear error.

### Files added/changed (V2)

```
schema.sql                              + meta_post_id TEXT, published_via TEXT DEFAULT 'meta_graph'
app/core/meta_publisher.py              publish_page_post / schedule_page_post / verify_token
app/core/db.py                          get_content_item; update_status extended (meta_post_id, scheduled_for)
app/core/slack_client.py                format_resolved_approval_blocks now appends Publish/Schedule buttons
                                        + format_schedule_picker_blocks, format_publish_result_blocks, update_message
app/routes/publish.py                   POST /publish + GET /meta/verify (X-Cron-Secret gated)
app/routes/slack_interactions.py        publish_now / publish_schedule / publish_confirm / publish_dt_* handlers
app/main.py                             publish_router registered; version 1.3.0
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
7. Verify: `curl -H "X-Cron-Secret: massive-music-tech-issues" https://nova-production-14f6.up.railway.app/meta/verify` should return `{"ok": true, "page_name": "Bahama Islands Co-operative Credit Union Limited"}`.

### Token lifecycle note (2026-07-05)

The `META_PAGE_ACCESS_TOKEN` on Railway is **long-lived (never expires, `expires_at: 0`)** — generated via the `fb_exchange_token` flow + page-token re-fetch. App used is "Nova-Agent" (app id `27311849091810327`), token type PAGE, scopes `pages_show_list` + `pages_read_engagement` + `pages_manage_posts`. It only stops working if the app is removed from the page or the page unlinks the app.

To regenerate from scratch (e.g. for a second brand's page): see the steps in section 15.
