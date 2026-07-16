#!/usr/bin/env python3
"""Fatsoma ticket-drop monitor.

Polls the Fatsoma JSON API for watched events (Ministry of Sound Tuesdays,
fabric student nights), detects when a "normal" (non-VIP, non-table) ticket
option becomes available, and posts a Discord webhook notification with the
cheapest available option, date, title and link.

State is kept in state.json so only *transitions* to available trigger a
notification (new releases and re-releases included). First ever run just
baselines current availability without notifying.

Stdlib only - no dependencies.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

API_BASE = "https://api.fatsoma.com/v1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Make emoji-laden event names printable on any console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------- API access

def api_get(path, params):
    url = f"{API_BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.api+json", "User-Agent": USER_AGENT},
    )
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"API request failed after retries: {url}: {last_err}")


def fetch_event_pages(extra_filters):
    """Fetch all pages of /v1/events for the given filters.

    Returns (events, included_index) where included_index maps
    (type, id) -> attributes for pages/locations/ticket-options.
    """
    events, index = [], {}
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    page = 1
    while page <= 5:
        params = {
            "filter[status]": "active",
            "filter[ends-at][gte]": now_iso,
            "include": "page,location,ticket-options",
            "page[number]": str(page),
            "page[size]": "52",
            "sort": "starts-at-day,relevance",
        }
        params.update(extra_filters)
        data = api_get("events", params)
        events.extend(data.get("data", []))
        for inc in data.get("included", []):
            index[(inc["type"], inc["id"])] = inc.get("attributes", {})
        total_pages = data.get("meta", {}).get("total-pages", 1)
        if page >= total_pages:
            break
        page += 1
    return events, index


def fetch_all_sources(watch):
    """Collect events for a watch from its page ids and search queries."""
    all_events, index = {}, {}
    sources = []
    for pid in watch.get("page_ids", []):
        sources.append({"filter[page.id]": pid})
    for q in watch.get("queries", []):
        sources.append({"filter[query]": q})
    for flt in sources:
        try:
            events, inc = fetch_event_pages(flt)
        except RuntimeError as e:
            log(f"WARN: source {flt} failed: {e}")
            continue
        for ev in events:
            all_events[ev["id"]] = ev
        index.update(inc)
    return list(all_events.values()), index


# ------------------------------------------------------------- event parsing

def rel_id(event, rel):
    data = event.get("relationships", {}).get(rel, {}).get("data")
    return data["id"] if data else None


def event_matches_watch(event, index, watch):
    attrs = event["attributes"]
    loc = index.get(("locations", rel_id(event, "location")), {})
    venue = (loc.get("name") or "").strip()
    postcode = (loc.get("postal-code") or "").replace(" ", "").upper()

    venue_ok = False
    for pat in watch.get("venue_patterns", []):
        if re.search(pat, venue, re.IGNORECASE):
            venue_ok = True
    for pc in watch.get("venue_postcodes", []):
        if pc.replace(" ", "").upper() == postcode:
            venue_ok = True
    if not venue_ok:
        return False

    weekdays = [w.lower() for w in watch.get("weekdays", [])]
    if weekdays:
        starts = datetime.fromisoformat(attrs["starts-at"])
        if WEEKDAYS[starts.weekday()] not in weekdays:
            return False
    return True


def option_is_normal(opt, cfg):
    """A 'normal' ticket: visible, publicly buyable, not VIP/table, not pricey."""
    if not opt.get("visible", True):
        return False
    if opt.get("addon") or opt.get("rep-only") or opt.get("accessible-via-access-code"):
        return False
    name = opt.get("name") or ""
    for pat in cfg["exclude_ticket_name_patterns"]:
        if re.search(pat, name, re.IGNORECASE):
            return False
    if per_person_price(opt) > cfg["max_price_per_person_gbp"]:
        return False
    return True


def per_person_price(opt):
    admits = opt.get("admits") or 1
    total = (opt.get("price-sub-unit") or 0) + (opt.get("transaction-fee-sub-unit") or 0)
    return total / 100 / max(admits, 1)


def option_available(opt):
    if opt.get("on-sale-status") != "available":
        return False
    amount = opt.get("amount-available")
    if amount is not None and amount <= 0:
        return False
    return True


def fmt_price(opt):
    admits = opt.get("admits") or 1
    base = (opt.get("price-sub-unit") or 0) / 100
    fee = (opt.get("transaction-fee-sub-unit") or 0) / 100
    s = f"£{base:.2f}"
    if fee:
        s += f" +£{fee:.2f} fee"
    if admits > 1:
        s += f" (admits {admits})"
    return s


def event_url(attrs):
    return f"https://www.fatsoma.com/e/{attrs['vanity-name']}/{attrs['seo-name']}"


# ------------------------------------------------------------------- Discord

def send_discord(webhook, content, embeds):
    """Post embeds to the webhook, 5 per message, honouring rate limits."""
    for i in range(0, len(embeds), 5):
        payload = {"embeds": embeds[i : i + 5], "allowed_mentions": {"parse": ["everyone"]}}
        if content and i == 0:
            payload["content"] = content
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = float(e.headers.get("Retry-After", "2"))
                    time.sleep(min(retry, 30) + 0.5)
                    continue
                raise
        time.sleep(0.5)


def build_embed(event, index, watch, triggered, cheapest, is_first_sight):
    attrs = event["attributes"]
    loc = index.get(("locations", rel_id(event, "location")), {})
    seller = index.get(("pages", rel_id(event, "page")), {})
    starts = datetime.fromisoformat(attrs["starts-at"])
    unix = int(starts.timestamp())

    lines = []
    for opt in triggered[:8]:
        left = opt.get("amount-available")
        left_s = f" — {left} left" if left is not None else ""
        lines.append(f"• **{opt.get('name', '?').strip()}** — {fmt_price(opt)}{left_s}")

    cheapest_line = "—"
    if cheapest is not None:
        left = cheapest.get("amount-available")
        left_s = f" — {left} left" if left is not None else ""
        cheapest_line = f"**{fmt_price(cheapest)}** — {cheapest.get('name', '?').strip()}{left_s}"

    header = "\U0001f195 New event on sale" if is_first_sight else "\U0001f501 Tickets (re-)released"
    return {
        "title": attrs["name"][:256],
        "url": event_url(attrs),
        "color": 0x57F287 if is_first_sight else 0xFEE75B,
        "description": header,
        "fields": [
            {
                "name": "\U0001f4c5 Date",
                "value": f"<t:{unix}:F> (<t:{unix}:R>)",
                "inline": False,
            },
            {
                "name": "\U0001f4b7 Cheapest normal ticket",
                "value": cheapest_line[:1024],
                "inline": False,
            },
            {
                "name": "\U0001f39f Just became available",
                "value": "\n".join(lines)[:1024] or "—",
                "inline": False,
            },
        ],
        "footer": {
            "text": f"{watch['name']} · {loc.get('name', '?')} · seller: {seller.get('name', '?')}"[:2048]
        },
    }


# --------------------------------------------------------------------- state

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"initialized": False, "events": {}}


def save_state(state):
    now = datetime.now(timezone.utc)
    def still_relevant(ev):
        try:
            return datetime.fromisoformat(ev["ends"]) > now - timedelta(days=1)
        except (KeyError, ValueError, TypeError):
            return True

    state["events"] = {eid: ev for eid, ev in state["events"].items() if still_relevant(ev)}
    state["last_run"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------- main

def run(test_mode=False):
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    state = load_state()
    first_run = not state.get("initialized")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    cooldown = timedelta(hours=cfg.get("renotify_cooldown_hours", 6))
    now = datetime.now(timezone.utc)

    embeds, test_embeds = [], []

    for watch in cfg["watches"]:
        events, index = fetch_all_sources(watch)
        matched = [e for e in events if event_matches_watch(e, index, watch)]
        log(f"watch '{watch['name']}': {len(events)} fetched, {len(matched)} match")

        for event in matched:
            attrs = event["attributes"]
            eid = event["id"]
            ev_state = state["events"].get(eid)
            is_first_sight = ev_state is None
            ends = attrs.get("ends-at") or attrs["starts-at"]
            if ev_state is None:
                ev_state = {"name": attrs["name"], "ends": ends, "opts": {}}
                state["events"][eid] = ev_state
            ev_state["ends"] = ends

            rel = event.get("relationships", {}).get("ticket-options", {}).get("data") or []
            opt_ids = [d["id"] for d in rel]
            normal_opts = []
            for oid in opt_ids:
                opt = index.get(("ticket-options", oid))
                if opt is not None and option_is_normal(opt, cfg):
                    normal_opts.append((oid, opt))

            available = [(oid, o) for oid, o in normal_opts if option_available(o)]
            cheapest = min((o for _, o in available), key=per_person_price, default=None)

            triggered = []
            for oid, opt in normal_opts:
                avail = option_available(opt)
                prev = ev_state["opts"].get(oid, {})
                was_avail = prev.get("st") == "available"
                notified_at = prev.get("notified_at")
                cool_ok = (
                    notified_at is None
                    or now - datetime.fromisoformat(notified_at) > cooldown
                )
                if avail and not was_avail and cool_ok and not first_run:
                    triggered.append(opt)
                    prev["notified_at"] = now.isoformat()
                prev["st"] = "available" if avail else (opt.get("on-sale-status") or "unknown")
                ev_state["opts"][oid] = prev

            if triggered:
                name_ascii = attrs["name"].encode("ascii", "replace").decode()
                log(f"  DROP: {name_ascii[:70]} ({len(triggered)} option(s))")
                embeds.append(
                    build_embed(event, index, watch, triggered, cheapest, is_first_sight)
                )
            if test_mode and cheapest is not None and len(test_embeds) < 5:
                test_embeds.append(
                    build_embed(event, index, watch, [o for _, o in available], cheapest, False)
                )

    state["initialized"] = True
    save_state(state)

    if first_run:
        log("first run: baselined current availability, no notifications sent")
    if test_mode:
        embeds = test_embeds
        log(f"test mode: sending {len(embeds)} sample notification(s)")

    if embeds:
        if not webhook:
            log(f"WARN: {len(embeds)} notification(s) pending but DISCORD_WEBHOOK_URL is not set")
        else:
            mention = "" if test_mode else cfg.get("discord_mention", "")
            content = f"{mention} \U0001f39f Fatsoma ticket drop!".strip()
            if test_mode:
                content = "\U0001f9ea Test notification — current availability snapshot"
            send_discord(webhook, content, embeds)
            log(f"sent {len(embeds)} notification(s) to Discord")
    else:
        log("no new drops")


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv)
