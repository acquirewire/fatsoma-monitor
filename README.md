# fatsoma-monitor

Watches Fatsoma 24/7 via GitHub Actions (polls every 60s) and posts to a
private Discord channel when **normal** tickets drop or get re-released for:

- **Ministry of Sound Tuesdays** (Milkshake student nights, Freshers launches, Halloween etc.)
- **fabric student nights** (any seller listing an event at fabric, EC1M 6HJ)
- **LSE AU Wednesday sports nights** (currently at XOYO London)
- **London Halloween club nights** (any London venue, any seller; until 1 Nov)

Each notification includes the event title, date, the **cheapest normal ticket**
(price + booking fee, and the per-order cap), which options just became
available, the seller, and a direct link. Fatsoma's `amount-available` is
capped at `max-per-order`, so it is *not* remaining stock.

VIP tables, booths, queue jumps, bottle packages and anything over
`max_price_per_person_gbp` (default £30/head, bundles normalised per person)
are ignored. Re-releases of standard tickets *do* notify. A 6-hour per-option
cooldown stops cart-release flapping from spamming the channel.

## How it works

`monitor.py` (stdlib only) polls Fatsoma's public JSON API
(`api.fatsoma.com/v1/events`):

- Ministry: fetches the Milkshake page's events directly (`filter[page.id]`)
  plus a `ministry of sound` search, then keeps events at Ministry of Sound
  (name or postcode SE1 6DP) that start on a **Tuesday**.
- fabric: searches `fabric` and keeps events whose venue matches
  `\bfabric\b` or postcode **EC1M 6HJ** — sellers each create their own copy
  of the venue record, so this catches all of them.
- LSE AU: fetches the `lseathleticsunion` page's events plus an `lse`
  search, then keeps **Wednesday** events whose title or seller matches
  `LSE … AU/sports/athletics`. No venue filter, so a venue move is still caught.
- Halloween: sweeps the whole `halloween` search restricted to the "Club Nights"
  category (~27 pages, every 5 min), then keeps events with a Halloween-ish
  title whose venue city is London or postcode is in a London district. The
  API has no area/city filter and the `halloween london` search misses half the
  London listings (it doesn't search venue city), hence the full sweep. Its
  first sweep baselines silently and posts one digest instead of ~150 alerts.

API calls use gzip and sparse fieldsets (`SPARSE_FIELDS` in `monitor.py`) —
~17× smaller payloads. Any new attribute the code reads must be added there.

GitHub throttles `*/5` cron schedules to a few runs a day, so the workflow
runs one ~5.5h job that polls in a loop and dispatches its own successor
before ending (it waits as a pending run in the same concurrency group). An
hourly cron restarts the chain if a handoff is ever lost. Each loop iteration
`git pull`s, so config/code pushes take effect within a minute.

Per-ticket-option availability is tracked in `state.json` (committed back by
the workflow). Only transitions **to** available notify — the first run just
baselines. Notifications go to the `DISCORD_WEBHOOK_URL` webhook as embeds
with Discord dynamic timestamps.

## Setup

1. In Discord: channel settings (your private channel) → **Integrations →
   Webhooks → New Webhook** → copy the webhook URL.
2. Add it as a repo secret:
   `gh secret set DISCORD_WEBHOOK_URL --repo acquirewire/fatsoma-monitor`
3. Verify: **Actions → fatsoma-monitor → Run workflow** with *test* ticked —
   sends a snapshot of current availability to the channel.

## Tuning (`config.json`)

| Key | Meaning |
| --- | --- |
| `max_price_per_person_gbp` | Ignore options above this per-head price (incl. fee) |
| `renotify_cooldown_hours` | Min hours before the same option can notify again |
| `discord_mention` | Prefix for real alerts, e.g. `@everyone` (empty to disable pings) |
| `exclude_ticket_name_patterns` | Case-insensitive regexes for ticket names to ignore |
| `watches[]` | `page_ids` / `queries` are sources (`extra_filters` added to each, `max_pages` caps paging); `venue_patterns` / `venue_postcodes` / `city_patterns` / `postcode_patterns` (any match) plus `name_patterns` (title + seller) and `weekdays` filter — each omitted filter is skipped |
| `min_interval_minutes` / `active_until` | Per-watch sweep throttle and expiry date |
| `silent_baseline` | First sweep records current listings without alerting and posts one digest |

Run locally with `python monitor.py` (add `--test` for a snapshot message).
