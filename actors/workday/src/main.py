"""Workday Jobs Scraper.

Reads the JSON API behind any public Workday careers site - the same cxs
endpoint the careers page itself calls. You paste careers or job-posting
URLs; the Actor decomposes tenant, shard and site from them, which are the
three parts you otherwise cannot guess.

No proxy, no browser, no API key.

Three traps this Actor handles for you, verified against live tenants:

* The endpoint pages at exactly 20 records. Asking for more returns HTTP 200
  with an empty list - the response for "not hiring". This Actor always pages
  at 20.
* `postedOn` is a localized display string, not a date. This Actor pins
  `Accept-Language: en-US` and derives a real date from the English strings.
* The list response is thin. Full descriptions, real start dates and
  employment type live behind one extra request per job - fetched only when
  you ask for them.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from apify import Actor

from .delta import DeltaTracker
from .signal import summarise

ATS = "workday"

# The endpoint's hard page size. limit=21 returns zero rows with HTTP 200 and
# `total` still filled in, which looks exactly like a company with no
# openings. Documented nowhere; cost a day to find.
PAGE = 20

# Hard ceiling per board so a runaway `total` cannot loop forever.
MAX_OFFSET = 10000

_WD_URL = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com(?:/([^?#\s]*))?", re.I
)
_LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2,4})?$")
_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)
_OLDER = re.compile(r"30\+", re.I)

REMOTE_HINT = re.compile(r"\bremote\b|\bwork from home\b|\banywhere\b", re.I)

MISS_HINT = (
    "This entry is not a Workday careers URL. Workday addresses cannot be "
    "guessed from a company name: the tenant, the wd-number shard and the "
    "site name are all set per company. Paste any link from the company's "
    "careers site - a single job posting URL is enough, all three parts are "
    "in it. It looks like company.wd5.myworkdayjobs.com/SiteName."
)


def parse_target(raw: Any):
    """Return (tenant, shard, site) or None.

    Accepts any myworkdayjobs.com URL (careers home, a search page, a single
    posting, even the cxs API URL itself), a dict with tenant/shard/site, or
    a compact "tenant/wd5/Site" triple.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        t = str(raw.get("tenant") or "").strip().lower()
        sh = str(raw.get("shard") or "").strip().lower()
        si = str(raw.get("site") or "").strip()
        return (t, sh, si) if t and sh and si else None

    text = str(raw).strip()
    m = _WD_URL.search(text)
    if m:
        tenant = m.group(1).lower()
        shard = m.group(2).lower()
        path = [p for p in (m.group(3) or "").split("/") if p]
        # The cxs API URL carries the site one segment after the tenant.
        if len(path) >= 4 and path[0].lower() == "wday" and path[1].lower() == "cxs":
            return (tenant, shard, path[3])
        # Human URLs: the site is the first segment that is not a locale
        # like en-US or fr-CA.
        for seg in path:
            if _LOCALE.match(seg):
                continue
            return (tenant, shard, seg)
        return None

    # Compact "tenant/wd5/SiteName".
    parts = [p for p in text.split("/") if p]
    if len(parts) == 3 and re.fullmatch(r"wd\d+", parts[1], re.I):
        return (parts[0].lower(), parts[1].lower(), parts[2])
    return None


def read_targets(cfg: dict):
    """Clean, deduplicated list of raw entries.

    Guards the bill the same way the sibling Actors do: a bare string is not
    iterated character by character, duplicates are fetched once, junk is
    reported once instead of hitting the network.
    """
    raw = cfg.get("careersUrls")
    if isinstance(raw, (str, bytes)):
        raw = [p for p in re.split(r"[,\n]", str(raw)) if p.strip()]
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    entries, seen = [], set()
    for item in raw:
        if item is None:
            continue
        key = str(item).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(item)
    return entries


def compile_terms(values):
    out = []
    for v in values or []:
        v = str(v).strip()
        if v:
            out.append(re.compile(re.escape(v), re.I))
    return out


def matches_any(patterns, *haystacks) -> bool:
    if not patterns:
        return True
    stack = " ".join(str(h) for h in haystacks if h)
    return any(p.search(stack) for p in patterns)


def parse_posted(posted_on: str):
    """(iso_date_or_None, older_than_30) from the en-US display string.

    Safe only because every request pins Accept-Language: en-US - the same
    posting answers "Posted Today" in English and something else entirely in
    whatever your server's locale is.
    """
    if not posted_on:
        return None, False
    m = _POSTED.search(posted_on)
    if not m:
        return None, bool(_OLDER.search(posted_on))
    word = m.group(1).lower()
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if word == "today":
        return today.isoformat(), False
    if word == "yesterday":
        return (today - timedelta(days=1)).isoformat(), False
    days = int(m.group(2))
    if "+" in posted_on:
        # "Posted 30+ Days Ago" is the only granularity past a month.
        return None, True
    return (today - timedelta(days=days)).isoformat(), False


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def charge(event: str) -> None:
    try:
        await Actor.charge(event_name=event)
    except Exception as exc:  # pragma: no cover
        Actor.log.debug("charge(%s) skipped: %s" % (event, exc))


