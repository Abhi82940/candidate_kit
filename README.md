# Employee Attendance & Analytics API (Contract v2.0)

Production-grade implementation of the HROne Employee Attendance & Analytics API built with FastAPI and MongoDB 6.0+ (compatible with MongoDB 7.0 and 8.0+).

## Prerequisites

- **Python**: 3.11 or newer
- **MongoDB**: 6.0 or newer (tested with local MongoDB 8.2)

## Environment Configuration

The application reads configuration from standard environment variables, with fallback to a local `.env` file if present:

- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Target database name (default: `attendance_db`)

Environment variables take precedence over `.env` definitions. Do not commit `.env` or credentials to version control.

### Example environment setup

On Linux/macOS:
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

On Windows PowerShell:
```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

## Installation & Setup

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Linux/macOS:
   source .venv/bin/activate
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Ensure MongoDB is running:**
   Start a local MongoDB instance, a Docker container, or point `MONGO_URI` to a MongoDB Atlas cluster:
   ```bash
   # Example with Docker if using containers:
   docker run -d -p 27017:27017 --name mongo-attendance mongo:7
   ```

4. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

5. **Start the API application:**
   The application must start from the repository root:
   ```bash
   uvicorn app.main:app --port 8000
   ```

6. **Verify readiness probe:**
   ```bash
   curl http://localhost:8000/health
   # Returns: {"status": "ok"} when MongoDB is reachable (HTTP 503 if unavailable)
   ```

7. **Interactive API Documentation:**
   Swagger UI is available at: `http://localhost:8000/docs`

## Implemented Features & Endpoints

All 12 endpoints specified in `openapi.yaml` are implemented in `app/main.py`:

- `GET /health`: Readiness probe returning 200 on successful MongoDB ping, 503 if unavailable.
- `POST /employees`: Create employee with strict validation (`^EMP\d{4,6}$`, shift validation, server-side UTC timestamps, unique index enforcement).
- `GET /employees`: Paginated employee list with department filter and `emp_code` ascending sort.
- `POST /attendance/punch-in`: Records punch-in, computes R1 attendance date (with overnight shift handling), R2 late minutes, and handles concurrent identical punch-ins safely with HTTP 409.
- `POST /attendance/punch-out`: Closes eligible unpunched record, computes R3 overtime, R4 work hours (half-up rounding), and R5 half-day flag using atomic updates.
- `GET /attendance`: High-performance indexed attendance listing with date range, status, and employee filtering.
- `PATCH /attendance/{emp_code}/{date}`: Manual attendance regularization with optimistic concurrency and audit history tracking.
- `GET /analytics/employees/{emp_code}/monthly`: MongoDB aggregation pipeline computing working days (R7), present days (R5-R7), leave days, late count, overtime, and attendance percentage.
- `GET /analytics/departments/summary`: Cross-collection aggregation including employees with zero logs in headcount (R9) and record-level average work hours.
- `GET /analytics/leaderboard/late`: MongoDB `$rank` window function computing standard competition ranking (1, 2, 2, 4) with ties and post-ranking limits.
- `GET /analytics/departments/{department}/trend`: Daily date series with `$densify` gap filling, headcount tracking, and 7-day bounded moving average.
- `GET /admin/explain/{endpoint}`: Returns raw MongoDB `explain` executionStats plans proving index usage (`IXSCAN`).

## Documentation Files

- `REVIEW.md`: Detailed defect analysis of the original starter code and how each issue was resolved.
- `DECISIONS.md`: Architectural decisions regarding database indexes, race conditions, ranking ties, headcount calculation, and 100x scaling strategy.
