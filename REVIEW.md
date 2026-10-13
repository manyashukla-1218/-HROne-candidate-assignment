# REVIEW: findings in the starter `app/main.py`

| # | Where | What is wrong | How you would notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses `punch_in.replace(...)` in whatever zone the datetime has, not IST; ignores seconds; `int(secs/60) > 10` means 09:40:30 (shift 09:30) is "not late" although it is strictly more than 10:00 after start; no overnight handling | Punch 09:40:01 -> expected 10, got 0 | `late_minutes_for`: aware IST shift start, `delta > 10 min`, floor minutes from shift start |
| 2 | `compute_work_hours` | Python `round()` is banker's/float rounding (R4 wants half-up); uses un-truncated instants | 8h 7m 30s = 8.125 -> returns 8.12, expected 8.13 | `Decimal` micros/3600 quantised `ROUND_HALF_UP`, instants truncated to seconds |
| 3 | `compute_overtime` | No 30-minute minimum (R3); `max(0, int(...))` truncates toward zero; end date taken from `date_str` naively, overnight end (next day) ignored | Punch-out 5 min after shift end returns 5, expected 0 | `shift_bounds` (overnight adds a day) and `overtime_for` (floor, >= 30) |
| 4 | `punch_in` date | `datetime.fromtimestamp(ms/1000)` gives naive server-local time; the date is not the IST date (R1), no overnight rule, no truncation | Server in UTC: punch 00:30 IST gets the previous date | `from_ms` (aware UTC) + `attendance_date` (IST, overnight rule) + `trunc` |
| 5 | `punch_in` unknown employee | `emp` is `None` -> `TypeError` -> 500 | POST an unknown `emp_code` | 404 via `get_employee_or_404` |
| 6 | `punch_in` race | `find_one` then `insert_one` (check-then-insert); two parallel requests both pass | Fire 10 parallel punch-ins: several 201 | Unique `(emp_code, date)` index, catch `DuplicateKeyError` -> 409 |
| 7 | `punch_in` input | `status` is any string; `punched_at` accepts seconds, floats, `0` is treated as "missing" (`if body.punched_at`) | `status="FOO"` or `punched_at=1783312500` is accepted | `Literal` status, strict `EpochMs` (>= 1e11), `is not None` check |
| 8 | `punch_in` response | Leaks `id` (ObjectId string), returns datetimes instead of epoch ms | Response has `id`, `punch_in` is an ISO string | `ser_attendance` returns contract fields only, ms integers |
| 9 | `create_employee` | `find_one` + `insert_one` race, no unique index; `datetime.now()` is naive local time; unvalidated fields (`emp_code` format, email, shift format, `joined_on`, shift_start == shift_end) | Parallel duplicate creates; `joined_on="2026-02-30"` accepted | Unique index + `DuplicateKeyError` -> 409; `now_ms()` in UTC; pydantic patterns and validators |
| 10 | `create_employee` response | Returns `created_at` as a datetime object | JSON shows an ISO string, not epoch ms | `ser_employee` |
| 11 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the filter; no sort; `page` / `page_size` unbounded (0, -1, 100000) | `page=1` skips the first 20 rows | `(page-1)*size`, `count_documents(q)`, sort by `emp_code`, `Query(ge=1, le=100)` |
| 12 | `list_attendance` | Loads the whole collection into memory, sorts in Python by `date` only (no `emp_code` tie-break), slices in Python; no validation of dates/status; leaks `id`; raw datetimes | Slow at 100k records, unstable order | DB-side sort/skip/limit, indexes, `count_documents`, validation, `from > to` -> 422 |
| 13 | `/health` | Always returns ok even if MongoDB is down | Stop MongoDB, `/health` still 200 | Ping MongoDB, 503 on failure |
| 14 | Mongo client | Default client returns naive datetimes; no tz handling anywhere | Naive vs aware comparison errors, wrong epoch conversion | `tz_aware=True`, `aware()` and `to_ms()` helpers |
| 15 | Startup | No indexes at all | `explain` shows COLLSCAN | `ensure_indexes()` at startup, idempotent |
| 16 | Legacy documents | Code would `KeyError` on records without `history` / `half_day` | Seeded legacy record (sample 9) | `.get()` defaults in serialisers; PATCH handles missing `history` |

## Examined and judged fine
* `load_dotenv()` does not override existing environment variables, so injected values win (kept).
* Plain `def` endpoints with synchronous PyMongo are fine: FastAPI runs them in a threadpool and the client is thread-safe.
* Helpers were not reused as-is: items 1 to 3 show they had to be rewritten.
