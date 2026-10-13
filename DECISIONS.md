# DECISIONS

**1. Indexes.** `employees`: unique `emp_code` (identity, duplicate -> 409), `(department, emp_code)`, `(department, joined_on)` and `joined_on` for listing and headcount. `attendance_logs`: unique `(emp_code, date)` (race guard, monthly and lookup queries), `(date -1, emp_code)` for the list sort and month scans, `(status, date -1, emp_code)` for status filters, `(emp_code, punch_in -1)` for punch-out.

**2. Two identical punch-ins at the same instant.** Both compute the same attendance date and try to insert. The unique `(emp_code, date)` index lets exactly one insert succeed; the other gets DuplicateKeyError and I return 409. There is no check-then-insert.

**3. Tie at the leaderboard cutoff.** `$setWindowFields` with `$rank` gives competition ranking (1, 2, 2, 4). `limit` filters `rank <= limit` after ranking, so everyone tied at the cutoff is returned; limit 2 over 1, 2, 2, 4 returns three rows.

**4. Employees with no logs.** The summary pipeline starts from `employees` (`joined_on` <= month end), `$lookup`s the logs and `$unwind`s with preserveNullAndEmptyArrays. Headcount is the set of distinct `emp_code`s, so people with zero logs count and later joiners do not.

**5. 100x the data.** Pre-aggregated monthly rollups instead of scanning logs, department copied onto each log (no `$lookup`), keyset pagination instead of `skip`, sharding by `emp_code`, archiving old months, and caching analytics.
