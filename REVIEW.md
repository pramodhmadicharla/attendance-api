# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed. All fixes are in the final `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floors to minutes *then* tests `> 10`, so 09:40:01 (601 s) gives 10 -> "not > 10" -> 0. R2 says strictly more than 10:00 (600 s). | Punch in 09:40:01 on a 09:30 shift: expect 10, got 0. | Compare in seconds (`secs > 600`), then floor to minutes. |
| 2 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour, minute)` in the punch-in's own time zone (UTC from Mongo), but shift times are IST. Off by 5h30. | Any punch-in whose UTC and IST clock times differ. | Build the shift start as an IST datetime on the attendance date. |
| 3 | `compute_late_minutes` | No overnight handling: for a 22:00 shift a punch-in at 00:30 is compared with 22:00 the *same* day (negative -> 0). | Overnight employee punching in after midnight. | Late is measured from shift start on the *attendance date* (the previous day for such punch-ins, see #20). |
| 4 | helpers | Sub-second parts are never truncated (R1). | Punch at `...:00.900` is treated as `:00.900`. | `ms_to_dt` truncates to whole seconds before anything else; the truncated value is stored. |
| 5 | `compute_work_hours` | Python `round()` is banker's/float rounding, not half-up (R4). | 29,250 s = 8.125 h -> 8.12, expected 8.13. | `Decimal` with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3): `max(0, ...)` reports 5 min of overtime. | Punch out 18:35, expect 0. | Return the floored minutes only when `>= 30`, else 0. |
| 7 | `compute_overtime` | Shift end is put on the attendance date, so for overnight shifts it lands 24h too early (hundreds of overtime minutes). | Overnight punch-out 06:40 next day: expect 40. | Add one day to the shift end when `shift_end <= shift_start`. |
| 8 | `compute_overtime` | Subtracts an aware IST datetime from a naive datetime (what PyMongo returns by default) -> `TypeError` -> 500. | Any punch-out against a stored record. | Client uses `tz_aware=True`; helpers normalise with `as_utc`. |
| 9 | `MongoClient(...)` | Not `tz_aware`, so every datetime read back is naive. Root cause of #8. | `rec["punch_in"].tzinfo is None`. | `MongoClient(..., tz_aware=True)`. |
| 10 | startup | No indexes at all: no unique `emp_code`, no unique `(emp_code, date)`, nothing for the list sort. Root of the races and the slow list. | `index_information()` shows only `_id_`. | `ensure_indexes()` in the FastAPI lifespan, idempotent. |
| 11 | `/health` | Always returns ok, never pings MongoDB (spec: 503 when it is down). | Stop MongoDB, call `/health`. | `ping`, map failures to 503. |
| 12 | `EmployeeIn` | No validation: `emp_code` format, e-mail, `HH:MM`, real `joined_on`, lengths, `shift_start != shift_end`. | `joined_on: "2026-13-45"` returns 201. | Pydantic constraints plus validators -> 422. |
| 13 | `PunchInIn` | `status` is any string (accepts `ABSENT`, garbage); `punched_at` accepts floats / seconds-looking values. | `status: "ABSENT"` or `punched_at: 1783312500` returns 201. | `Literal` presence statuses; strict int in the epoch-ms range. |
| 14 | `create_employee` | Check-then-insert race. Parallel creates can all return 201. | 20 parallel POSTs, same `emp_code`. | Unique index + `DuplicateKeyError` -> 409. |
| 15 | `create_employee` | `created_at = datetime.now()` is local and naive; the response returns a datetime, not epoch ms. | Compare the response with the contract. | UTC, ms precision, serialised as epoch ms. |
| 16 | `list_employees` | `skip = page * page_size`: page 1 skips the first page. | Create 3 employees, `GET /employees` returns an empty page. | `skip = (page - 1) * page_size`. |
| 17 | `list_employees` | `total` is `count_documents({})`, ignoring the department filter (R10). | Filter by department, compare `total` with `items`. | Count with the same filter. |
| 18 | `list_employees` | No sort (unstable pages) and no bounds on `page` / `page_size`; `page=-1` -> negative skip -> 500. | `?page=-1`, `?page_size=100000`. | Sort by `emp_code`; `ge=1`, `le=100` -> 422. |
| 19 | `punch_in` | Unknown employee leaves `emp = None`, `emp["shift_start"]` raises -> 500 instead of 404. | POST an unknown `emp_code`. | Explicit 404. |
| 20 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server's local zone and float math; `ts.date()` is not the IST date; no overnight rule (R1). | Run the server in UTC, punch in 00:10 IST: wrong date. | `ms_to_dt` (UTC, whole seconds) + `attendance_date()` (IST, overnight rule). |
| 21 | `punch_in` | Check-then-insert race: parallel punch-ins create duplicate days. | Parallel requests. | Unique `(emp_code, date)` index, one winner, rest 409. |
| 22 | `punch_in` | Response adds `id` (the internal `_id`) and returns raw datetimes. | Inspect the response. | `record_out()` removes `_id` and emits epoch ms. |
| 23 | `list_attendance` | Loads the whole collection, sorts and slices in Python. Very slow at 100k rows. | Time it on 100k records, or `explain` shows COLLSCAN. | DB-side sort/skip/limit backed by indexes; `count_documents` on the same filter. |
| 24 | `list_attendance` | Sorts by `date` only; contract says `date` desc then `emp_code` asc. | Two employees on the same date. | Sort `{date: -1, emp_code: 1}`. |
| 25 | `list_attendance` | Exposes `id`, raw datetimes, and does not default `history` / `half_day` for legacy records. | GET the legacy EMP0002 record. | `record_out()` defaults and normalises. |
| 26 | `list_attendance` | No validation: `date_from > date_to`, bad dates, bad `status`, `page=0` (negative slice). | `?date_from=2026-08-01&date_to=2026-07-01`. | 422 for each. |

## Looked at and judged *not* a defect
- `load_dotenv()` does not override variables that are already set, so "real environment variables win" already holds.
- Plain `def` endpoints with synchronous PyMongo are fine: FastAPI runs them in a thread pool.
- `status_code=201` on the create endpoints is correct.
- `doc.pop("_id", None)` after `insert_one` is needed because PyMongo adds `_id` to the dict. (Replaced by `record_out`.)
