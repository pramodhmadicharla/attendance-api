# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # or export MONGO_URI / MONGO_DB (real env vars win over .env)
uvicorn app.main:app --port 8000
```
Needs Python 3.11+ and MongoDB 6.0+ (uses `$setWindowFields`, `$densify`, `$lookup` with `localField` + `pipeline`).
Indexes are created idempotently at startup; no manual steps.

## What is implemented
Every endpoint in `openapi.yaml`: health, employees (create/list), punch-in, punch-out, list attendance,
regularize (PATCH), the four analytics endpoints (employee monthly, department summary, late leaderboard,
department trend) and `/admin/explain/{endpoint}`.

## Notes
- Concurrency: unique indexes decide the punch-in / employee-create races; punch-out is an atomic
  compare-and-set on `punch_out == null`; PATCH uses optimistic concurrency (guards on the values it read and the
  history length, retries up to 5 times, then 409).
- Rounding that must be half-up (R4, R8, attendance rate, moving average) is done with `Decimal` or exact integer
  arithmetic, never Python `round()` or MongoDB `$round` (which rounds half to even).
- `REVIEW.md` lists the defects found in the starter; `DECISIONS.md` answers the five questions.

## Testing status
The pure rule logic (R1-R5, rounding, weekday counting) was unit-tested against the examples in the contract and the
nine sample documents. Please see "Known limits" below for anything not verified.

## Known limits
- Nothing is intentionally missing from the contract.
- The aggregation pipelines were written for MongoDB 7.0 semantics; if you see a pipeline error on an older 6.0.x
  build, please report it.
