# DECISIONS.md

1. **Indexes created and why:**
   On `employees`, unique `(emp_code: 1)` enforces identity; compound `(department: 1, joined_on: 1, emp_code: 1)` supports department and headcount queries. On `attendance_logs`, unique `(emp_code: 1, date: 1)` guarantees one daily record; `(date: -1, emp_code: 1)` supports pagination; `(date: 1, late_minutes: 1)` supports leaderboard queries.

2. **Punch-in race:**
   The unique `(emp_code, date)` index resolves concurrent punch-ins atomically. The first insert returns 201; a duplicate-key error from the competing write becomes HTTP 409 Conflict.

3. **Leaderboard ties at cutoff:**
   `$setWindowFields` with `$rank` gives tied employees the same rank. Every employee whose rank is within the cutoff is returned, ordered by `emp_code`.

4. **Headcount & zero-log employees:**
   Department summary starts with employees joined by period end, then `$lookup`s attendance and preserves empty results with `$unwind(preserveNullAndEmptyArrays=True)`. Eligible employees without logs still count toward headcount.

5. **100× Data scaling:**
   At 10M records, runtime aggregations may become expensive. I would benchmark first, then consider summary collections maintained by Change Streams with documented lag and reconciliation. Department is not an attendance-log field, so it is not a valid shard-key proposal. Any shard key must reflect measured access patterns and account for unique `(emp_code, date)` index constraints.
