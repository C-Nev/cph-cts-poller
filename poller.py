"""Poll Kiwi.com for cheap CPH -> Sapporo trips and email the good ones.

Two routings are tracked and compared on total cost:
  direct     CPH -> CTS on one ticket
  via_tokyo  CPH -> Tokyo, plus a separately booked Tokyo -> CTS domestic hop

Uses the public GraphQL endpoint kiwi.com's own front end calls. No API key:
Kiwi's Tequila developer programme is invite-only and no longer issues keys.

Alerts are emailed through Resend. Set RESEND_API_KEY and ALERT_EMAIL_TO.
Until a domain is verified with Resend, ALERT_EMAIL_TO must be the address the
Resend account was registered with, and the sender stays onboarding@resend.dev.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api.skypicker.com/umbrella/v2/graphql"
BOOKING_BASE = "https://www.kiwi.com"

ORIGIN = "Station:airport:CPH"
SAPPORO = "Station:airport:CTS"
TOKYO = "City:tokyo_jp"

OUTBOUND_WINDOW = ("2027-02-01T00:00:00", "2027-02-14T23:59:59")
INBOUND_WINDOW = ("2027-03-20T00:00:00", "2027-03-31T23:59:59")

# The domestic hop is booked separately, so it needs slack either side of the
# long-haul legs rather than matching their dates exactly.
DOMESTIC_OUTBOUND_WINDOW = ("2027-02-02T00:00:00", "2027-02-16T23:59:59")
DOMESTIC_INBOUND_WINDOW = ("2027-03-18T00:00:00", "2027-03-30T23:59:59")

HOLD_BAGS = 1
HAND_BAGS = 1

# Kiwi caps each search at ~10-25 results sorted by price, so a single
# maxStops=3 search only ever surfaces tight-connection fares. Search each stop
# count and merge.
STOP_COUNTS = (1, 2, 3)

CURRENCY = "dkk"
CURRENCY_LABEL = "DKK"

MIN_LAYOVER_SECONDS = 3 * 60 * 60  # self-transfer means re-checking bags by hand


def _threshold(name, default):
    """GitHub passes an empty string for an unset repository variable."""
    return float(os.environ.get(name) or default)


# Floor with a hold bag and legal layovers is ~8,900 via Tokyo, ~13,500 direct.
# Shoulder-season pricing on the same long-haul is ~7,150, so there is roughly
# 1,000-1,800 DKK of headroom, not more.
ALERT_THRESHOLD = _threshold("ALERT_THRESHOLD", 8500)
SMASH_BUY_THRESHOLD = _threshold("SMASH_BUY_THRESHOLD", 7800)

# A total this far under the cheapest ever recorded is worth shouting about even
# if it is above the fixed thresholds.
RECORD_MARGIN = 100

# If nothing has been posted in this long, post a status message anyway, so that
# silence from the poller always means "no deals" and never "it died".
HEARTBEAT_DAYS = 7

# GitHub only honours roughly 70% of a short cron, so one scheduled run polls
# repeatedly instead of relying on the schedule for resolution.
POLL_ITERATIONS = int(os.environ.get("POLL_ITERATIONS") or 1)
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS") or 300)

# ntfy.sh push for the fares worth interrupting you for. Optional.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# Only re-alert a routing already seen if it got at least this much cheaper.
PRICE_DROP_TO_REALERT = 250
MAX_ALERTS_PER_RUN = 3

CACHE_PATH = Path(__file__).with_name("seen_fares.json")
REQUEST_TIMEOUT = 90

RESEND_URL = "https://api.resend.com/emails"
# resend.dev is the unverified-domain sender. Override once a real domain is
# verified, e.g. ALERT_EMAIL_FROM="Fare poller <alerts@polliknow.com>".
DEFAULT_FROM = "Fare poller <onboarding@resend.dev>"

SEARCH_QUERY = """
query SearchReturnItinerariesQuery(
  $search: SearchReturnInput
  $filter: ItinerariesFilterInput
  $options: ItinerariesOptionsInput
) {
  returnItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { error: message }
    ... on Itineraries {
      itineraries {
        ... on ItineraryReturn {
          id
          price { amount }
          outbound { ...SectorFields }
          inbound { ...SectorFields }
          bookingOptions { edges { node { bookingUrl } } }
        }
      }
    }
  }
}