async def fetch_board(client, tenant, shard, site, search_text):
    """Every posting on one board, paged at the mandatory 20 per request."""
    url = "https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s/jobs" % (
        tenant, shard, tenant, site,
    )
    out, offset, total = [], 0, None
    while True:
        r = await client.post(url, json={
            "appliedFacets": {},
            "limit": PAGE,
            "offset": offset,
            "searchText": search_text or "",
        })
        r.raise_for_status()
        data = r.json()
        batch = data.get("jobPostings") or []
        if total is None:
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        out.extend(batch)
        offset += PAGE
        if not batch or offset >= total or offset >= MAX_OFFSET:
            return out, total


async def fetch_detail(client, tenant, shard, site, external_path):
    """One job's full record - description, real start date, employment type.

    The same URL serves the careers page HTML unless you ask for JSON, so the
    Accept header on the client is what makes this work.
    """
    url = "https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s%s" % (
        tenant, shard, tenant, site, external_path,
    )
    r = await client.get(url)
    r.raise_for_status()
    return (r.json() or {}).get("jobPostingInfo") or {}


def build_row(tenant, shard, site, posting):
    external = str(posting.get("externalPath") or "")
    job_id = external.rsplit("/", 1)[-1] or external or str(posting.get("title"))
    location = str(posting.get("locationsText") or "") or None
    posted_text = str(posting.get("postedOn") or "") or None
    published, older = parse_posted(posted_text or "")
    board = "%s/%s/%s" % (tenant, shard, site)
    human_url = "https://%s.%s.myworkdayjobs.com/%s%s" % (
        tenant, shard, site, external,
    )
    return {
        "companyName": tenant,
        "title": str(posting.get("title") or "") or None,
        "department": None,
        "team": None,
        "employmentType": None,
        "location": location,
        "isRemote": bool(location and REMOTE_HINT.search(location)) or None,
        "workplaceType": None,
        "salary": None,
        "publishedAt": published,
        "postedOnText": posted_text,
        "olderThan30Days": older or None,
        "updatedAt": None,
        "jobUrl": human_url,
        "applyUrl": human_url,
        "jobId": job_id,
        "ats": ATS,
        "boardToken": board,
        "globalId": "%s:%s:%s" % (ATS, board, job_id),
        "recordType": "job",
        "_externalPath": external,
    }


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        entries = read_targets(cfg)
        if not entries:
            raise ValueError(
                "Input 'careersUrls' is empty. Paste any URL from a company's "
                "Workday careers site - a single job posting link is enough."
            )

        search_text = str(cfg.get("searchText") or "").strip()
        title_terms = compile_terms(cfg.get("titleKeywords") or [])
        exclude_terms = compile_terms(cfg.get("excludeKeywords") or [])
        location_terms = compile_terms(cfg.get("locations") or [])
        remote_only = bool(cfg.get("remoteOnly", False))
        posted_days = int(cfg.get("postedWithinDays") or 0)
        want_desc = bool(cfg.get("includeDescription", False))
        max_per_board = int(cfg.get("maxJobsPerBoard") or 0)
        concurrency = max(1, min(int(cfg.get("concurrency") or 3), 10))

        output_mode = str(cfg.get("outputMode", "jobs")).strip().lower()
        if output_mode not in ("jobs", "companies", "both"):
            Actor.log.warning(
                "'outputMode' was %r, which is not one of jobs / companies / "
                "both. Returning jobs." % cfg.get("outputMode")
            )
            output_mode = "jobs"
        want_jobs = output_mode in ("jobs", "both")
        want_summary = output_mode in ("companies", "both")

        delta = DeltaTracker(
            enabled=cfg.get("onlyNewSinceLastRun", False),
            config={
                "ats": ATS,
                "boards": sorted(str(e) for e in entries),
                "searchText": search_text,
                "titleKeywords": cfg.get("titleKeywords") or [],
                "excludeKeywords": cfg.get("excludeKeywords") or [],
                "locations": cfg.get("locations") or [],
                "remoteOnly": remote_only,
                "postedWithinDays": posted_days,
                "maxJobsPerBoard": max_per_board,
            },
        )
        await delta.load()

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_days)
            if posted_days else None
        )

        totals = {"jobs": 0, "failed": 0}
        summaries = []
        semaphore = asyncio.Semaphore(concurrency)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WorkdayJobsScraper/1.0)",
                "Accept": "application/json",
                # Without this the API answers in the server's locale and
                # postedOn stops being parseable. See module docstring.
                "Accept-Language": "en-US",
                "Content-Type": "application/json",
            },
        ) as client:

            async def handle(entry) -> None:
                target = parse_target(entry)
                if not target:
                    totals["failed"] += 1
                    Actor.log.warning("%s: not a Workday URL" % entry)
                    await Actor.push_data({
                        "companyName": str(entry)[:80],
                        "title": "[not found - see hint]",
                        "recordType": "notFound",
                        "error": "could not extract tenant/shard/site",
                        "hint": MISS_HINT,
                        "ats": ATS,
                    })
                    await charge("apify-default-dataset-item")
                    return

                tenant, shard, site = target
                token = "%s/%s/%s" % (tenant, shard, site)
                jobs, total, error = [], 0, None
                async with semaphore:
                    try:
                        postings, total = await fetch_board(
                            client, tenant, shard, site, search_text
                        )
                        jobs = [build_row(tenant, shard, site, p) for p in postings]
                    except httpx.HTTPStatusError as exc:
                        error = "HTTP %d from the cxs endpoint" % (
                            exc.response.status_code
                        )
                    except httpx.HTTPError as exc:
                        error = "network error: %s" % exc

                if error:
                    totals["failed"] += 1
                    Actor.log.warning("%s: %s" % (token, error))
                    await Actor.push_data({
                        "companyName": tenant,
                        "title": "[not found - see hint]",
                        "recordType": "notFound",
                        "error": error,
                        "hint": (
                            "The URL decomposed to tenant=%s shard=%s site=%s "
                            "but the endpoint refused it. Site names are case "
                            "sensitive; re-paste a fresh link from the careers "
                            "page." % (tenant, shard, site)
                        ),
                        "ats": ATS,
                        "boardToken": token,
                    })
                    await charge("apify-default-dataset-item")
                    return

                kept, matched, new_ids = 0, [], set()
                if max_per_board:
                    jobs = sorted(jobs, key=lambda j: str(j.get("jobId") or ""))
                for item in jobs:
                    if max_per_board and kept >= max_per_board:
                        break
                    if not matches_any(title_terms, item["title"]):
                        continue
                    if exclude_terms and matches_any(exclude_terms, item["title"]):
                        continue
                    if not matches_any(location_terms, item["location"]):
                        continue
                    if remote_only and not item["isRemote"]:
                        continue
                    if cutoff:
                        posted = parse_dt(item["publishedAt"])
                        if posted and posted < cutoff:
                            continue
                        # "Posted 30+ Days Ago" carries no date at all. If the
                        # caller asked for a window of 30 days or less, those
                        # postings are certainly outside it.
                        if item.get("olderThan30Days") and posted_days <= 30:
                            continue

                    if want_desc:
                        try:
                            info = await fetch_detail(
                                client, tenant, shard, site,
                                item["_externalPath"],
                            )
                            item["description"] = info.get("jobDescription")
                            item["employmentType"] = info.get("timeType")
                            start = info.get("startDate")
                            if start:
                                item["publishedAt"] = str(start)
                            if info.get("jobReqId"):
                                item["jobId"] = str(info["jobReqId"])
                        except httpx.HTTPError as exc:
                            Actor.log.debug(
                                "%s: detail fetch failed: %s"
                                % (item["jobId"], exc)
                            )

                    item.pop("_externalPath", None)
                    kept += 1
                    matched.append(item)
                    is_new = delta.see(item)
                    if is_new:
                        new_ids.add(item["globalId"])
                    if not (want_jobs and is_new):
                        continue

                    await Actor.push_data(item)
                    await charge("apify-default-dataset-item")
                    totals["jobs"] += 1

                if want_summary:
                    summaries.append(summarise(
                        company=tenant, token=token, ats=ATS, jobs=matched,
                        new_ids=new_ids if delta.enabled else None,
                        closed_count=None,
                    ))

                Actor.log.info(
                    "%s: %d job(s) kept out of %d found (board total %d)"
                    % (token, kept, len(jobs), total)
                )

            await asyncio.gather(*(handle(e) for e in entries))

        closed = delta.closed()
        if want_jobs:
            for row in closed:
                await Actor.push_data(row)
                await charge("apify-default-dataset-item")

        if want_summary:
            per_board = {}
            for row in closed:
                key = str(row.get("boardToken") or "")
                per_board[key] = per_board.get(key, 0) + 1
            for row in summaries:
                if delta.enabled:
                    row["closedCount"] = per_board.get(str(row.get("boardToken")), 0)
                await Actor.push_data(row)
                await charge("apify-default-dataset-item")

        await delta.save()

        msg = "%d job(s) from %d board(s)" % (totals["jobs"], len(entries))
        if delta.enabled and not delta.is_baseline:
            msg += ", %d closed" % len(closed)
        if totals["failed"]:
            msg += ", %d board(s) failed" % totals["failed"]
        await Actor.set_status_message(msg)
