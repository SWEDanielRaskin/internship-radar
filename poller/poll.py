"""Internship Radar poller.

Reads each company's public job board feed (Workday, Greenhouse, Lever, Ashby,
SmartRecruiters), keeps internship/co-op postings with software-leaning titles,
and records which ones are new since the previous run.

Outputs (committed back to the repo by the workflow):
  data/seen.json      every posting ever seen: key -> {first_seen, ...}
  data/new_jobs.json  postings first seen in the last 72 hours (read by the daily digest)
  data/status.json    per-company result of the latest run, for spotting broken feeds

Standard library only.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "companies.json"
DATA = ROOT / "data"
SEEN = DATA / "seen.json"
NEW = DATA / "new_jobs.json"
STATUS = DATA / "status.json"

NEW_WINDOW_SECONDS = 72 * 3600
TIMEOUT = 25
UA = "internship-radar/1.0 (+https://github.com/SWEDanielRaskin/internship-radar)"

INTERN = re.compile(r"\bintern(ship)?s?\b|co-?op\b|\bstudent\b|summer 20\d\d", re.I)
SOFTWARE = re.compile(
    r"software|developer|engineer|engineering|\bdata\b|\bai\b|\bml\b|machine learning|"
    r"cloud|devops|\bweb\b|full[ -]?stack|back[ -]?end|front[ -]?end|platform|"
    r"cyber|security|\bit\b|information technology|digital|automation|analytics|computer",
    re.I,
)
EXCLUDE = re.compile(r"\bph\.?d\b|doctoral|\bmba\b|\bsenior\b|\bstaff\b|\bprincipal\b", re.I)


def http_json(url, payload=None):
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_greenhouse(f):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{f['board']}/jobs")
    for j in d.get("jobs", []):
        yield {
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted": j.get("first_published") or j.get("updated_at"),
        }


def fetch_lever(f):
    d = http_json(f"https://api.lever.co/v0/postings/{f['board']}?mode=json")
    for j in d:
        cat = j.get("categories") or {}
        yield {
            "id": j["id"],
            "title": j.get("text", ""),
            "location": cat.get("location", "") or ", ".join(cat.get("allLocations") or []),
            "url": j.get("hostedUrl", ""),
            "posted": (j.get("createdAt") or 0) // 1000 or None,
        }


def fetch_ashby(f):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{f['board']}")
    for j in d.get("jobs", []):
        loc = j.get("location", "")
        if j.get("isRemote"):
            loc = (loc + " (Remote)").strip()
        yield {
            "id": j.get("id") or j.get("jobUrl"),
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl", ""),
            "posted": j.get("publishedAt"),
        }


def fetch_smartrecruiters(f):
    c = f["company"]
    offset = 0
    while offset < 1000:
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{c}/postings?limit=100&offset={offset}&q=intern")
        rows = d.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                where += " (Remote)"
            yield {
                "id": str(j["id"]),
                "title": j.get("name", ""),
                "location": where,
                "url": f"https://jobs.smartrecruiters.com/{c}/{j['id']}",
                "posted": j.get("releasedDate"),
            }
        offset += 100
        if offset >= d.get("totalFound", 0) or not rows:
            break


def fetch_workday(f):
    host, tenant, site = f["host"], f["tenant"], f["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    seen_paths = set()
    for term in ("intern", "co-op"):
        offset = 0
        while offset < 200:
            d = http_json(url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            rows = d.get("jobPostings", [])
            for j in rows:
                path = j.get("externalPath", "")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                yield {
                    "id": path,
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"https://{host}/en-US/{site}{path}",
                    "posted": j.get("postedOn"),  # e.g. "Posted Today", "Posted 3 Days Ago"
                }
            offset += 20
            if offset >= d.get("total", 0) or not rows:
                break


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def relevant(job):
    t = job["title"]
    return bool(INTERN.search(t)) and bool(SOFTWARE.search(t)) and not EXCLUDE.search(t)


def poll_company(c):
    feed = c["feed"]
    try:
        jobs = [j for j in FETCHERS[feed["type"]](feed) if relevant(j)]
        return c, jobs, None
    except urllib.error.HTTPError as e:
        return c, [], f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - one bad feed must not stop the run
        return c, [], f"{type(e).__name__}: {e}"[:200]


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    companies = json.loads(CONFIG.read_text())
    DATA.mkdir(exist_ok=True)
    seen = load(SEEN, {})
    baseline = not seen  # first run: record everything, flag nothing as new
    now = int(time.time())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(poll_company, companies))

    status = {}
    fresh = 0
    for c, jobs, err in results:
        status[c["name"]] = {"ok": err is None, "error": err, "matches": len(jobs), "checked": now}
        for j in jobs:
            key = f"{c['feed']['type']}:{c['name']}:{j['id']}"
            if key in seen:
                seen[key]["last_seen"] = now
                continue
            seen[key] = {
                "company": c["name"],
                "tier": c["tier"],
                "title": j["title"],
                "location": j["location"],
                "url": j["url"],
                "posted": j["posted"],
                "first_seen": now,
                "last_seen": now,
                "baseline": baseline,
            }
            if not baseline:
                fresh += 1

    new_jobs = sorted(
        (
            {"key": k, **v}
            for k, v in seen.items()
            if not v.get("baseline") and now - v["first_seen"] <= NEW_WINDOW_SECONDS
        ),
        key=lambda x: -x["first_seen"],
    )

    SEEN.write_text(json.dumps(seen, indent=1, sort_keys=True))
    NEW.write_text(json.dumps({"generated": now, "window_hours": 72, "jobs": new_jobs}, indent=1))
    STATUS.write_text(json.dumps(status, indent=1, sort_keys=True))

    ok = sum(1 for s in status.values() if s["ok"])
    print(f"{ok}/{len(status)} feeds OK, {sum(s['matches'] for s in status.values())} matching postings, "
          f"{fresh} new{' (baseline run)' if baseline else ''}")
    for name, s in sorted(status.items()):
        if not s["ok"]:
            print(f"  FAILED {name}: {s['error']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