fragment SectorFields on Sector {
  duration
  sectorSegments {
    layover { duration }
    segment {
      source { station { code } utcTime localTime }
      destination { station { code } utcTime localTime }
      carrier { code name }
      type
    }
  }
}
"""


def search(origin, destination, max_stops, outbound_window, inbound_window):
    variables = {
        "search": {
            "itinerary": {
                "source": {"ids": [origin]},
                "destination": {"ids": [destination]},
                "outboundDepartureDate": {
                    "start": outbound_window[0],
                    "end": outbound_window[1],
                },
                "inboundDepartureDate": {
                    "start": inbound_window[0],
                    "end": inbound_window[1],
                },
            },
            "passengers": {
                "adults": 1,
                "children": 0,
                "infants": 0,
                "adultsHoldBags": [HOLD_BAGS],
                "adultsHandBags": [HAND_BAGS],
                "childrenHoldBags": [],
                "childrenHandBags": [],
            },
            "cabinClass": {"cabinClass": "ECONOMY", "applyMixedClasses": False},
        },
        "filter": {
            "allowChangeInboundDestination": True,
            "allowChangeInboundSource": True,
            "allowDifferentStationConnection": True,
            "enableSelfTransfer": True,
            "enableThrowAwayTicketing": True,
            "enableTrueHiddenCity": True,
            "transportTypes": ["FLIGHT"],
            "contentProviders": ["KIWI"],
            "flightsApiLimit": 25,
            "limit": 25,
            "maxStopsCount": max_stops,
        },
        "options": {
            "sortBy": "PRICE",
            "mergePriceDiffRule": "INCREASED",
            "currency": CURRENCY,
            "locale": "en",
            "partner": "skypicker",
            "affilID": "skypicker",
            "storeSearch": False,
            "searchStrategy": "REDUCED",
        },
    }
    response = requests.post(
        f"{API_URL}?featureName=SearchReturnItinerariesQuery",
        json={"query": SEARCH_QUERY, "variables": variables},
        headers={
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")

    result = payload.get("data", {}).get("returnItineraries") or {}
    if result.get("__typename") == "AppError":
        raise RuntimeError(f"Kiwi error: {result.get('error')}")
    return result.get("itineraries") or []


def search_all(origin, destination, outbound_window, inbound_window, stop_counts=STOP_COUNTS):
    """Merge searches across stop counts, keeping the cheapest copy of each routing."""
    best = {}
    for max_stops in stop_counts:
        for itinerary in search(
            origin, destination, max_stops, outbound_window, inbound_window
        ):
            key = signature(itinerary)
            if key not in best or price_of(itinerary) < price_of(best[key]):
                best[key] = itinerary
    return list(best.values())


def domestic_hop():
    """Cheapest Tokyo -> CTS return with a hold bag, used as a flat add-on."""
    itineraries = search_all(
        TOKYO,
        SAPPORO,
        DOMESTIC_OUTBOUND_WINDOW,
        DOMESTIC_INBOUND_WINDOW,
        stop_counts=(0,),
    )
    if not itineraries:
        return None
    cheapest = min(itineraries, key=price_of)
    carrier = cheapest["outbound"]["sectorSegments"][0]["segment"]["carrier"]["name"]
    return {"price": price_of(cheapest), "carrier": carrier}


def segments(itinerary, leg):
    return (itinerary.get(leg) or {}).get("sectorSegments") or []


def price_of(itinerary):
    return float(itinerary["price"]["amount"])


def layover_ok(itinerary):
    """Reject anything with a self-transfer connection shorter than 3 hours."""
    for leg in ("outbound", "inbound"):
        for entry in segments(itinerary, leg):
            layover = entry.get("layover")
            if layover and layover["duration"] < MIN_LAYOVER_SECONDS:
                return False
    return True


def signature(itinerary):
    """Identify a routing independent of its price, so price drops are detectable."""
    parts = []
    for leg in ("outbound", "inbound"):
        for entry in segments(itinerary, leg):
            seg = entry["segment"]
            parts.append(
                f"{seg['carrier']['code']}:"
                f"{seg['source']['station']['code']}-{seg['destination']['station']['code']}:"
                f"{seg['source']['utcTime']}"
            )
    return "|".join(parts)


def departure_time(itinerary):
    return segments(itinerary, "outbound")[0]["segment"]["source"]["utcTime"]


def booking_url(itinerary):
    edges = (itinerary.get("bookingOptions") or {}).get("edges") or []
    if not edges:
        return f"{BOOKING_BASE}/en/search/results/copenhagen-denmark/sapporo-japan"
    url = edges[0]["node"]["bookingUrl"]
    return url if url.startswith("http") else f"{BOOKING_BASE}{url}"


EMPTY_CACHE = {"meta": {"last_run": None, "last_notified": None, "records": {}}, "fares": {}}


def load_cache():
    if not CACHE_PATH.exists():
        return json.loads(json.dumps(EMPTY_CACHE))
    try:
        cache = json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        print("Cache unreadable, starting fresh.", file=sys.stderr)
        return json.loads(json.dumps(EMPTY_CACHE))
    cache.setdefault("meta", {}).setdefault("records", {})
    cache.setdefault("fares", {})
    return cache


def save_cache(cache):
    now = utc_now()
    # Drop itineraries whose outbound has already departed.
    cache["fares"] = {
        key: entry
        for key, entry in cache["fares"].items()
        if entry.get("departure", "9999") > now[:19]
    }
    # Always stamp the run, so the workflow has something to commit and GitHub
    # keeps the cron schedule alive on an otherwise quiet repo.
    cache["meta"]["last_run"] = now
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_new_deal(cache, key, price):
    entry = cache["fares"].get(key)
    if entry is None:
        return True
    return price <= entry["price"] - PRICE_DROP_TO_REALERT


def is_record(cache, route, total):
    record = cache["meta"]["records"].get(route)
    return record is None or total <= record - RECORD_MARGIN


def heartbeat_due(cache):
    last = cache["meta"].get("last_notified")
    if last is None:
        return True
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    return elapsed.days >= HEARTBEAT_DAYS


def describe(itinerary):
    lines = []
    for leg, label in (("outbound", "Outbound"), ("inbound", "Return")):
        entries = segments(itinerary, leg)
        if not entries:
            continue
        lines.append(f"{label}:")
        for entry in entries:
            layover = entry.get("layover")
            if layover:
                hours, minutes = divmod(layover["duration"] // 60, 60)
                lines.append(f"  ... {hours}h{minutes:02d}m layover (self-transfer)")
            seg = entry["segment"]
            departure = seg["source"]["localTime"].replace("T", " ")[:16]
            lines.append(
                f"  {seg['source']['station']['code']} -> "
                f"{seg['destination']['station']['code']} "
                f"({seg['carrier']['name']}) {departure}"
            )
    return "\n".join(lines)


def build_message(candidate, previous_price=None, record=None):
    total = candidate["total"]
    smash = total < SMASH_BUY_THRESHOLD
    is_record_low = candidate.get("record_low")

    if is_record_low and smash:
        tier = "RECORD LOW + SMASH BUY"
    elif is_record_low:
        tier = "RECORD LOW"
    elif smash:
        tier = "SMASH BUY"
    else:
        tier = "Fare alert"

    subject = (
        f"{tier}: {total:,.0f} {CURRENCY_LABEL} CPH -> Sapporo "
        f"({candidate['route_label']})"
    )

    lines = [f"{total:,.0f} {CURRENCY_LABEL} total, {candidate['route_label']}, 1 hold bag."]
    if is_record_low and record is not None:
        lines.append(f"Cheapest seen since polling began (previous best {record:,.0f}).")
    elif is_record_low:
        lines.append("First price recorded for this routing.")
    if smash:
        lines.append(f"Under the {SMASH_BUY_THRESHOLD:,.0f} {CURRENCY_LABEL} line. Book now.")
    if previous_price is not None:
        lines.append(f"Down from {previous_price:,.0f} {CURRENCY_LABEL} since the last alert.")

    lines += ["", describe(candidate["itinerary"])]
    addon = candidate.get("addon")
    if addon:
        lines += [
            "",
            f"Separate domestic hop (book yourself): Tokyo <-> CTS return "
            f"{addon['price']:,.0f} {CURRENCY_LABEL} on {addon['carrier']}",
            f"Long-haul {price_of(candidate['itinerary']):,.0f} + "
            f"domestic {addon['price']:,.0f} = {total:,.0f} {CURRENCY_LABEL}",
        ]
    lines += ["", f"Book: {booking_url(candidate['itinerary'])}"]
    return subject, "\n".join(lines)


def build_heartbeat(candidates, cache):
    """Sent when nothing has been worth alerting on for a while."""
    best_overall = min(candidates, key=lambda c: c["total"], default=None)
    subject = "Fare poller heartbeat" + (
        f": best now {best_overall['total']:,.0f} {CURRENCY_LABEL}" if best_overall else ""
    )

    lines = ["Still running. Nothing under the alert line."]
    for route, label in (("direct", "direct to CTS"), ("via_tokyo", "via Tokyo")):
        best = min(
            (c for c in candidates if c["route"] == route),
            key=lambda c: c["total"],
            default=None,
        )
        record = cache["meta"]["records"].get(route)
        if best is None:
            lines.append(f"  {label}: nothing with >=3h layovers right now")
            continue
        line = f"  {label}: best now {best['total']:,.0f} {CURRENCY_LABEL}"
        if record is not None:
            line += f" (record {record:,.0f})"
        lines.append(line)
    lines.append("")
    lines.append(
        f"Alerting below {ALERT_THRESHOLD:,.0f} / smash buy below "
        f"{SMASH_BUY_THRESHOLD:,.0f} {CURRENCY_LABEL}."
    )
    return subject, "\n".join(lines)


def push_urgent(subject, body, link):
    """High-priority phone push for smash-buy fares. Silently skipped if unset."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": subject,
                "Priority": "urgent",
                "Tags": "airplane",
                "Click": link,
            },
            timeout=15,
        ).raise_for_status()
    except Exception as exc:  # never let the push break the email path
        print(f"ntfy push failed: {exc}", file=sys.stderr)


