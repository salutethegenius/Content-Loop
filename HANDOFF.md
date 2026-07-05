# Content Loop Agent — Session Handoff

**Last updated:** 2026-07-05
**Repo:** https://github.com/salutethegenius/Content-Loop (private)
**Live deployment:** https://nova-production-14f6.up.railway.app
**Railway project:** `verityos-agents` (ID `47ab2c83-6fc9-42e7-9a12-92c98552c2ea`), service `nova`, environment `production`

Read this first if you are picking up the project in a new session. It covers what is built, what is live, the gotchas already solved, and the exact next step the user wants.

---

## 1. What this project is

Brand-agnostic content loop. FastAPI on Railway, Postgres on Railway, Slack approval flow, Claude API for drafting. "Nova" is the agent persona. Brands are plug-ins. V1 is text-only: drafts land in Slack for Approve/Reject, approved items sit in `content_items` with `status = 'approved'` and Kenneth posts them manually. No Meta publishing, no image generation yet.

Two capabilities are live:
1. **Content loop** — cron hits `/cron/generate`, generates drafts per active brand/platform, posts each to Slack with Approve/Reject buttons, stores in `content_items`.
2. **Nova onboarding** — admin hits `/onboard/start`, Nova opens a Slack thread and walks a business through a 6-phase interview, synthesizes `voice.md` + `config.json` via Claude, posts the draft with Approve/Reject/Regenerate buttons. Approve persists the brand to the `brands` table and it is immediately live in the next cron run (no file write, no redeploy).

---

## 2. Repo layout

```
/app/main.py                      FastAPI app, sys.path shim for `from core/` imports
/app/brands/{kgc,biccu,drewber}/  filesystem seed brands (config.json + voice.md)
/app/core/brand_loader.py         get_active_brands / load_voice / is_due_for_post
                                  reads DB brands first, falls back to filesystem
/app/core/db.py                   psycopg2 helpers: content_items, brands, onboarding_sessions
/app/core/generator.py            Claude draft generation, pillar-based topic selection
/app/core/onboarding.py           6-phase interview script + Claude synthesis of voice/config
/app/core/slack_client.py         post_for_approval (content) + post_message (generic)
/app/routes/cron.py               POST /cron/generate (X-Cron-Secret gated)
/app/routes/onboarding.py         POST /onboard/start (X-Cron-Secret gated)
/app/routes/slack_events.py       POST /slack/events (Slack Events API, signed)
/app/routes/slack_interactions.py POST /slack/interactions (button clicks, signed)
schema.sql                        content_items + brands + onboarding_sessions
```

Key design rule: **nothing in `/app/core` or `/app/routes` references a brand name directly.** Brands are plug-ins. Adding one = onboarding flow → row in `brands` table (or a folder under `app/brands/`).

---

## 3. Endpoints (all live)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/` | none | health check |
| POST | `/cron/generate` | `X-Cron-Secret` header | run the content loop for all due active brands/platforms |
| POST | `/onboard/start` | `X-Cron-Secret` header | open a Nova onboarding thread; body `{brand_id, display_name, channel}` |
| POST | `/slack/events` | Slack HMAC signature | Slack Events API: `url_verification` + `message.groups` (private channel) |
| POST | `/slack/interactions` | Slack HMAC signature | button clicks: content Approve/Reject + onboard Approve/Reject/Regenerate |

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
| `CRON_SECRET` | `massive-music-tech-issues` — required header for `/cron/generate` and `/onboard/start` |

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
- `generator.pick_pillar(brand_config, platform)` selects a pillar per run via date-based rotation: `index = date.toordinal(today) % len(pillars)`, with LinkedIn offset by +1 so same-day cross-platform drafts cover different pillars.
- `generate_draft` passes the pillar name + description as the user-prompt topic, with explicit instructions to stay in-voice and not invent products/rates/promotions.
- Brands without `content_pillars` (Drewber, KGC — not yet onboarded) fall back to a strengthened prompt that tells Claude to invent a topic in-voice instead of asking for a brief.
- **Proven:** BICCU drafts now anchor to specific pillars (Member Success Stories on Instagram, Financial Wellness on LinkedIn) instead of "I don't have a topic."

### Brand folder renames: COMPLETE
- `app/brands/bcu` → `biccu`, `app/brands/juber` → `drewber`. Configs + voice.md headers updated. `kgc` unchanged.

---

## 8. Current brand state

| brand_id | display_name | source | has_pillars | notes |
| --- | --- | --- | --- | --- |
| `biccu` | BICCU | DB (onboarded) | yes, 7 | credit union, cadence 2 days, fully onboarded via Nova |
| `drewber` | Drewber Solutions | filesystem seed | no | uses fallback prompt until onboarded |
| `kgc` | Kemis Group of Companies | filesystem seed | no | uses fallback prompt until onboarded |

To onboard Drewber or KGC, hit `/onboard/start` with the right `brand_id` and channel, same flow as BICCU.

---

## 9. BICCU onboarding outcome (reference example)

Onboarded via Nova in the `nova-agent` Slack channel. Session id 1, status `approved`, phase `done`. Stored in `brands` table:

- **display_name:** BICCU
- **posting_cadence_days:** 2
- **image_style_prompt:** "clean, professional and community-focused imagery using BICCU's corporate color palette of cyan blue, deep navy, warm earth tones, high visibility orange and clean white, featuring authentic Bahamian people and everyday life whenever possible."
- **content_pillars (7):** Member Success Stories, Financial Wellness and Education, Products That Solve Everyday Problems, Life Milestones, Community and Member Engagement, Empowering Membership, Announcements and Service Updates.
- **voice.md:** credit-union voice, "trusted neighbor who happens to know a lot about money," formality 3/5, warm humor, em-dashes banned, banned phrases list (revolutionary, game changing, get rich, etc.), no competitor mentions, no politics/religion, compliance-adjacent content flagged for approval. ~1081 chars.

