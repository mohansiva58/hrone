# DECISIONS.md

1. **Indexes:** On employees, unique `{emp_code:1}` enforces identity; `{department:1,joined_on:1,emp_code:1}` supports department and join-date filtering; `{joined_on:1}` serves headcount queries without a department filter. On attendance_logs, unique `{emp_code:1,date:1}` enforces one daily record and supports employee/month queries; `{date:-1,emp_code:1}` supports list ordering; `{date:1,emp_code:1}` supports date-correlated trend lookups; `{date:1,late_minutes:1}` narrows leaderboard candidates; `{emp_code:1,date:1,status:1}` supports combined filters.

2. **Punch-in race:** The unique compound index is the authority. Exactly one insert wins; competitors receive `DuplicateKeyError`, mapped to 409. A pre-check alone cannot prevent a race.

3. **Leaderboard cutoff:** `$rank` ranks by total late minutes, so tied employees share rank and the next rank is skipped. Filtering `rank <= limit` happens after ranking; all cutoff ties remain, ordered by `emp_code`.

4. **Headcount:** The summary begins with eligible employees, not attendance logs. Eligibility is `joined_on <= month end`; a lookup and `preserveNullAndEmptyArrays` retain employees with no logs.

5. **Scaling:** For 10M logs, benchmark representative explains and latency first. If dashboard aggregations are slow, build reconciled daily summary collections (with freshness/repair monitoring) and consider partitioning only after measuring shard-key cardinality and query patterns.
