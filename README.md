# internship-radar

A GitHub Actions poller that watches the public job boards of 46 metro Detroit employers for new software internship and co-op postings, so new roles are caught within hours of going live.

## How it works

- `config/companies.json` lists each company and its job board feed (Workday, Greenhouse, Lever, Ashby, or SmartRecruiters).
- `.github/workflows/poll.yml` runs `poller/poll.py` every 2 hours.
- The poller queries each feed's public JSON endpoint, keeps postings whose titles look like software-related internships or co-ops, and commits the results:
  - `data/seen.json`: every matching posting ever seen, with first-seen time
  - `data/new_jobs.json`: postings first seen in the last 72 hours
  - `data/status.json`: which feeds succeeded or failed on the latest run
- A daily digest job reads `data/new_jobs.json`, scores each posting for fit, adds it to a tracker, and sends a summary.

The first run records a baseline and flags nothing as new; every later run flags only postings that appeared since.

## Adding a company

Add an entry to `config/companies.json`:

```json
{"name": "Example Co", "tier": 2, "city": "Ann Arbor",
 "feed": {"type": "greenhouse", "board": "exampleco"}}
```

Feed shapes:

| type | fields | where to find them |
|---|---|---|
| workday | `host`, `tenant`, `site` | `https://<host>/en-US/<site>` on the careers page; tenant is the first part of the host |
| greenhouse | `board` | `job-boards.greenhouse.io/<board>` |
| lever | `board` | `jobs.lever.co/<board>` |
| ashby | `board` | `jobs.ashbyhq.com/<board>` |
| smartrecruiters | `company` | `jobs.smartrecruiters.com/<company>` |

Run it locally with `python poller/poll.py` (Python 3.10+, no dependencies).
