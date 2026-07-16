# fatsoma-monitor

Watches Fatsoma 24/7 via GitHub Actions (every ~5 min) and posts to a private
Discord channel when **normal** tickets drop or get re-released for:

- **Ministry of Sound Tuesdays** (Milkshake student nights, Freshers launches, Halloween etc.)
- **fabric student nights** (any seller listing an event at fabric, EC1M 6HJ)

Each notification includes the event title, date, the **cheapest normal ticket**
(price + booking fee, and how many are left), which options just became
available, the seller, and a direct link.

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
| `watches[]` | `page_ids` / `queries` are sources; `venue_patterns` / `venue_postcodes` / `weekdays` filter |

Run locally with `python monitor.py` (add `--test` for a snapshot message).