def notify(subject, body):
    """Send one alert through Resend."""
    response = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "from": os.environ.get("ALERT_EMAIL_FROM") or DEFAULT_FROM,
            "to": [os.environ["ALERT_EMAIL_TO"]],
            "subject": subject,
            "text": body,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Resend rejected the email ({response.status_code}): {response.text}")


def gather():
    """Build a price-sorted list of candidate trips across both routings."""
    addon = domestic_hop()
    if addon:
        print(
            f"Domestic Tokyo <-> CTS add-on: {addon['price']:,.0f} "
            f"{CURRENCY_LABEL} ({addon['carrier']})"
        )
    else:
        print("No domestic hop priced; skipping the via-Tokyo routing.", file=sys.stderr)

    candidates = []
    for key, label, destination, extra in (
        ("direct", "direct to CTS", SAPPORO, None),
        ("via_tokyo", "via Tokyo", TOKYO, addon),
    ):
        if key == "via_tokyo" and addon is None:
            continue
        itineraries = search_all(ORIGIN, destination, OUTBOUND_WINDOW, INBOUND_WINDOW)
        legal = [i for i in itineraries if layover_ok(i)]
        print(
            f"{label}: {len(itineraries)} itineraries, {len(legal)} with >=3h layovers"
            + (
                f", cheapest legal {min(map(price_of, legal)):,.0f} {CURRENCY_LABEL}"
                if legal
                else ""
            )
        )
        for itinerary in legal:
            candidates.append(
                {
                    "route": key,
                    "route_label": label,
                    "itinerary": itinerary,
                    "addon": extra,
                    "total": price_of(itinerary) + (extra["price"] if extra else 0),
                }
            )
    return sorted(candidates, key=lambda c: c["total"])


