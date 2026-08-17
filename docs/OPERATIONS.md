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
