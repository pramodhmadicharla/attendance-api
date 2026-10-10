"""Smoke test for the running API. NOT part of the submission zip.

1. Use an EMPTY database:   MONGO_DB=attendance_smoke uvicorn app.main:app --port 8000
2. Run:                     python smoke_test.py            (stdlib only; BASE_URL env var to override)
"""
import json, os, threading, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

BASE = os.getenv("BASE_URL", "http://localhost:8000")
IST = timezone(timedelta(hours=5, minutes=30))
fails = 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


def check(name, got, exp):
    global fails
    ok = got == exp
    fails += 0 if ok else 1
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  got={got!r} expected={exp!r}"))


def emp(code, dept, joined, ss="09:30", se="18:30"):
    return call("POST", "/employees", {"emp_code": code, "name": "N " + code, "email": f"{code}@x.com",
                                       "department": dept, "joined_on": joined, "shift_start": ss, "shift_end": se})


check("health", call("GET", "/health")[0], 200)
check("create 9001", emp("EMP9001", "Engineering", "2026-01-05")[0], 201)
check("duplicate -> 409", emp("EMP9001", "Engineering", "2026-01-05")[0], 409)
check("bad code -> 422", emp("X1", "Engineering", "2026-01-05")[0], 422)
check("same shift -> 422", emp("EMP9100", "Engineering", "2026-01-05", "09:00", "09:00")[0], 422)
emp("EMP9002", "Engineering", "2026-07-13"); emp("EMP9003", "Engineering", "2026-01-05")
emp("EMP9004", "Sales", "2026-02-01"); emp("EMP9005", "Support", "2026-03-10", "22:00", "06:00")
s, body = call("GET", "/employees?page=1&page_size=2")
check("employees page total/size", (body["total"], len(body["items"])), (5, 2))
check("employees dept total", call("GET", "/employees?department=Sales")[1]["total"], 1)

# punch-in / punch-out rules
s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 40, 1)})
check("punch-in 09:40:01", (s, r["date"], r["late_minutes"]), (201, "2026-07-06", 10))
check("punch-in twice -> 409", call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 41)})[0], 409)
check("unknown emp -> 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP0000"})[0], 404)
check("seconds-looking -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": 1783312500})[0], 422)
check("float -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": 1783312500000.5})[0], 422)
check("ABSENT status -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "status": "ABSENT"})[0], 422)
check("punch-out <= in -> 422", call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 40, 1)})[0], 422)
s, r = call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 19, 10)})
check("punch-out 19:10", (s, r["work_hours"], r["overtime_minutes"], r["half_day"]), (200, 9.5, 40, False))
check("punch-out twice -> 409", call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 19, 11)})[0], 409)

# overnight shift
s, r = call("POST", "/attendance/punch-in", {"emp_code": "EMP9005", "punched_at": ms(2026, 7, 6, 21, 55)})
check("overnight date", (s, r["date"]), (201, "2026-07-06"))
s, r = call("POST", "/attendance/punch-out", {"emp_code": "EMP9005", "punched_at": ms(2026, 7, 7, 6, 40)})
check("overnight punch-out", (s, r["work_hours"], r["overtime_minutes"]), (200, 8.75, 40))

