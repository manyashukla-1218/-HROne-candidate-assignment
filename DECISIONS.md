# DECISIONS

**1. Indexes I created.**
On `employees` I made a unique index on `emp_code`, because it is the identity and duplicates must fail. I also added `(department, emp_code)`, `(department, joined_on)` and `joined_on`, which the list, headcount and summary queries use.
On `attendance_logs` I made a unique `(emp_code, date)` index. It stops double records and also speeds up the monthly queries. `(date desc, emp_code)` serves the list sort and month scans. `(status, date desc, emp_code)` covers status filters, and `(emp_code, punch_in desc)` helps punch-out find the latest record.

**2. Two punch-ins at the same instant.**
Both requests work out the same date and both try to insert. I deliberately did not check first and then insert, because both checks could pass. The unique `(emp_code, date)` index lets only one insert win. The other gets a DuplicateKeyError and I turn that into a 409.

**3. A tie at the leaderboard cutoff.**
I use `$rank`, so ties share a rank and the next one is skipped (1, 2, 2, 4). The limit is applied after ranking, on `rank <= limit`. So with limit 2 and ranks 1, 2, 2, 4, I return three rows, because both tied people deserve their place.

**4. Employees with no logs.**
The department summary starts from `employees`, not from logs. I take everyone with `joined_on` on or before the month end, then `$lookup` their logs and keep empty ones with `preserveNullAndEmptyArrays`. Headcount counts distinct `emp_code`s, so people with no logs still count and people who join later do not.

**5. With 100 times more data.**
I would store monthly roll-ups instead of scanning raw logs, and copy the department onto each log to avoid the `$lookup`. I would switch from `skip` to keyset pagination, shard by `emp_code`, archive old months, and cache the analytics results.
