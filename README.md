# Employee Attendance & Analytics API

![Python](https://img.shields.io/badge/Python-3.11+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-009688) ![MongoDB](https://img.shields.io/badge/MongoDB-6.0+-47A248)

Backend for a small HR attendance system: employees punch in and out, managers correct records, and analytics answer who is late, which department is slipping and what the trend looks like.

## Features
- Employee create and list (unique `emp_code`, pagination)
- Punch-in and punch-out with IST rules: late, overtime, work hours, half day, overnight shifts
- Race-safe: unique indexes make duplicate punch-ins return exactly one 201
- Manual regularization with an append-only audit `history`
- MongoDB aggregation analytics: employee monthly, department summary, late leaderboard (ties), department trend (gap filling and 7-day moving average)
- `/admin/explain/{endpoint}` proves queries use indexes

## Setup
```bash
pip install -r requirements.txt
docker run -d --name mongo-hr -p 27017:27017 mongo:7   # or use Atlas
export MONGO_URI="mongodb://localhost:27017"            # PowerShell: $env:MONGO_URI="..."
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Open `http://localhost:8000/docs` for the interactive API. Indexes are created automatically at startup.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | 200 only if MongoDB answers |
| POST / GET | `/employees` | Create / list employees |
| POST | `/attendance/punch-in` | Create the day's record |
| POST | `/attendance/punch-out` | Close the record |
| GET | `/attendance` | Filtered, paginated list |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-comers ranking |
| GET | `/analytics/departments/{department}/trend` | Daily trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain output |

## Notes
- All code is in `app/main.py`; review findings in `REVIEW.md`, design answers in `DECISIONS.md`.
- Config comes from `MONGO_URI` and `MONGO_DB` (a `.env` is loaded, real env vars win).
- Instants are stored as BSON dates (UTC) and exposed as epoch milliseconds; dates, months and shift times are IST strings.
- Rounding (half-up, 2 and 4 decimals) is applied to aggregation output in Python because MongoDB `$round` is banker's rounding.
- Requires MongoDB 6.0+. Nothing from the contract is missing.