# races
def race(path, body, n=10):
    out = []
    ts = [threading.Thread(target=lambda: out.append(call("POST", path, body)[0])) for _ in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return sorted(out)
res = race("/attendance/punch-in", {"emp_code": "EMP9004", "punched_at": ms(2026, 7, 7, 9, 30)})
check("punch-in race", (res.count(201), res.count(409)), (1, 9))
res = race("/attendance/punch-out", {"emp_code": "EMP9004", "punched_at": ms(2026, 7, 7, 18, 30)})
check("punch-out race", (res.count(200), res.count(409)), (1, 9))
res = race("/employees", {"emp_code": "EMP9500", "name": "R", "email": "r@x.com", "department": "Ops", "joined_on": "2026-01-01"})
check("create race", (res.count(201), res.count(409)), (1, 9))

# regularize
s, r = call("PATCH", "/attendance/EMP9001/2026-07-06",
            {"punch_in": ms(2026, 7, 6, 9, 28), "reason": "biometric glitch", "regularized_by": "hr"})
check("patch", (s, r["late_minutes"], len(r["history"])), (200, 0, 1))
check("patch changes keys", sorted(r["history"][0]["changes"]), ["late_minutes", "punch_in", "work_hours"])
check("patch from/to are epoch ms", r["history"][0]["changes"]["punch_in"]["to"], ms(2026, 7, 6, 9, 28))
check("patch no-op -> 422", call("PATCH", "/attendance/EMP9001/2026-07-06",
      {"punch_in": ms(2026, 7, 6, 9, 28), "reason": "same again", "regularized_by": "hr"})[0], 422)
check("patch ABSENT + punch -> 422", call("PATCH", "/attendance/EMP9001/2026-07-06",
      {"status": "ABSENT", "punch_in": ms(2026, 7, 6, 9, 0), "reason": "bad combo", "regularized_by": "hr"})[0], 422)
check("patch no record -> 404", call("PATCH", "/attendance/EMP9001/2026-07-20",
      {"status": "LEAVE", "reason": "no record", "regularized_by": "hr"})[0], 404)
check("patch other day on punch_in -> 422", call("PATCH", "/attendance/EMP9001/2026-07-06",
      {"punch_in": ms(2026, 7, 7, 9, 0), "reason": "wrong day", "regularized_by": "hr"})[0], 422)
check("patch -> LEAVE clears", call("PATCH", "/attendance/EMP9001/2026-07-06",
      {"status": "LEAVE", "reason": "mistake fixed", "regularized_by": "hr"})[1]["punch_in"], None)
call("PATCH", "/attendance/EMP9001/2026-07-06",
     {"status": "PRESENT", "punch_in": ms(2026, 7, 6, 9, 28), "reason": "restore it", "regularized_by": "hr"})
call("PATCH", "/attendance/EMP9001/2026-07-06",
     {"punch_out": ms(2026, 7, 6, 19, 10), "reason": "restore out", "regularized_by": "hr"})

# weekend late record + tie for the leaderboard
call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 11, 10, 0)})   # Saturday, late 30
call("POST", "/attendance/punch-in", {"emp_code": "EMP9004", "punched_at": ms(2026, 7, 8, 10, 0)})    # late 30

s, a = call("GET", "/attendance?emp_code=EMP9001&page_size=100")
check("list sorted date desc", [x["date"] for x in a["items"]], sorted([x["date"] for x in a["items"]], reverse=True))
check("list invalid range -> 422", call("GET", "/attendance?date_from=2026-08-01&date_to=2026-07-01")[0], 422)
check("list page_size 101 -> 422", call("GET", "/attendance?page_size=101")[0], 422)

s, m = call("GET", "/analytics/employees/EMP9001/monthly?month=2026-07")
check("monthly", (m["working_days"], m["present_days"], m["late_count"], m["total_late_minutes"], m["attendance_pct"]),
      (23, 1.0, 1, 30, 4.35))
check("monthly unknown -> 404", call("GET", "/analytics/employees/EMP0000/monthly?month=2026-07")[0], 404)
check("monthly bad month -> 422", call("GET", "/analytics/employees/EMP9001/monthly?month=2026-13")[0], 422)

s, d = call("GET", "/analytics/departments/summary?month=2026-07")
eng = next(i for i in d["items"] if i["department"] == "Engineering")
check("dept summary headcount (incl. no-log + joiner)", eng["headcount"], 3)
check("dept sorted", [i["department"] for i in d["items"]], sorted(i["department"] for i in d["items"]))

s, lb = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=1")
check("leaderboard tie at cutoff", ([i["rank"] for i in lb["items"]], [i["emp_code"] for i in lb["items"]]),
      ([1, 1], ["EMP9001", "EMP9004"]))

s, t = call("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-14")
rows = t["items"]
check("trend one row per day", len(rows), 14)
sat = next(r for r in rows if r["date"] == "2026-07-11")
check("weekend row", (sat["is_working_day"], sat["attendance_rate"]), (False, None))
check("empty working day rate 0.0", next(r for r in rows if r["date"] == "2026-07-01")["attendance_rate"], 0.0)
check("headcount grows on join day", (next(r for r in rows if r["date"] == "2026-07-10")["headcount"],
                                      next(r for r in rows if r["date"] == "2026-07-13")["headcount"]), (2, 3))
check("trend range > 92 days -> 422", call("GET", "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")[0], 422)
check("trend unknown dept -> 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02")[0], 404)

# explain: IXSCAN and no COLLSCAN (run again on a ~100k dataset to be sure)
for ep, q in [("attendance_list", ""), ("attendance_list", "?emp_code=EMP9001"),
              ("employee_monthly", "?emp_code=EMP9001&month=2026-07"), ("department_summary", "?month=2026-07"),
              ("late_leaderboard", "?month=2026-07"), ("department_trend", "?department=Engineering&from=2026-07-01&to=2026-07-14")]:
    s, e = call("GET", f"/admin/explain/{ep}{q}")
    txt = json.dumps(e)
    check(f"explain {ep}{q}", (s, "IXSCAN" in txt, "COLLSCAN" in txt), (200, True, False))
check("explain missing param -> 422", call("GET", "/admin/explain/employee_monthly?month=2026-07")[0], 422)

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
