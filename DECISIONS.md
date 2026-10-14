# DECISIONS.md

1. **Indexes:** Employees use unique `{emp_code:1}` for identity, `{department:1,joined_on:1,emp_code:1}` for department scans, `{department:1,emp_code:1}` for filtered sorting, and `{joined_on:1}` for headcount. Attendance uses unique `{emp_code:1,date:1}` for daily identity; `{date:-1,emp_code:1}` for list order; `{date:1,emp_code:1}` for trend lookups; `{date:1,late_minutes:1}` for leaderboard candidates; and `{emp_code:1,date:1,status:1}` for combined filters.

2. **Punch-in race:** The unique compound index is authoritative. One insert wins; duplicate contenders raise `DuplicateKeyError`, mapped to 409. A pre-check cannot close the race.

3. **Leaderboard cutoff:** `$rank` ranks by total late minutes, so ties share rank and skipped ranks follow. Filtering `rank <= limit` happens after ranking; all cutoff ties remain, ordered by `emp_code`.

4. **Headcount:** Department summary starts from eligible employees, not logs. Eligibility is `joined_on <= month end`; lookup with preserved empty arrays keeps employees with no logs.

5. **Scaling:** For 10M logs, benchmark explains and latency first. If aggregations are slow, add reconciled daily summaries with freshness checks; partition only after measuring shard keys and query patterns.