def run_once(dry_run):
    """One search pass. Returns the number of alerts sent, or None if empty."""
    candidates = gather()
    if not candidates:
        return None

    best = candidates[0]
    print(f"Best total: {best['total']:,.0f} {CURRENCY_LABEL} ({best['route_label']})")

    cache = load_cache()
    alerts = 0

    for candidate in candidates:
        route = candidate["route"]
        total = candidate["total"]
        current_record = cache["meta"]["records"].get(route)
        record = current_record
        candidate["record_low"] = is_record(cache, route, total)

        # Update the running record immediately: candidates are sorted cheapest
        # first, so only the genuine best of this run can claim a record.
        if current_record is None or total < current_record:
            cache["meta"]["records"][route] = total

        if total >= ALERT_THRESHOLD:
            continue

        key = f"{route}::{signature(candidate['itinerary'])}"
        if not is_new_deal(cache, key, total):
            continue

        previous = cache["fares"].get(key, {}).get("price")
        if alerts < MAX_ALERTS_PER_RUN:
            subject, body = build_message(candidate, previous, record)
            if dry_run:
                print(f"--- {subject}\n{body}\n")
            else:
                notify(subject, body)
                if total < SMASH_BUY_THRESHOLD:
                    push_urgent(subject, body, booking_url(candidate["itinerary"]))
            alerts += 1
            cache["meta"]["last_notified"] = utc_now()

        cache["fares"][key] = {
            "price": total,
            "departure": departure_time(candidate["itinerary"]),
            "alerted_at": utc_now(),
        }

    if alerts == 0 and heartbeat_due(cache):
        subject, body = build_heartbeat(candidates, cache)
        if dry_run:
            print(f"--- {subject}\n{body}\n")
        else:
            notify(subject, body)
        cache["meta"]["last_notified"] = utc_now()
        print("Heartbeat posted.")

    save_cache(cache)
    return alerts


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    missing = [k for k in ("RESEND_API_KEY", "ALERT_EMAIL_TO") if not os.environ.get(k)]
    if missing and not dry_run:
        sys.exit(f"{' and '.join(missing)} must be set (or set DRY_RUN=1).")

    total_alerts = 0
    productive = 0

    for n in range(POLL_ITERATIONS):
        if n:
            time.sleep(POLL_INTERVAL_SECONDS)
        print(f"--- pass {n + 1}/{POLL_ITERATIONS} at {utc_now()}")
        try:
            alerts = run_once(dry_run)
        except Exception as exc:
            # One bad pass should not end a run that still has passes left.
            print(f"pass {n + 1} failed: {exc}", file=sys.stderr)
            continue
        if alerts is None:
            print("No itineraries with legal layovers this pass.", file=sys.stderr)
            continue
        productive += 1
        total_alerts += alerts

    # Only treat the run as broken if nothing worked at all: a green run that
    # quietly found nothing looks identical to "no deals today".
    if not productive:
        sys.exit(
            f"All {POLL_ITERATIONS} pass(es) failed or found nothing. "
            "Either both routes genuinely sold out, or the Kiwi schema moved."
        )

    print(
        f"{total_alerts} alert(s) sent across {productive}/{POLL_ITERATIONS} "
        f"successful passes; cache holds {len(load_cache()['fares'])} routings."
    )


if __name__ == "__main__":
    main()
