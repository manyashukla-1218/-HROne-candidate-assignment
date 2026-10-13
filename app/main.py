"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded, real environment variables win)

Conventions
* Instants: BSON datetimes (UTC, tz-aware client) <-> epoch milliseconds at the API boundary.
* Calendar values (date, month, HH:MM) are plain strings, IST.
* Derived fields are computed here (R2-R5), stored, and analytics read the stored values.
"""

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # never overrides variables that are already set in the environment

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # PyMongo returns aware (UTC) datetimes instead of naive ones
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        ([("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        (
            [("department", ASCENDING), ("emp_code", ASCENDING)],
            {"name": "dept_emp_code"},
        ),
        (
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            {"name": "dept_joined_on"},
        ),
        ([("joined_on", ASCENDING)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        (
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            {"unique": True, "name": "emp_date_unique"},
        ),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        (
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
            {"name": "status_date_emp"},
        ),
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ],
}


def ensure_indexes() -> None:
    for attempt in range(3):
        try:
            client.admin.command("ping")
            break
        except PyMongoError as exc:
            log.warning("MongoDB not reachable yet (%s), attempt %d", exc, attempt + 1)
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            try:
                db[coll].create_index(keys, **opts)  # idempotent
            except PyMongoError as exc:
                log.error("index %s.%s failed: %s", coll, opts.get("name"), exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    dt = aware(dt)
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def trunc(dt: datetime) -> datetime:
    """R1: drop sub-second part."""
    return aware(dt).replace(microsecond=0)


def now_s() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_ms() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def parse_date(s: Any, name: str = "date") -> Date:
    if not isinstance(s, str) or not DATE_RE.match(s):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_bounds(month: str) -> tuple[Date, Date]:
    y, m = int(month[:4]), int(month[5:7])
    return Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])


def hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm(shift_end) <= hm(shift_start)


def shift_bounds(
    d: Date, shift_start: str, shift_end: str
) -> tuple[datetime, datetime]:
    """Aware IST start/end of the shift that belongs to attendance date d (R1)."""
    sh, sm = hm(shift_start)
    eh, em = hm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST date of punch_in; overnight shift + punch before shift_end -> previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = hm(shift_end)
        if ist.hour * 3600 + ist.minute * 60 + ist.second < eh * 3600 + em * 60:
            d -= timedelta(days=1)
    return d


def rnd(x: Any, places: int) -> float:
    """Half-up rounding (R4/R8), immune to float noise and banker's rounding."""
    d = x if isinstance(x, Decimal) else Decimal(str(round(float(x), 10)))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def late_minutes_for(punch_in: datetime, start: datetime) -> int:
    """R2: strictly more than 10:00 after shift start; minutes counted from shift start."""
    delta = punch_in - start
    return delta // timedelta(minutes=1) if delta > timedelta(minutes=10) else 0


def overtime_for(punch_out: datetime, end: datetime) -> int:
    """R3: floor minutes after shift end, only if >= 30."""
    m = (punch_out - end) // timedelta(minutes=1)
    return m if m >= 30 else 0


