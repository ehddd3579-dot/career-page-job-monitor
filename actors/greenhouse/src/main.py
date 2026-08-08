"""Greenhouse Jobs Scraper.

Reads the official public Greenhouse board API. One Actor, one platform, no
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

ATS = "greenhouse"
_TOKEN_FROM_URL = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([\w.-]+)", re.I)

# What to say when a board cannot be found. Never "go look up the board
# token": not having to do that is this Actor's whole point, and repeating it
# at the moment of failure is the worst possible time to go back on it. Name
# the platforms this Actor does not read instead, so "nothing found" reads as
# an explanation rather than a broken tool.
MISS_HINT = (
    "No Greenhouse board answered for this entry. The company may be on a platform "
    "this Actor does not read (Workday, Taleo, iCIMS, SuccessFactors, BambooHR, "
    "Personio or an in-house careers page), or the board may be private. "
    "Pasting the full careers URL usually resolves it - the link a job posting "
    "sits on is enough. The token is the company slug in job-boards.greenhouse.io/<company>."
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


def read_boards(cfg: dict) -> list:
    """Turn whatever arrived in `boards` into a clean, deduplicated list.

    Every branch here is something a real caller sent, and every one of them
    used to be charged for:

    * A bare string. `"boards": "stripe"` looks obviously right when you are
      writing JSON by hand against the API. Python iterates it one character
      at a time, so the run fetched six boards named s, t, r, i, p, e and
      billed for six.
    * Duplicates. A pasted list with the same company twice - or once as
      `Stripe` and once as `stripe` - was fetched twice, charged twice, and
      returned every job twice.
    * Junk entries. A stray `null` or a number in the array became a board
      token via `str()` and went out to the network as one.

    None of those are the caller being careless with someone else's money;
    they are the caller being careless with their own, which is worse.
    """
    raw = cfg.get("boards")
    if raw is None:
        raw = []
    if isinstance(raw, (str, bytes)):
        # One string is one entry - or several, if they separated them the way
        # a text box invites. Never a sequence of characters.
        raw = [p for p in re.split(r"[,\n]", str(raw)) if p.strip()]
    elif isinstance(raw, dict):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        raw = [raw]

    entries, tokens, seen, skipped, dupes = [], [], set(), [], 0
    for entry in raw:
        if isinstance(entry, bool) or isinstance(entry, (int, float)):
            # A number is never a board token. Sending it anyway turns a typo
            # into a paid request.
            skipped.append(repr(entry))
            continue
        token = parse_board(entry)
        if not token:
            if entry not in (None, "", [], {}):
                skipped.append(repr(entry))
            continue
        key = token.lower()
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        # The original entry is kept, not the parsed token: a pasted domain
        # carries spellings the token does not, and the fetch loop tries those
        # variants before giving up.
        entries.append(entry)
        tokens.append(token)

    if skipped:
        Actor.log.warning(
            "Ignored %d entry/entries in 'boards' that are not usable tokens: "
            "%s. Nothing was fetched or charged for them."
            % (len(skipped), ", ".join(skipped[:5]))
        )
    if dupes:
        Actor.log.info(
            "Removed %d duplicate compan%s from 'boards'; each board is "
            "fetched and charged once."
            % (dupes, "y" if dupes == 1 else "ies")
        )
    return entries, tokens


def read_int(cfg: dict, key: str, default: int = 0) -> int:
    """A number field that survives a caller typing words into it.

    The Actor input form constrains these, but the API does not, and a
    `ValueError: invalid literal for int()` traceback tells the caller nothing
    about which field they got wrong.
    """
    value = cfg.get(key, default)
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        Actor.log.warning(
            "'%s' is not a number (got %r) - ignoring it and using %d."
            % (key, value, default)
        )
        return default


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

        raw_entries, boards = read_boards(cfg)
        if not boards:
            raise ValueError(
                "Input 'boards' is empty. Add Greenhouse board tokens - the company slug in job-boards.greenhouse.io/<company>."
            )

        title_terms = compile_terms(cfg.get("titleKeywords") or [])
        exclude_terms = compile_terms(cfg.get("excludeKeywords") or [])
        location_terms = compile_terms(cfg.get("locations") or [])
        dept_terms = compile_terms(cfg.get("departments") or [])
        remote_only = bool(cfg.get("remoteOnly", False))
        want_desc = bool(cfg.get("includeDescription", False))
        # 0 means no limit. A negative used to slip through and slice the
        # list to nothing, so the run "succeeded" with zero rows and no
        # explanation - treat it as the no-limit it was meant to be.
        max_per_board = read_int(cfg, "maxJobsPerBoard", 0)
        if max_per_board < 0:
            Actor.log.warning(
                "'maxJobsPerBoard' was %d; a negative limit is read as no "
                "limit rather than as zero jobs." % max_per_board
            )
        max_per_board = max_per_board if max_per_board > 0 else None
        posted_days = read_int(cfg, "postedWithinDays", 0)
        posted_days = posted_days if posted_days > 0 else None
        concurrency = max(1, min(read_int(cfg, "concurrency", 5) or 5, 20))
        # "jobs" is one row per opening, "companies" is one row per board,
        # "both" is the job rows followed by the summaries.
        output_mode = str(cfg.get("outputMode", "jobs")).strip().lower()
        if output_mode not in ("jobs", "companies", "both"):
            # Silently returning jobs to someone who asked for companies looks
            # like the feature is broken rather than like a typo.
            Actor.log.warning(
                "'outputMode' was %r, which is not one of jobs / companies / "
                "both. Returning jobs." % cfg.get("outputMode")
            )
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

        Actor.log.info("%d Greenhouse board(s), concurrency %d" % (len(boards), concurrency))
        adapter = ADAPTERS[ATS]
        semaphore = asyncio.Semaphore(concurrency)
        totals = {"jobs": 0, "ok": 0, "failed": 0}
        summaries = []

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; GreenhouseJobsScraper/1.0)",
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
                    # companyName and title are filled in deliberately. The
                    # dataset view is a table of job columns, and this row used
                    # to carry none of them - only `error` and `hint`, neither
                    # of which the view shows. The result was a row that looked
                    # completely blank: the caller could see that something had
                    # gone wrong but not what, or for which company. Saying it
                    # in the two columns they are already reading costs nothing
                    # and is the difference between an explanation and a
                    # mystery.
                    await Actor.push_data({
                        "recordType": "notFound",
                        "companyName": token,
                        "title": "Not found on Greenhouse - see the hint column",
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
            summary = ("Done. %d jobs from %d Greenhouse board(s) (%d not found)."
                       % (totals["jobs"], totals["ok"], totals["failed"]))
        elif delta.is_baseline:
            summary = ("Baseline recorded: %d jobs from %d Greenhouse board(s). "
                       "The next run returns only changes."
                       % (totals["jobs"], totals["ok"]))
        else:
            summary = ("%d new, %d closed, across %d Greenhouse board(s)."
                       % (totals["jobs"], len(closed), totals["ok"]))
        await Actor.set_status_message(summary)
