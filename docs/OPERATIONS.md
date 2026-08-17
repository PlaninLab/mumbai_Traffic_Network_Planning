# Operations Runbook — collecting data & running the dashboard

Two independent processes:

| Process | What it does | Long-running? |
|---------|--------------|---------------|
| **Collector** (`collect_day` / scheduled tasks) | Pulls live speeds → SQLite store → refreshes summary | Yes (all day) or fire-and-forget |
| **Web server** (`uvicorn`) | Serves the dashboard + report at `:8000` | Yes (always on) |

They do not depend on each other — the dashboard just reads whatever the collector has written.

---

## 0. One-time prerequisites

1. Keys in the git-ignored `.env` (never commit it):
   ```
   TOMTOM_API_KEY=...
   HERE_API_KEY=...          # collect_day uses HERE by default
   GOOGLE_MAPS_API_KEY=...   # only for OD cost matrices
   ```
2. Dependencies installed once:
   ```bash
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Sanity-check a single reading before automating:
   ```bash
   python -m src.data.here_client flow --point 19.115,72.860
   ```

---

## 1. Start the data collector

**Always preview the day's plan + API-call budget first (no calls made):**
```bash
python -m src.data.collect_day --dry-run
```

### Option A — run it in the foreground (you watch it)
```bash
python -m src.data.collect_day --n 25 --until 23:00
```
- Samples every **10 min in peak** windows, **15 min otherwise**, tagging each reading's segment.
- Writes to `data/processed/traffic.db` and refreshes the dashboard after every reading.
- It **stops itself** at `--until` (23:00 here). Leave the window open until then.

### Option B — run it in the background (Windows)
```bash
Start-Process -WindowStyle Hidden -FilePath ".venv\Scripts\python.exe" ^
  -ArgumentList "-m","src.data.collect_day","--n","25","--until","23:00" ^
  -RedirectStandardOutput "data\collect.log" -RedirectStandardError "data\collect.err"
```

### Option C — fully automated (recommended, no babysitting)
Register weekday jobs once (from an **admin** PowerShell); Windows runs them Mon–Fri:
```bash
powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1 -FullDay
```
This launches the full-day loop at 06:00 every weekday. Nothing to start manually.

---

## 2. When to switch the collector OFF, and how

**When:** the model needs at least a few complete **weekday** cycles that include a real
**peak** and a real **avg** reading (that's what unlocks BPR β calibration). Practical target:
let it run **3–5 full weekdays**. After that, more data only sharpens the fit — you can stop
any time. Off-peak/weekend readings add little.

**How to stop, by how you started it:**

| Started with | Stop it by |
|--------------|-----------|
| Foreground (Option A) | Press **Ctrl-C** in that window (or just let it reach `--until` and exit) |
| Background (Option B) | `Get-Process python \| Where-Object {$_.Path -like '*mumbai*'} \| Stop-Process` — or Task Manager → end the `python.exe` running collect_day |
| Scheduled tasks (Option C) | `powershell -ExecutionPolicy Bypass -File scripts\register_weekday_tasks.ps1 -Remove` (unregisters all MumbaiTraffic-* jobs) |

> The loop also self-terminates every day at `--until`, so a single day never needs a manual stop.

**Confirm it stopped / see what you collected:**
```bash
python -m src.data.store --info      # rows + per-segment counts (peak/avg should be > 0)
```

---

## 3. Start / stop the web dashboard

**Start (keep running):**
```bash
uvicorn src.web.app:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000 — dashboard updates automatically as the collector writes data.

**Stop:** press **Ctrl-C** in that window. (Background/prod: run under Docker — `docker run -p 8000:8000 mumbai-traffic` — and stop with `docker stop <id>`.)

The web server is safe to leave on 24/7; it never calls any paid API.

---

## 4. Once you have peak + avg data — run the model on real inputs
```bash
python -m src.demand.calibration                       # β should now lift off its floor
python -m src.scenarios.evaluate --cost-source google  # scenarios on real Google OD times
```

---

## Quick reference

```bash
# preview plan/budget          python -m src.data.collect_day --dry-run
# collect all day (HERE)       python -m src.data.collect_day --n 25 --until 23:00
# coarser nights               ... --peak-interval 15 --offpeak-interval 15 --night-interval 60
# cap the monthly spend        ... --max-calls-month 38000   (or HERE_MONTHLY_CALL_LIMIT)
# automate weekdays            powershell ... register_weekday_tasks.ps1 -FullDay
# stop automation              powershell ... register_weekday_tasks.ps1 -Remove
# how much data so far         python -m src.data.store --info
# API calls used this month    python -m src.data.budget --status --provider here
# dashboard                    uvicorn src.web.app:app --host 0.0.0.0 --port 8000
# what has been collected      http://localhost:8000/data
# scenarios on real OD         python -m src.scenarios.evaluate --cost-source google
```

