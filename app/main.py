"""
Employee Attendance & Analytics API - Production Implementation
Contract Version 2.0 (FastAPI + MongoDB)
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

# Load environment variables (.env fallback, environment takes precedence)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000

DATE_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
logger = logging.getLogger("uvicorn.error")


def parse_strict_date(d_str: str, field_name: str = "date") -> dt_date:
    """Strictly validate and parse YYYY-MM-DD calendar date."""
    if not isinstance(d_str, str) or not DATE_REGEX.match(d_str):
        raise HTTPException(
            status_code=HTTP_422,
            detail=f"Invalid {field_name} format '{d_str}'. Expected YYYY-MM-DD.",
        )
    try:
        return datetime.strptime(d_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=HTTP_422,
            detail=f"Invalid calendar date '{d_str}' for {field_name}.",
        )


def init_indexes(database=db):
    """Create all required indexes idempotently. Raises exception on failure."""
    # employees indexes
    database.employees.create_index([("emp_code", 1)], unique=True)
    database.employees.create_index([("department", 1)])
    database.employees.create_index([("joined_on", 1)])
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    # attendance_logs indexes
    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes idempotently at startup without silently swallowing failures
    try:
        init_indexes(db)
    except Exception as e:
        logger.error(f"Critical error during MongoDB index creation: {e}")
        raise RuntimeError(f"Database initialization failed: unable to create required indexes: {e}") from e
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal, decimals: int) -> float:
    """R8: Round decimal values using ROUND_HALF_UP (not banker's rounding)."""
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime truncated to whole seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Current UTC datetime truncated to whole seconds."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=0)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    For overnight shifts (shift_end <= shift_start), if punch_in time in IST
    is strictly earlier than shift_end, it belongs to the previous calendar day.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_ist.time() < time(end_h, end_m, 0):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if strictly more than 10 minutes (600 seconds) after shift_start.
    When late, floor of elapsed whole minutes since shift_start.
    """
    h, m = map(int, shift_start.split(":"))
    att_d = dt_date.fromisoformat(attendance_date)
    start_dt = datetime.combine(att_d, time(h, m, 0), tzinfo=IST)
    punch_ist = punch_in_dt.astimezone(IST)
    diff_sec = (punch_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(
    punch_out_dt: datetime, shift_end: str, shift_start: str, attendance_date: str
) -> int:
    """
    R3: Overtime in whole minutes (floor) from shift_end to punch_out.
    Counted only when >= 30 minutes; otherwise 0.
    For overnight shifts, shift_end is on the next calendar day.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    att_d = dt_date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        end_d = att_d + timedelta(days=1)
    else:
        end_d = att_d
    end_dt = datetime.combine(end_d, time(end_h, end_m, 0), tzinfo=IST)
    out_ist = punch_out_dt.astimezone(IST)
    diff_sec = (out_ist - end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: Optional[float], status_val: str) -> bool:
    """
    R5: Half day is true when rounded work_hours < 4.50.
    Only applies to presence statuses with punch-out.
    """
    if status_val not in ("PRESENT", "WFH", "ON_DUTY") or work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days_for_month(month: str, joined_on: str) -> int:
    """
    R7: Monday through Friday are working days.
    For employees who joined mid-month, working days start from joined_on.
    """
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = dt_date(y, m, 1)
    month_end = dt_date(y, m, num_days)

    join_date = dt_date.fromisoformat(joined_on)
    if join_date > month_end:
        return 0

    cur = max(month_start, join_date)
    count = 0
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def sanitize_bson(obj: Any) -> Any:
    """Sanitize raw BSON/MongoDB explain output for JSON serialization."""
    if isinstance(obj, dict):
        return {k: sanitize_bson(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_bson(v) for v in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, Decimal):
        return float(obj)
    elif hasattr(obj, "__str__") and type(obj).__name__ in ("ObjectId", "Int64"):
        return int(obj) if type(obj).__name__ == "Int64" else str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Document Serializers
# --------------------------------------------------------------------------- #
def serialize_history(history_list: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not history_list:
        return []
    out = []
    for h in history_list:
        at_val = dt_to_epoch_ms(h.get("at"))
        changes_out = {}
        for field, chg in h.get("changes", {}).items():
            from_val = chg.get("from")
            to_val = chg.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = dt_to_epoch_ms(to_val)
            changes_out[field] = {"from": from_val, "to": to_val}
        out.append({
            "at": at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })
    return out


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #
EpochMillisStrict = Annotated[
    int,
    Field(
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
        description="Epoch milliseconds strictly validated.",
    ),
]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not isinstance(v, str) or not DATE_REGEX.match(v):
            raise ValueError(f"Invalid joined_on format '{v}'. Expected YYYY-MM-DD.")
        try:
            datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"Invalid calendar date '{v}' for joined_on.")
        return v

    @model_validator(mode="after")
    def validate_shifts(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisStrict] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisStrict] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillisStrict] = None
    punch_out: Optional[EpochMillisStrict] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


