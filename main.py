"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Layout of this single file:
  1. configuration, indexes
  2. pure helpers (time zone, R1-R5 rules, rounding)  <- no database access, easy to unit test
  3. request models
  4. employees + attendance endpoints
  5. query / pipeline builders (shared by the real endpoints AND /admin/explain)
  6. analytics endpoints
  7. explain endpoint
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: datetimes read back from MongoDB are UTC-aware instead of naive.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists is a no-op."""
    # employees
    db.employees.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")
    db.employees.create_index([("joined_on", 1)], name="joined_on")
    # attendance_logs
    # unique (emp_code, date): the punch-in race guarantee + emp_code filters + monthly + $lookup
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique")
    # list sort (date desc, emp_code asc), date-range scans for leaderboard
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")
    # status filter + the same sort
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    # punch-out: "latest record of this employee with punch_in <= t"
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for attempt in range(10):
        try:
            ensure_indexes()
            break
        except PyMongoError as exc:  # MongoDB may still be starting
            log.warning("index creation failed (attempt %s): %s", attempt + 1, exc)
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Pure helpers: time zone, rules R1-R5, rounding
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
MAX_SHIFT = timedelta(hours=24)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: datetime) -> int:
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value, places: int) -> Decimal:
    """Half-up rounding on the decimal representation (R4, R8)."""
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _hm(hhmm: str) -> tuple:
    h, m = hhmm.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hm(shift_end) <= _hm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; for an overnight shift a punch-in earlier
    than shift_end belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and (local.hour, local.minute) < _hm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def _ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date_cls.fromisoformat(date_str) + timedelta(days=plus_days)
    h, m = _hm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    secs = int((as_utc(punch_in) - _ist_at(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    secs = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float(round_half_up(Decimal(secs) / Decimal(3600), 2))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes after shift end, only if >= 30. Overnight shifts end on the next day."""
    end = _ist_at(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    secs = int((as_utc(punch_out) - end).total_seconds())
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_BELOW


def compute_derived(emp: dict, date_str: str, status: str,
                    punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four derived fields for a record, from its final state."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in NON_PRESENCE or punch_in is None:
        return out
    ss, se = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    out["late_minutes"] = compute_late_minutes(punch_in, ss, date_str)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, ss, se, date_str)
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


def count_weekdays(start: date_cls, end: date_cls) -> int:
    """Mon-Fri days in [start, end] (R7). At most ~31 iterations; this is calendar math, not data."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def parse_date(value: str, name: str = "date") -> date_cls:
    if not DATE_RE.match(value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_bounds(month: str) -> tuple:
    if not MONTH_RE.match(month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    if y < 1:
        raise HTTPException(422, "month out of range")
    first = date_cls(y, m, 1)
    last = date_cls(y, m, calendar.monthrange(y, m)[1])
    return first, last


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # raises ValueError for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Serialisation (MongoDB document -> API JSON)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d.get("name"),
        "email": d.get("email"),
        "department": d.get("department"),
        "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"),
        "joined_on": d.get("joined_on"),
        "created_at": dt_to_ms(d["created_at"]) if d.get("created_at") else None,
    }


def record_out(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": _api_value(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                for k, c in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": _api_value(d.get("punch_in")),
        "punch_out": _api_value(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day")),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # MongoDB keeps ms
    try:
        db.employees.insert_one(doc)  # unique index decides the race, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp.get("shift_start", "09:30"), d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: one winner, rest 409
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    if ts <= p_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - p_in > MAX_SHIFT:
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    derived = compute_derived(emp, rec["date"], rec["status"], p_in, ts)
    # Atomic compare-and-set: only the request that still sees punch_out == null wins.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str,
    date: Annotated[str, Path(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")],
    body: RegularizeRequest,
):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    for _attempt in range(5):  # optimistic concurrency: retry if someone else changed the record
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no record for that date")

        old_status = rec["status"]
        old_in, old_out = as_utc(rec.get("punch_in")), as_utc(rec.get("punch_out"))
        new_status = body.status or old_status
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out

        if new_status in NON_PRESENCE:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, f"{new_status} cannot carry punch times")
            new_in = new_out = None
        elif new_in is None:
            raise HTTPException(422, f"{new_status} requires a punch_in")

        if new_in is not None:
            if body.punch_in is not None:
                shift_start = emp.get("shift_start", "09:30")
                shift_end = emp.get("shift_end", "18:30")
                if attendance_date(new_in, shift_start, shift_end) != date:
                    raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
                if new_out <= new_in:
                    raise HTTPException(422, "punch_out must be after punch_in")
                if new_out - new_in > MAX_SHIFT:
                    raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

        if (new_status, new_in, new_out) == (old_status, old_in, old_out):
            raise HTTPException(422, "request changes nothing")

        derived = compute_derived(emp, date, new_status, new_in, new_out)
        new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
        old_vals = {
            "status": old_status, "punch_in": old_in, "punch_out": old_out,
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0,
            "half_day": bool(rec.get("half_day")),
        }
        changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        guard = {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "history": {"$size": len(rec["history"])} if isinstance(rec.get("history"), list)
            else {"$exists": False},
        }
        updated = db.attendance_logs.find_one_and_update(
            guard,
            {"$set": new_vals, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Query / pipeline builders. The real endpoints and /admin/explain both call these,
# so the explained query is by construction the query that really runs.
# --------------------------------------------------------------------------- #
def attendance_list_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "projection": {"_id": 0},
    }


# Aggregation expression fragments (dates are "YYYY-MM-DD" strings in MongoDB)
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun..7=Sat
_IS_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_PRESENT_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _month_match(start: date_cls, end: date_cls) -> dict:
    return {"$gte": start.isoformat(), "$lte": end.isoformat()}


def employee_monthly_pipeline(emp_code: str, start: date_cls, end: date_cls) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": _month_match(start, end)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_VALUE, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start: date_cls, end: date_cls, department: Optional[str]) -> list:
    """Starts from employees (so people with zero logs are counted), joins their month of logs."""
    emp_match: dict = {"joined_on": {"$lte": end.isoformat()}}  # R9
    if department is not None:
        emp_match["department"] = department
    per_employee = [
        {"$match": {"date": _month_match(start, end)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_VALUE, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _LATE},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            # hours kept as integer hundredths so the average can be rounded exactly in Python
            "hours_cents": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "hours_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
        }},
    ]
    return [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "stats"}},
        {"$set": {"s": {"$arrayElemAt": ["$stats", 0]}}},  # missing when the employee has no logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
            "hours_cents": {"$sum": "$s.hours_cents"},
            "hours_n": {"$sum": "$s.hours_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: date_cls, end: date_cls, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": _month_match(start, end), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name",
                      "department": "$e.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(codes: list, joined: list, d_from: date_cls, d_to: date_cls) -> list:
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [_IS_PRESENT, _PRESENT_VALUE, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        }},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one document per calendar day, weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {"headcount": {"$size": {"$filter": {
            "input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        # attendance_rate scaled by 10^4 and rounded half-up with integer arithmetic (exact):
        #   floor(10000 * p / h + 1/2) = floor((10000 * (2p) + h) / (2h))
        {"$set": {"rate_k": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [{"$multiply": ["$present_count", 2]}, 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "k_sum": {"$sum": "$rate_k", "window": window},
            "k_n": {"$sum": {"$cond": [{"$ne": ["$rate_k", None]}, 1, 0]}, "window": window},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "rate_k": 1, "k_sum": 1, "k_n": 1}},
    ]


def trend_inputs(department: str, d_from: str, d_to: str) -> tuple:
    f, t = parse_date(d_from, "from"), parse_date(d_to, "to")
    if t < f:
        raise HTTPException(422, "to must not be before from")
    if (t - f).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return f, t, [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


# --------------------------------------------------------------------------- #
# Attendance list (uses the builder above) and analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    q = spec["filter"]
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = (db.attendance_logs.find(q, spec["projection"])
           .sort(list(spec["sort"].items())).skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    s = rows[0] if rows else {}
    joined = date_cls.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last) if joined <= last else 0  # R7
    present = Decimal(str(s.get("present_days", 0)))
    pct = None if working_days == 0 else float(round_half_up(present / working_days * 100, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(round_half_up(present, 2)),
        "leave_days": int(s.get("leave_days", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        n = int(r.get("hours_n", 0))
        avg = None if n == 0 else float(round_half_up(Decimal(int(r["hours_cents"])) / (100 * n), 2))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(round_half_up(r.get("present_days", 0), 2)),
            "avg_work_hours": avg,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: str = Query(..., alias="from"),
                     to: str = Query(...)):
    f, t, codes, joined = trend_inputs(department, from_, to)
    rows = db.attendance_logs.aggregate(department_trend_pipeline(codes, joined, f, t))
    items = []
    for r in rows:
        k = r["rate_k"]
        n = int(r["k_n"])
        moving = None if n == 0 else ((2 * int(r["k_sum"]) + n) // (2 * n)) / 10000  # half-up mean, exact
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if k is None else int(k) / 10000,
            "moving_avg_7d": moving,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary",
                          "late_leaderboard", "department_trend"]


def _require(value, name: str):
    if value is None:
        raise HTTPException(422, f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        collection, inner = "attendance_logs", {"find": "attendance_logs", **spec}
    elif endpoint == "employee_monthly":
        first, last = month_bounds(_require(month, "month"))
        collection = "attendance_logs"
        inner = {"aggregate": collection, "cursor": {},
                 "pipeline": employee_monthly_pipeline(_require(emp_code, "emp_code"), first, last)}
    elif endpoint == "department_summary":
        first, last = month_bounds(_require(month, "month"))
        collection = "employees"
        inner = {"aggregate": collection, "cursor": {},
                 "pipeline": department_summary_pipeline(first, last, department)}
    elif endpoint == "late_leaderboard":
        first, last = month_bounds(_require(month, "month"))
        collection = "attendance_logs"
        inner = {"aggregate": collection, "cursor": {},
                 "pipeline": late_leaderboard_pipeline(first, last, limit, department)}
    else:  # department_trend
        f, t, codes, joined = trend_inputs(_require(department, "department"),
                                           _require(from_, "from"), _require(to, "to"))
        collection = "attendance_logs"
        inner = {"aggregate": collection, "cursor": {}, "pipeline": department_trend_pipeline(codes, joined, f, t)}

    try:
        out = db.command({"explain": inner, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc}")
    plain = json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))
    return JSONResponse({"endpoint": endpoint, "collection": collection, "explain": plain})