---

## 5. Spend control and the data inventory

**Cap the API spend.** The collector takes a reading the moment it starts, so a crash
that repeats under a restart policy would bill one sweep per restart. Set a monthly cap
and the collector refuses to sweep past it:

```bash
export HERE_MONTHLY_CALL_LIMIT=38000     # or --max-calls-month on the CLI
python -m src.data.budget --status --provider here
```

The count lives in `traffic.db`, so it survives a restart. Calls are reserved **before**
each sweep, which is what makes the cap hold against a crash loop. Months are keyed in
UTC. This is a client-side guard — also set a spending alert with the provider, which is
the hard stop.

When the cap is reached the collector **holds** rather than exiting: it keeps its schedule,
makes no calls, and resumes when the month rolls over. `/api/health` reports
`budget_exhausted`, so a monitor can tell "out of quota" apart from "collector is dead".

**See what has been collected** at `/data` on the running dashboard — totals, per-day
coverage with the peak/avg/off-peak split, the latest readings, and the per-point corridor
profile. It reads the store live, so it fills in as the collector writes.

---

## 6. Provider outages and rate limits

The collector stops rather than retrying into a wall. On a 429, a rejected key, a 5xx
or an unreachable host it **aborts the sweep**, returns the calls it never issued to the
monthly budget, records the failure, and holds off:

| Consecutive failures | 1 | 2 | 3 | 4 | 5+ |
|---|---|---|---|---|---|
| Skips sweeps for | 15 min | 30 min | 60 min | 120 min | 240 min |

A rate limit or a rejected key stops the sweep on the first point — neither fixes itself
mid-sweep. A network blip or a 5xx allows three points before giving up.

While holding, `collect_day` skips its sweep at the top of the loop: no reservation, no
requests, and the sampling grid is unchanged — it simply misses the slots the provider
cannot serve. One sweep that returns data clears the hold.

```bash
python -m src.data.incidents --status --provider here   # hold state + recent failures
python -m src.data.incidents --clear  --provider here   # resume now, do not wait
```

The failures are also on `/data` under **Provider health**, and `/api/health` reports
`provider_hold`, `provider_hold_minutes` and `consecutive_failures` — so a monitor can
tell a provider outage apart from a dead collector and from an exhausted quota.

Use `--request-pause SECONDS` to space out the individual point requests inside a sweep
if you ever hit a per-second rate limit.

### Hard stop after repeated failure

The timed back-off handles a blip. A provider that is simply broken needs a person, so
after **25 failed calls** since the last successful sweep (`--max-failed-calls`, or
`HERE_FAILURE_LATCH`) collection **stops** and does not restart on its own — not on a
timer, not on a container restart.

```bash
python -m src.data.incidents --status --provider here    # is it stopped, and why
python -m src.data.incidents --resume --provider here    # resume collection
```

From the dashboard: `/data` → **Provider health** → *Resume collection*. That button
restarts spending against a metered API, so it is only shown when `ADMIN_TOKEN` is set on
the web service, and the endpoint refuses outright when it is not. Set it to something
long and random:

```
ADMIN_TOKEN=<long random string>
```

`/api/health` reports `collection_stopped`, `stopped_reason`, `failed_calls` and
`failed_calls_limit`.

### Evidence for a billing dispute

Every failure records the provider's OWN identifiers, captured at the moment it happened:
`X-Correlation-ID`, `X-Request-Id`, the `x-slo` tier the call was rated against, their
`Date` header (their clock, which is what their logs use), our measured latency and their
error payload.

```bash
python -m src.data.incidents --outages --provider here   # grouped into windows
python -m src.data.incidents --export  --provider here   # CSV to attach to a ticket
```

The outage view is what an argument is made from: *"you were unavailable from X to Y, we
made N calls into it, here are your correlation IDs, the SLO tier was `traffic-v7-flow-small`."*

Note what is realistic: providers do not normally bill 429s or 5xx responses, so the
number worth disputing is usually **not** the failed calls themselves — it is a service
credit against the SLO for the outage window. The `billable_calls` column counts requests
that actually reached them, which is the conservative figure to quote.