# --------------------------------------------------------------------------- #
# ENDPOINT 1: GET /health
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        res = client.admin.command("ping")
        if res.get("ok") == 1.0 or res.get("ok") == 1:
            return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unavailable: {str(e)}",
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Database unavailable",
    )


# --------------------------------------------------------------------------- #
# ENDPOINT 2: POST /employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at_dt = now_utc_truncated()
    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="emp_code already exists",
        )
    except PyMongoError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    return serialize_employee_doc(doc)


# --------------------------------------------------------------------------- #
# ENDPOINT 3: GET /employees
# --------------------------------------------------------------------------- #
@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)

    items = [serialize_employee_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# ENDPOINT 4: POST /attendance/punch-in
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = now_utc_truncated()

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )
    except PyMongoError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    return serialize_attendance_doc(doc)


# --------------------------------------------------------------------------- #
# ENDPOINT 5: GET /attendance
# --------------------------------------------------------------------------- #
@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(
        None, alias="status"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_strict_date(date_from, "date_from")
    if date_to:
        parse_strict_date(date_to, "date_to")
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(
                status_code=HTTP_422,
                detail="date_from cannot be greater than date_to",
            )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_filter:
        q["status"] = status_filter
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# ENDPOINT 6: POST /attendance/punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_out_dt = now_utc_truncated()

    # Find the employee's most recent unclosed punch-in record
    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", -1)],
    )

    if not rec:
        # Check if the most recent record has already been punched out or if employee has no records
        rec_any = db.attendance_logs.find_one(
            {"emp_code": body.emp_code},
            sort=[("punch_in", -1)],
        )
        if not rec_any or rec_any.get("punch_in") is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No punch-in record found",
            )
        if rec_any.get("punch_out") is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Record is already punched out",
            )
        rec = rec_any

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Chronology validation: punch-out earlier than or equal to punch-in must return 422
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=HTTP_422,
            detail="punched_at must be strictly after punch_in",
        )

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=HTTP_422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(
        punch_out_dt, emp["shift_end"], emp["shift_start"], rec["date"]
    )
    half_day = is_half_day(work_hours, rec["status"])

    # Atomic conditional update guarantees exactly one winner under concurrency
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# ENDPOINT 7: PATCH /attendance/{emp_code}/{date}
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., description="The record's attendance date, YYYY-MM-DD."),
    body: RegularizeRequest = ...,
):
    att_date = date
    parse_strict_date(att_date, "date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attendance record not found",
        )

    orig_status = rec["status"]
    orig_punch_in = rec.get("punch_in")
    orig_punch_out = rec.get("punch_out")
    orig_work_hours = rec.get("work_hours")
    orig_late_minutes = rec.get("late_minutes", 0)
    orig_overtime_minutes = rec.get("overtime_minutes", 0)
    orig_half_day = rec.get("half_day", False)
    orig_history = rec.get("history")  # May be None for legacy records

    if orig_punch_in and orig_punch_in.tzinfo is None:
        orig_punch_in = orig_punch_in.replace(tzinfo=timezone.utc)
    if orig_punch_out and orig_punch_out.tzinfo is None:
        orig_punch_out = orig_punch_out.replace(tzinfo=timezone.utc)

    new_status = body.status if body.status is not None else orig_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=HTTP_422,
                detail="punch_in and punch_out must not be supplied for ABSENT or LEAVE status",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else orig_punch_in
        )
        new_punch_out = (
            epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else orig_punch_out
        )

    # Presence status requires punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(
            status_code=HTTP_422,
            detail="Presence status requires punch_in",
        )

    # punch_in must remain associated with the record's attendance date per R1
    if new_punch_in is not None:
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != att_date:
            raise HTTPException(
                status_code=HTTP_422,
                detail="punch_in does not belong to the record's attendance date per R1",
            )

    # punch_out validation
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(
                status_code=HTTP_422,
                detail="punch_out requires a valid punch_in",
            )
        if new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=HTTP_422,
                detail="punch_out must be strictly after punch_in",
            )
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(
                status_code=HTTP_422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], att_date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, emp["shift_end"], emp["shift_start"], att_date
            )
            new_half_day = is_half_day(new_work_hours, new_status)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect changes
    changes: Dict[str, Dict[str, Any]] = {}
    if new_status != orig_status:
        changes["status"] = {"from": orig_status, "to": new_status}
    if new_punch_in != orig_punch_in:
        changes["punch_in"] = {"from": orig_punch_in, "to": new_punch_in}
    if new_punch_out != orig_punch_out:
        changes["punch_out"] = {"from": orig_punch_out, "to": new_punch_out}
    if new_work_hours != orig_work_hours:
        changes["work_hours"] = {"from": orig_work_hours, "to": new_work_hours}
    if new_late_minutes != orig_late_minutes:
        changes["late_minutes"] = {"from": orig_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != orig_overtime_minutes:
        changes["overtime_minutes"] = {"from": orig_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != orig_half_day:
        changes["half_day"] = {"from": orig_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=HTTP_422,
            detail="No fields were changed (no-op regularizations are rejected)",
        )

    correction_now = now_utc_truncated()
    history_entry = {
        "at": correction_now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: safely handles legacy records where history is missing/empty
    if orig_history is None or len(orig_history) == 0:
        query_filter = {
            "_id": rec["_id"],
            "$or": [
                {"history": {"$exists": False}},
                {"history": None},
                {"history": []},
            ],
        }
    else:
        query_filter = {
            "_id": rec["_id"],
            "history": orig_history,
        }

    updated = db.attendance_logs.find_one_and_update(
        query_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict detected; please retry",
        )

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# ENDPOINT 8: GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    working_days = count_working_days_for_month(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(pct, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# ENDPOINT 9: GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.dayOfWeek", [2, 3, 4, 5, 6]]},
                                            {
                                                "$in": [
                                                    "$$l.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": 1,
                "emp_late_count": 1,
                "emp_total_late_minutes": 1,
                "emp_leave_count": 1,
                "emp_on_duty_count": 1,
                "emp_work_hours_sum": {"$sum": "$work_hours_list.work_hours"},
                "emp_work_hours_count": {"$size": "$work_hours_list"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "dept_work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "dept_work_hours_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = int(r["headcount"])
        if headcount == 0:
            continue
        tot_work_hours = float(r.get("dept_work_hours_sum", 0.0))
        tot_work_count = int(r.get("dept_work_hours_count", 0))
        avg_hours = (
            round_half_up(tot_work_hours / tot_work_count, 2) if tot_work_count > 0 else None
        )

        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": avg_hours,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# ENDPOINT 10: GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
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
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    formatted_items = [
        {
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        }
        for doc in items
    ]

    return {"month": month, "items": formatted_items}


# --------------------------------------------------------------------------- #
# ENDPOINT 11: GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
def build_department_trend_pipeline(
    dept_emp_codes: List[str],
    dept_joined_ons: List[str],
    from_date: str,
    to_date: str,
) -> List[Dict[str, Any]]:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_plus_1_dt = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(
        tzinfo=timezone.utc
    )

    return [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"emp_code": dept_emp_codes[0] if dept_emp_codes else {"$exists": True}}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "date_str": {"$literal": from_date}, "is_seed": {"$literal": True}}},
                ],
            }
        },
        {
            "$group": {
                "_id": "$date_str",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_plus_1_dt],
                },
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt", "timezone": "UTC"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$dayOfWeek": {"date": "$date_dt", "timezone": "UTC"}},
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": dept_joined_ons,
                            "as": "joined",
                            "cond": {"$lte": ["$$joined", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    d_from = parse_strict_date(from_date, "from")
    d_to = parse_strict_date(to_date, "to")

    if d_to < d_from:
        raise HTTPException(
            status_code=HTTP_422,
            detail="to date cannot be earlier than from date",
        )

    diff_days = (d_to - d_from).days + 1
    if diff_days > 92:
        raise HTTPException(
            status_code=HTTP_422,
            detail="Date range cannot exceed 92 days",
        )

    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not dept_employees:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Department not found or has no employees",
        )

    dept_emp_codes = [e["emp_code"] for e in dept_employees]
    dept_joined_ons = [e["joined_on"] for e in dept_employees]

    pipeline = build_department_trend_pipeline(
        dept_emp_codes, dept_joined_ons, from_date, to_date
    )
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for row in raw_items:
        att_rate = row.get("attendance_rate")
        mov_avg = row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(att_rate, 4) if att_rate is not None else None,
            "moving_avg_7d": round_half_up(mov_avg, 4) if mov_avg is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# ENDPOINT 12: GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(
        None, alias="status"
    ),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if endpoint == "attendance_list":
            if date_from:
                parse_strict_date(date_from, "date_from")
            if date_to:
                parse_strict_date(date_to, "date_to")
            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="date_from cannot be greater than date_to",
                )
            q: Dict[str, Any] = {}
            if emp_code:
                q["emp_code"] = emp_code
            if status_filter:
                q["status"] = status_filter
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to

            skip = (page - 1) * page_size
            sort_spec = {"date": -1, "emp_code": 1}
            cmd = {
                "find": "attendance_logs",
                "filter": q,
                "sort": sort_spec,
                "skip": skip,
                "limit": page_size,
            }
            explain_res = db.command("explain", cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": sanitize_bson(explain_res),
            }

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="emp_code and month are required",
                )
            pipeline = build_employee_monthly_pipeline(emp_code, month)
            cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_res = db.command("explain", cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": sanitize_bson(explain_res),
            }

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="month is required",
                )
            pipeline = build_department_summary_pipeline(month, department)
            cmd = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_res = db.command("explain", cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": sanitize_bson(explain_res),
            }

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="month is required",
                )
            pipeline = build_late_leaderboard_pipeline(month, limit, department)
            cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_res = db.command("explain", cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": sanitize_bson(explain_res),
            }

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="department, from, and to are required",
                )
            d_from = parse_strict_date(from_date, "from")
            d_to = parse_strict_date(to_date, "to")
            if d_to < d_from:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="to date cannot be earlier than from date",
                )
            diff_days = (d_to - d_from).days + 1
            if diff_days > 92:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail="Date range cannot exceed 92 days",
                )

            dept_employees = list(
                db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1})
            )
            dept_emp_codes = [e["emp_code"] for e in dept_employees]
            dept_joined_ons = [e["joined_on"] for e in dept_employees]

            pipeline = build_department_trend_pipeline(
                dept_emp_codes, dept_joined_ons, from_date, to_date
            )
            cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            explain_res = db.command("explain", cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": sanitize_bson(explain_res),
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to explain query: {str(e)}",
        )