Sample pillar-anchored drafts (item ids 7, 8) are in `content_items` and were posted to Slack.

---

## 10. Common operations

```bash
# Link Railway to the nova service (if cwd link is stale)
cd /Users/ghost/Desktop/ORG/agents/nova/content-loop
railway link --project 47ab2c83-6fc9-42e7-9a12-92c98552c2ea --service nova --environment production

# Run schema updates on Postgres
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway -f schema.sql

# Trigger the content loop manually
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

# Check DB-backed brands
PGPASSWORD='SDKkDXQSUDdkVccpGoHEPTkfFHbeXsod' psql -h reseau.proxy.rlwy.net -p 13297 -U postgres -d railway \
  -c "SELECT brand_id, config->>'display_name' FROM brands;"
```

Auto-deploy is on — pushing to `main` triggers a Railway rebuild. Setting env vars also triggers a redeploy.

---

## 11. NEXT STEP — what Kenneth wants next session

> "Have Nova ask first who she will be generating posts for, and not just generate for all brands, this could be wasteful. Also should ask which channels she is generating for. Facebook and IG are already first, then optional platforms like LinkedIn."

### The requirement

Today `/cron/generate` blindly loops over **all** active brands × **all** their platforms and generates a draft for each. That is wasteful: Kenneth may only want posts for one brand on a given run, or only Instagram + Facebook (not LinkedIn).

The next build makes generation **interactive and targeted**:

1. **Nova asks who she is generating for** — instead of (or in addition to) the blanket cron, a flow where Nova posts a Slack message asking which brand(s) to generate for this run. Could be buttons (one per active brand) or a multi-select.
2. **Nova asks which platforms** — for the selected brand(s), ask which platforms to generate for this run.
3. **Platform priority shift** — Facebook and Instagram are the **primary** platforms. LinkedIn is **optional**. This is a change from the current configs which default to `["instagram", "linkedin"]`.

### Implementation notes for the next session

- **Platform contract change:** `config.json` `platforms` should default to `["facebook", "instagram"]` with `linkedin` as an opt-in third. The generator's system prompt already mentions per-platform formatting; BICCU's voice.md already has Facebook hashtag rules ("Use 2 to 4 hashtags on Facebook") so Facebook is partially anticipated. Need to:
  - Update `generator.generate_draft` to handle `platform == "facebook"` (formatting rules in the system prompt).
  - Update the onboarding Phase 5 script to ask about Facebook explicitly as a primary platform (currently it asks about Instagram vs LinkedIn).
  - Update filesystem seed configs (`kgc`, `biccu`, `drewber`) to `["facebook", "instagram"]` and re-onboard BICCU if Kenneth wants LinkedIn back in.
- **New endpoint or extend the cron flow:** likely a new `POST /generate/start` (X-Cron-Secret gated) that posts a Slack message with brand-selection buttons. On brand selection, post platform-selection buttons. On platform confirmation, run the loop for just that brand/platform subset. The existing `/cron/generate` can stay as the blanket fallback for the scheduled Railway cron, OR be replaced by the interactive flow depending on Kenneth's preference.
- **Slack interaction actions to add:** `gen_pick_brand` (value = brand_id), `gen_pick_platforms` (value = brand_id + platform subset, probably encoded as `brand_id:facebook,instagram`), `gen_confirm` (kicks off generation in a BackgroundTask).
- **State:** can use a lightweight `generation_requests` table or just drive it statelessly through sequential button payloads. Stateless is simpler if each button click carries enough context in its `value`.
- **Keep the cron path working** — Railway cron can still hit `/cron/generate` for the blanket run, but Kenneth may want to remove the cron schedule and use the interactive flow exclusively. Ask him.

### Open questions to confirm with Kenneth before building

1. Does the interactive flow **replace** the blanket `/cron/generate` cron, or run **alongside** it (cron as fallback, interactive for manual runs)?
2. Multi-brand per run, or one brand at a time?
3. For platforms: hard default to Facebook + Instagram with LinkedIn opt-in, or per-brand config still drives the menu and we just change the defaults?
4. Should the interactive prompt live in the same `nova-agent` channel or a dedicated `#nova-generate` channel?

---

## 12. V2 hooks (still not built)

- `/publish` endpoint — select approved items, push to Meta Graph API or Pipeboard Meta Ads MCP tool, set `status='posted'` + `posted_at`.
- Image generation — `generator.generate_image` is a stub returning `None`. Wire via Auto mode across Grok, Gemini, ChatGPT when ready.

---

## 13. Known small issues / polish for later

- The `content_items` table has no `pillar` column — currently you cannot tell which pillar a draft was anchored to. Consider adding `pillar TEXT` to `content_items` and having `save_draft` record it, for analytics/content planning.
- `onboarding_sessions.answers` accumulates raw message text per phase; if a human posts many messages, the synthesis prompt grows. Fine for now (6 phases, modest volume), but worth a length guard eventually.
- No per-session lock — two rapid replies in the same onboarding thread could race. Low risk at current volume.
- `append_onboarding_answer` uses `answers || jsonb_build_object(...)` which is safe but worth noting if answers ever get nested.
