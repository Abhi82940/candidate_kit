# DECISIONS.md

1. **Indexes.**
   On `employees`, a unique index on `emp_code` enforces employee uniqueness and point lookups; `department` supports filtering; `joined_on` and `(department, joined_on)` ensure indexed filtering for department summaries. On `attendance_logs`, a unique compound index on `(emp_code, date)` guarantees at most one record per employee-day and serializes punch-ins. An index on `(emp_code, punch_in)` provides instant lookup for unclosed shifts during punch-out. The compound index `(date, emp_code)` fulfills listing queries with index-backed sorting, avoiding memory sort limits, while `(date, late_minutes)` optimizes the leaderboard pipeline. An index on `status` alone was considered and rejected due to low cardinality and poor selectivity.

2. **Punch-in race.**
   Both requests fetch employee details and calculate the IST attendance date. Both then attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` acts as the serialization barrier: exactly one request secures the index slot and commits with HTTP 201; the second immediately raises PyMongo's `DuplicateKeyError`, which our handler translates into HTTP 409.

3. **Ties.**
   The late leaderboard uses MongoDB's `$rank` window operator for standard competition ranking (1, 2, 2, 4). Filtering is applied after ranking (`rank <= limit`). If two employees tie at the cutoff, both share the same rank and are returned, even if the total row count exceeds `limit`. Tied rows are ordered deterministically by `emp_code` ascending.

4. **Headcount.**
   The aggregation starts from the `employees` collection matching `joined_on <= month_end` and groups by department, establishing authoritative headcount first. It joins `attendance_logs` using `$lookup`. Employees with zero logs yield an empty array but contribute to department headcount and zero to present days.

5. **One thing you would change for 100x the data.**
   At 100x data (10 million logs), I would replace on-demand aggregations with asynchronous incremental rollups maintained via MongoDB Change Streams into pre-aggregated daily summaries, shard `attendance_logs` on `{emp_code: "hashed"}`, and replace pipeline gap filling with a pre-populated date dimension collection.
