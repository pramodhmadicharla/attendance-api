# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique (identity, race-safe create, `$lookup` target); `(department, emp_code)` for the filtered, sorted list; `(department, joined_on)` and `(joined_on)` for headcount. On logs: unique `(emp_code, date)` (one record per day, monthly and trend queries); `(date desc, emp_code)` (list sort, leaderboard month scan); `(status, date desc, emp_code)` (status filter); `(emp_code, punch_in desc)` (find the open record at punch-out). Rejected: an index on `late_minutes`; the leaderboard already narrows by date, and the extra index would slow every write.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed; the other raises `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.

3. **Ties.** Rank is computed first with `$rank` (standard competition ranking), then I keep every row with `rank <= limit`. Employees tied at the cutoff all appear, so the list can be longer than `limit`.

4. **Headcount.** The department summary starts from `employees` (joined on or before month end) and `$lookup`s each person's logs, so someone with no logs still counts as one head.

5. **At 100x data.** I would pre-aggregate daily and monthly totals per employee, update them as records change, and read analytics from those instead of scanning raw logs. I would also stop passing the department's employee list into the trend pipeline.
