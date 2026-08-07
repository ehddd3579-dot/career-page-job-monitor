"""Workable Jobs Scraper.

Reads the official public Workable board API. One Actor, one platform, no
guessing: you give board tokens, it returns every live opening in a flat schema.

No proxy, no browser, no API key.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from apify import Actor

from .ats import ADAPTERS, REMOTE_HINT, normalize_token, token_variants
from .delta import DeltaTracker
from .signal import summarise

ATS = "workable"
_TOKEN_FROM_URL = re.compile(r"apply\.workable\.com/([\w.-]+)", re.I)

# What to say when a board cannot be found. Never "go look up the board
# token": not having to do that is this Actor's whole point, and repeating it
# at the moment of failure is the worst possible time to go back on it. Name
# the platforms this Actor does not read instead, so "nothing found" reads as
# an explanation rather than a broken tool.
MISS_HINT = (
    "No Workable board answered for this entry. The company may be on a platform "
    "this Actor does not read (Workday, Taleo, iCIMS, SuccessFactors, BambooHR, "
    "Personio or an in-house careers page), or the board may be private. "
    "Pasting the full careers URL usually resolves it - the link a job posting "
    "sits on is enough. The token is the company slug in apply.workable.com/<company>."
)


def parse_board(raw: Any) -> str:
    """Accept a bare token, a full careers URL, or a dict."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("token") or raw.get("board") or raw.get("url") or ""
    text = str(raw).strip()
    if not text:
        return ""
    hit = _TOKEN_FROM_URL.search(text)
    if hit:
        return hit.group(1)
    # People paste company domains, not board slugs. stripe.com -> stripe
    return normalize_token(text)


def compile_terms(terms: list) -> list:
    return [re.compile(re.escape(str(t).strip()), re.I) for t in terms if str(t).strip()]