def work_hours_for(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded half-up to 2 decimals."""
    micros = (punch_out - punch_in) // timedelta(microseconds=1)
    hours = Decimal(micros) / Decimal(3600 * 10**6)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    d: Date,
) -> dict:
    zero = {
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if status not in PRESENCE or punch_in is None:
        return zero
    start, end = shift_bounds(d, shift_start, shift_end)
    out = dict(zero, late_minutes=late_minutes_for(punch_in, start))
    if punch_out is not None:
        wh = work_hours_for(punch_in, punch_out)
        out.update(
            work_hours=wh,
            overtime_minutes=overtime_for(punch_out, end),
            half_day=wh < 4.5,
        )
    return out


# --------------------------------------------------------------------------- #
# Serialisation (BSON -> API)
# --------------------------------------------------------------------------- #
def ser_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def ser_history(h: dict) -> dict:
    changes = {}
    for k, v in (h.get("changes") or {}).items():
        if k in ("punch_in", "punch_out"):
            changes[k] = {"from": to_ms(v.get("from")), "to": to_ms(v.get("to"))}
        else:
            changes[k] = {"from": v.get("from"), "to": v.get("to")}
    return {
        "at": to_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def ser_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [ser_history(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ms()
    try:
        db.employees.insert_one(doc)  # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [ser_employee(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ss, se = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    ts = trunc(from_ms(body.punched_at)) if body.punched_at is not None else now_s()
    d = attendance_date(ts, ss, se)
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, ss, se, d),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index = race guard
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ss, se = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    po = trunc(from_ms(body.punched_at)) if body.punched_at is not None else now_s()
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": po}},
        sort=[("punch_in", DESCENDING)],
    )
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pi = aware(doc["punch_in"])
    if po <= pi:
        raise HTTPException(422, "punched_at must be after punch_in")
    if po - pi > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
    d = Date.fromisoformat(doc["date"])
    derived = derive(doc["status"], pi, po, ss, se, d)
    res = db.attendance_logs.update_one(
        {
            "_id": doc["_id"],
            "punch_out": None,
        },  # only one concurrent punch-out can match
        {
            "$set": {
                "punch_out": po,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    doc.update(
        punch_out=po,
        work_hours=derived["work_hours"],
        overtime_minutes=derived["overtime_minutes"],
        half_day=derived["half_day"],
    )
    return ser_attendance(doc)


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    df = parse_date(date_from, "date_from") if date_from is not None else None
    dt_ = parse_date(date_to, "date_to") if date_to is not None else None
    if df and dt_ and df > dt_:
        raise HTTPException(422, "date_from must not be after date_to")
    if df or dt_:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt_:
            q["date"]["$lte"] = dt_.isoformat()
    if status is not None:
        q["status"] = status
    return q


ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = (
        db.attendance_logs.count_documents(q)
        if q
        else db.attendance_logs.estimated_document_count()
    )
    cur = (
        db.attendance_logs.find(q)
        .sort(ATT_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [ser_attendance(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_date(date, "date")
    date_str = d.isoformat()
    emp = get_employee_or_404(emp_code)
    ss, se = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    sent = body.model_fields_set

    for _ in range(
        5
    ):  # optimistic concurrency: retry if somebody changed the record under us
        doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
        if not doc:
            raise HTTPException(404, "no attendance record for that date")
        cur_status = doc["status"]
        cur_pi, cur_po = aware(doc.get("punch_in")), aware(doc.get("punch_out"))
        new_status = body.status or cur_status

        if new_status in ("ABSENT", "LEAVE"):
            if "punch_in" in sent or "punch_out" in sent:
                raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
            new_pi = new_po = None
        else:
            new_pi = trunc(from_ms(body.punch_in)) if "punch_in" in sent else cur_pi
            new_po = trunc(from_ms(body.punch_out)) if "punch_out" in sent else cur_po
            if new_pi is None:
                raise HTTPException(422, "a presence status requires punch_in")
            if "punch_in" in sent and attendance_date(new_pi, ss, se) != d:
                raise HTTPException(
                    422, "punch_in must stay on the record's attendance date"
                )
            if new_po is not None:
                if new_po <= new_pi:
                    raise HTTPException(422, "punch_out must be after punch_in")
                if new_po - new_pi > timedelta(hours=24):
                    raise HTTPException(
                        422, "punch_out must be within 24 hours of punch_in"
                    )

        new = {
            "status": new_status,
            "punch_in": new_pi,
            "punch_out": new_po,
            **derive(new_status, new_pi, new_po, ss, se, d),
        }
        old = {
            "status": cur_status,
            "punch_in": cur_pi,
            "punch_out": cur_po,
            "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes") or 0,
            "overtime_minutes": doc.get("overtime_minutes") or 0,
            "half_day": bool(doc.get("half_day")),
        }
        changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
        if not changes:
            raise HTTPException(422, "request changes nothing")

        entry = {
            "at": now_ms(),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        flt: dict = {
            "_id": doc["_id"],
            "status": cur_status,
            "punch_in": doc.get("punch_in"),
            "punch_out": doc.get("punch_out"),
        }
        hist = doc.get("history")
        if isinstance(hist, list):
            flt["history"] = {"$size": len(hist)}
            upd = {"$set": new, "$push": {"history": entry}}
        else:  # legacy record without history
            flt["history"] = None
            upd = {"$set": {**new, "history": [entry]}}
        if db.attendance_logs.update_one(flt, upd).matched_count == 1:
            doc.update(new)
            doc["history"] = (hist if isinstance(hist, list) else []) + [entry]
            return ser_attendance(doc)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def _weight(status_f: str, half_f: str) -> dict:
    """1 for a present record, 0.5 for a present half day, 0 otherwise (R5, R6)."""
    return {
        "$cond": [
            {"$in": [{"$ifNull": [status_f, ""]}, PRESENCE]},
            {"$cond": [{"$eq": [{"$ifNull": [half_f, False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def _weekday_weight(status_f: str, half_f: str) -> dict:
    """present weight, but only on Mon-Fri ($dow: 1=Sunday ... 7=Saturday)."""
    return {
        "$cond": [
            {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            _weight(status_f, half_f),
            0,
        ]
    }


def _dow(date_f: str) -> dict:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": date_f}}}


def _late(field: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [field, 0]}, 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$set": {"dow": _dow("$date")}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _weekday_weight("$status", "$half_day")},
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {"$sum": _late("$late_minutes")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Runs on employees so people with zero logs still count (R9)."""
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    L = "$logs."
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$project": {"_id": 0, "emp_code": 1, "department": 1, "logs": 1}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"dow": _dow("$logs.date")}},
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": _weekday_weight(L + "status", L + "half_day")},
                "wh_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            {"$ifNull": [L + "status", ""]},
                                            PRESENCE,
                                        ]
                                    },
                                    {"$isNumber": L + "work_hours"},
                                ]
                            },
                            L + "work_hours",
                            0,
                        ]
                    }
                },
                "wh_cnt": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            {"$ifNull": [L + "status", ""]},
                                            PRESENCE,
                                        ]
                                    },
                                    {"$isNumber": L + "work_hours"},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": _late(L + "late_minutes")},
                "total_late_minutes": {"$sum": {"$ifNull": [L + "late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": [L + "status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": [L + "status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$set": {"headcount": {"$size": "$emps"}}},
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "wh_sum": 1,
                "wh_cnt": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def leaderboard_pipeline(
    start: str, end: str, limit: int, department: Optional[str]
) -> list:
    pipe: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})
    pipe += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipe


def trend_pipeline(
    department: str, codes: list, joins: list, d_from: Date, d_to: Date
) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(
        days=1
    )  # upper bound exclusive
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": f, "$lte": t}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": _weight("$status", "$half_day")},
                "late_count": {"$sum": _late("$late_minutes")},
            }
        },
        # seed row so $densify always has a document, even when the range has no logs at all
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$replaceWith": {
                            "_id": {"$literal": f},
                            "present_count": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "d",
                "range": {"step": 1, "unit": "day", "bounds": [lo, hi]},
            }
        },
        {"$sort": {"d": 1}},
        {
            "$set": {
                "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "dow": {"$dayOfWeek": "$d"},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {"$literal": joins},
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$ds"]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$ds",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def count_working_days(first: Date, last: Date, joined_on: str) -> int:
    start = max(first, Date.fromisoformat(joined_on))
    n, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def trend_inputs(department: str, d_from: Date, d_to: Date) -> tuple[list, list]:
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(
        db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}
        )
    )
    if not emps:
        raise HTTPException(404, "department not found")
    codes = [e["emp_code"] for e in emps]
    joins = sorted(
        e["joined_on"]
        for e in emps
        if e.get("joined_on") and e["joined_on"] <= d_to.isoformat()
    )
    return codes, joins


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    wd = count_working_days(first, last, emp["joined_on"])
    rows = list(
        db.attendance_logs.aggregate(
            monthly_pipeline(emp_code, first.isoformat(), last.isoformat())
        )
    )
    r = rows[0] if rows else {}
    present = Decimal(str(r.get("present_days", 0)))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": float(present),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": rnd(present / Decimal(wd) * 100, 2) if wd > 0 else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None
):
    first, last = month_bounds(month)
    rows = db.employees.aggregate(
        summary_pipeline(first.isoformat(), last.isoformat(), department)
    )
    items = []
    for r in rows:
        cnt = int(r["wh_cnt"])
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": (
                    rnd(Decimal(str(round(float(r["wh_sum"]), 8))) / Decimal(cnt), 2)
                    if cnt
                    else None
                ),
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(
        leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
    )
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    codes, joins = trend_inputs(department, d_from, d_to)
    rows = db.attendance_logs.aggregate(
        trend_pipeline(department, codes, joins, d_from, d_to)
    )
    items = []
    for r in rows:
        rate, mov = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": None if rate is None else rnd(rate, 4),
                "moving_avg_7d": None if mov is None else rnd(mov, 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: explain
# --------------------------------------------------------------------------- #
def _need(value: Any, name: str) -> Any:
    if value is None:
        raise HTTPException(
            422, f"query parameter '{name}' is required for this endpoint"
        )
    return value


def _check_month(month: Optional[str]) -> str:
    _need(month, "month")
    if not re.match(MONTH_RE, month):
        raise HTTPException(422, "month must be YYYY-MM")
    return month


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
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
        coll = "attendance_logs"
        inner: dict = {
            "find": coll,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _need(emp_code, "emp_code")
            first, last = month_bounds(_check_month(month))
            coll, pipe = "attendance_logs", monthly_pipeline(
                emp_code, first.isoformat(), last.isoformat()
            )
        elif endpoint == "department_summary":
            first, last = month_bounds(_check_month(month))
            coll, pipe = "employees", summary_pipeline(
                first.isoformat(), last.isoformat(), department
            )
        elif endpoint == "late_leaderboard":
            first, last = month_bounds(_check_month(month))
            coll, pipe = "attendance_logs", leaderboard_pipeline(
                first.isoformat(), last.isoformat(), limit, department
            )
        else:  # department_trend
            _need(department, "department")
            d_from, d_to = parse_date(_need(from_, "from"), "from"), parse_date(
                _need(to, "to"), "to"
            )
            codes, joins = trend_inputs(department, d_from, d_to)
            coll, pipe = "attendance_logs", trend_pipeline(
                department, codes, joins, d_from, d_to
            )
        inner = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    try:
        result = db.command({"explain": inner, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc}")
    safe = json.loads(
        json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)
    )
    return {"endpoint": endpoint, "collection": coll, "explain": safe}
