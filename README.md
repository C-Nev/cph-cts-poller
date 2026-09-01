# CPH -> CTS fare poller

Polls Kiwi.com every 4 hours for Copenhagen -> Sapporo round trips departing
1-14 Feb 2027 and returning 20-31 Mar 2027, and emails matches to you.
All prices include one hold bag.

Two routings are priced and compared on total cost:

- **direct** - CPH -> CTS on a single ticket
- **via Tokyo** - CPH -> Tokyo, plus a separately booked Tokyo <-> CTS domestic
  return added on as a flat extra

The via-Tokyo total has consistently come in several thousand DKK below the
direct ticket, so both are tracked and whichever is cheaper wins.

## Data source

Not Tequila. Kiwi closed the Tequila developer programme to self-serve signups —
it is invite-only, so the walkthrough's "register and copy your API key" step is
a dead end. This instead calls `api.skypicker.com/umbrella/v2/graphql`, the
endpoint kiwi.com's own front end uses. No key, no signup, same virtual-interline
inventory. It is undocumented and unversioned, so treat a schema change as a
"when", not an "if" — the run fails loudly rather than silently alerting nothing.

## Setup

Alerts go out over Gmail SMTP. Add two repository *secrets* (Settings >
Secrets and variables > Actions > New repository secret):

| Secret | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | a Google **app password**, not your account password |

App passwords need 2-Step Verification switched on, then are generated at
https://myaccount.google.com/apppasswords. Revoking one there kills only this
poller's access, nothing else.

Thresholds and the destination address are repository *variables*, not secrets,
so they can be changed without editing code:

| Variable | Default | Meaning |
|---|---|---|
| `ALERT_THRESHOLD` | 8500 | Email below this DKK total |
| `SMASH_BUY_THRESHOLD` | 7800 | Flag as a smash buy below this |
| `ALERT_EMAIL_TO` | your `SMTP_USER` | Send alerts somewhere other than the sending account |

These replace the 5500/4500 from the original spec, which nothing on this route
comes close to. Current floor with a hold bag and 3h layovers is ~8,960 DKK via
Tokyo and ~14,400 direct. The same long-haul in shoulder season prices at ~7,150,
so the realistic headroom is 1,000-1,800 DKK, not more.

## Local run

```
pip install -r requirements.txt
DRY_RUN=1 python poller.py
```

`DRY_RUN=1` prints alerts instead of emailing them and needs no credentials.

## Seen-fares cache

`seen_fares.json` records each routing that has been alerted on, keyed by
routing type plus carrier + airport + departure time (not price). A routing re-alerts only if it
drops at least 250 DKK below the last alerted price. Entries are pruned once
their outbound departure is in the past. The workflow commits the file back to
the repo after each run, which doubles as the repository activity GitHub
requires to keep a cron schedule alive.

## Knowing it is still alive

Three layers, because a fare poller that dies quietly is worse than no poller:

1. **Heartbeat.** If nothing has been sent in 7 days, the next run emails a
   status summary with the current best and the record low per routing. Silence
   in your inbox for more than a week therefore means something is broken, not
   that fares are high.
2. **Loud failure.** A run that finds zero itineraries with legal layovers exits
   non-zero rather than reporting success. That is the dangerous case - it looks
   identical to "no deals" but usually means the Kiwi schema moved.
3. **Failure email.** Any failed run emails you with a link to the Actions log,
   subject line `Fare poller FAILED`, so a break arrives the same way a deal does.

`seen_fares.json` is rewritten with a `last_run` timestamp every run, so the
workflow always has something to commit. That keeps the repository active and
stops GitHub disabling the cron schedule after 60 days of quiet.

## Alert tiers

| Tier | When |
|---|---|
| `Fare alert` | Total below `ALERT_THRESHOLD` |
| `SMASH BUY` | Total below `SMASH_BUY_THRESHOLD` |
| `RECORD LOW` | Cheaper than anything recorded for that routing (by 100 DKK) |

Record lows decorate an alert rather than triggering one - a record low that is
still expensive shows up in the weekly heartbeat instead of landing in your inbox.

The tier is the first thing in the subject line, so the inbox list alone tells
you whether something needs acting on.