def matches_any(patterns: list, *fields) -> bool:
    if not patterns:
        return True
    haystack = " ".join(f for f in fields if f)
    return any(p.search(haystack) for p in patterns)


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


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        raw_entries = [b for b in (cfg.get("boards") or []) if parse_board(b)]
        boards = [parse_board(b) for b in raw_entries]
        if not boards:
            raise ValueError(
                "Input 'boards' is empty. Add Workable board tokens - the company slug in apply.workable.com/<company>."
            )

        title_terms = compile_terms(cfg.get("titleKeywords") or [])
        exclude_terms = compile_terms(cfg.get("excludeKeywords") or [])
        location_terms = compile_terms(cfg.get("locations") or [])
        dept_terms = compile_terms(cfg.get("departments") or [])
        remote_only = bool(cfg.get("remoteOnly", False))
        want_desc = bool(cfg.get("includeDescription", False))
        max_per_board = int(cfg.get("maxJobsPerBoard", 0)) or None
        posted_days = int(cfg.get("postedWithinDays", 0)) or None
        concurrency = max(1, min(int(cfg.get("concurrency", 5)), 20))
        # "jobs" is one row per opening, "companies" is one row per board,
        # "both" is the job rows followed by the summaries.
        output_mode = str(cfg.get("outputMode", "jobs")).strip().lower()
        if output_mode not in ("jobs", "companies", "both"):
            output_mode = "jobs"
        want_jobs = output_mode in ("jobs", "both")
        want_summary = output_mode in ("companies", "both")

        # Keyed on everything that changes which jobs qualify, so tweaking a
        # filter starts a clean baseline instead of reporting the jobs you
        # stopped asking about as newly closed.
        delta = DeltaTracker(
            enabled=cfg.get("onlyNewSinceLastRun", False),
            config={
                "ats": ATS,
                "boards": sorted(boards),
                "titleKeywords": cfg.get("titleKeywords") or [],
                "excludeKeywords": cfg.get("excludeKeywords") or [],
                "locations": cfg.get("locations") or [],
                "departments": cfg.get("departments") or [],
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

        Actor.log.info("%d Workable board(s), concurrency %d" % (len(boards), concurrency))
        adapter = ADAPTERS[ATS]
        semaphore = asyncio.Semaphore(concurrency)
        totals = {"jobs": 0, "ok": 0, "failed": 0}
        summaries = []

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WorkableJobsScraper/1.0)",
                "Accept": "application/json",
            },
        ) as client:

            async def handle(entry) -> None:
                # A pasted domain may not match the board slug exactly
                # (shield.ai files its board as "shieldai"), so try the
                # sensible spellings before reporting a miss.
                primary = parse_board(entry)
                tries = [primary]
                if not _TOKEN_FROM_URL.search(str(entry)):
                    for guess in token_variants(entry):
                        if guess and guess not in tries:
                            tries.append(guess)
                token, jobs, error = primary, [], None
                async with semaphore:
                    for candidate in tries:
                        try:
                            jobs = await adapter(client, candidate, want_desc)
                            error = None
                        except httpx.HTTPStatusError as exc:
                            jobs, error = [], "HTTP %d" % exc.response.status_code
                        except Exception as exc:  # noqa: BLE001
                            jobs, error = [], type(exc).__name__
                        if jobs:
                            token = candidate
                            break

                # SmartRecruiters answers HTTP 200 with an empty board for an
                # identifier that does not exist - verified live, the response
                # is identical to a real company with nothing open, and no
                # other public endpoint separates them. Without this the Actor
                # could never report a bad identifier at all: every typo would
                # come back as a cheerful "0 jobs found".
                if not jobs and error is None and ATS == "smartrecruiters":
                    error = (
                        "the board came back empty, which is also how "
                        "SmartRecruiters answers an identifier that does not "
                        "exist. Identifiers are case sensitive: Bosch is "
                        "BoschGroup, Ubisoft is Ubisoft2."
                    )

                if error:
                    totals["failed"] += 1
                    Actor.log.warning("%s: %s" % (token, error))
                    await Actor.push_data({
                        "recordType": "notFound",
                        "boardToken": token,
                        "ats": ATS,
                        "error": error,
                        "hint": MISS_HINT,
                    })
                    return

                totals["ok"] += 1
                kept = 0
                matched = []
                new_ids = set()
                if max_per_board and delta.enabled:
                    # A cap keeps the first N in whatever order the board's API
                    # happened to return. That order is not guaranteed stable,
                    # so between runs the capped set can shift and the diff
                    # invents jobs that opened and closed. Sorting first makes
                    # the same N jobs qualify every run.
                    jobs = sorted(jobs, key=lambda j: str(j.get("jobId") or ""))
                for item in jobs:
                    if max_per_board and kept >= max_per_board:
                        break
                    if not matches_any(title_terms, item["title"]):
                        continue
                    if exclude_terms and matches_any(
                        exclude_terms, item["title"], item["department"]
                    ):
                        continue
                    if not matches_any(location_terms, item["location"]):
                        continue
                    if not matches_any(dept_terms, item["department"], item["team"]):
                        continue
                    if remote_only and not (
                        item["isRemote"]
                        or (item["location"] and REMOTE_HINT.search(item["location"]))
                    ):
                        continue
                    if cutoff:
                        posted = parse_dt(item["publishedAt"]) or parse_dt(item["updatedAt"])
                        if posted and posted < cutoff:
                            continue

                    if not want_desc:
                        item.pop("description", None)

                    kept += 1
                    # The summary describes what passed the caller's filters,
                    # so it is built from the same set the job rows come from.
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
                        company=(matched[0].get("companyName") if matched else token),
                        token=token, ats=ATS, jobs=matched,
                        new_ids=new_ids if delta.enabled else None,
                        closed_count=None,
                    ))

                Actor.log.info("%s: %d job(s) kept out of %d found" % (token, kept, len(jobs)))

            await asyncio.gather(*(handle(e) for e in raw_entries))

        closed = delta.closed()
        if want_jobs:
            for row in closed:
                await Actor.push_data(row)
                await charge("apify-default-dataset-item")

        if want_summary:
            # Closed roles are only known once every board has been read.
            per_board = {}
            for row in closed:
                key = str(row.get("boardToken") or "")
                per_board[key] = per_board.get(key, 0) + 1
            for row in summaries:
                if delta.enabled:
                    shut = per_board.get(str(row.get("boardToken") or ""), 0)
                    row["closedRoles"] = shut
                    if row["newRoles"] is not None:
                        row["netChange"] = row["newRoles"] - shut
                await Actor.push_data(row)
                await charge("apify-default-dataset-item")

        await delta.save()

        if not delta.enabled:
            summary = ("Done. %d jobs from %d Workable board(s) (%d not found)."
                       % (totals["jobs"], totals["ok"], totals["failed"]))
        elif delta.is_baseline:
            summary = ("Baseline recorded: %d jobs from %d Workable board(s). "
                       "The next run returns only changes."
                       % (totals["jobs"], totals["ok"]))
        else:
            summary = ("%d new, %d closed, across %d Workable board(s)."
                       % (totals["jobs"], len(closed), totals["ok"]))
        await Actor.set_status_message(summary)



