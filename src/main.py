"""Career Page Job Monitor.

Give it a list of companies. It finds which applicant-tracking system each one
uses, pulls their live job postings straight from the public board API, and
returns every opening in one normalised schema.

No proxy, no browser, no API key - these endpoints are public.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from apify import Actor

from .ats import ADAPTERS, DETECTION_ORDER, REMOTE_HINT, normalize_token, token_variants
from .delta import DeltaTracker

# --------------------------------------------------------------------------
# input parsing
# --------------------------------------------------------------------------

_BOARD_URL_PATTERNS = [
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([\w.-]+)", re.I)),
    ("greenhouse", re.compile(r"job-boards\.greenhouse\.io/([\w.-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([\w.-]+)", re.I)),
    ("recruitee", re.compile(r"([\w-]+)\.recruitee\.com", re.I)),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([\w.-]+)", re.I)),
]


def parse_target(raw: Any) -> tuple[str | None, str]:
    """Return (ats_or_None, token) for one input entry.

    Accepts:
      "stripe"                     -> (None, "stripe")          auto-detect
      "greenhouse:stripe"          -> ("greenhouse", "stripe")
      "https://jobs.ashbyhq.com/ramp" -> ("ashby", "ramp")
      "stripe.com"                 -> (None, "stripe")           domain accepted
      {"ats": "lever", "token": "shieldai"}
    """
    if raw is None:
        return None, ""
    if isinstance(raw, dict):
        token = str(raw.get("token") or raw.get("company") or raw.get("url") or "").strip()
        ats = (raw.get("ats") or "").strip().lower() or None
        if ats:
            return ats, token
        raw = token

    text = str(raw).strip()
    if not text:
        return None, ""

    for ats, pattern in _BOARD_URL_PATTERNS:
        hit = pattern.search(text)
        if hit:
            return ats, hit.group(1)

    if ":" in text and not text.startswith("http"):
        head, _, tail = text.partition(":")
        if head.strip().lower() in ADAPTERS:
            return head.strip().lower(), normalize_token(tail)

    # Not a known board URL. People paste company domains far more often than
    # board slugs, so pull the company name out of whatever this is.
    return None, normalize_token(text)


def target_variants(raw: Any, token: str) -> list[str]:
    """Fallback spellings to try if `token` finds nothing.

    An explicit board URL is unambiguous, so never second-guess it. Only a
    bare name or domain gets alternative spellings.
    """
    text = str(raw)
    for _, pattern in _BOARD_URL_PATTERNS:
        if pattern.search(text):
            return []
    return [v for v in token_variants(raw) if v and v != token]

# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


async def fetch_company(
    client: httpx.AsyncClient, ats: str | None, token: str, want_desc: bool,
    alt_tokens: list[str] | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one company's jobs. Auto-detects the ATS when it is not given.

    `alt_tokens` are fallback spellings of the same company (shield.ai also
    files its board under "shieldai"). They are only tried after the primary
    token has missed on every platform, so the common case costs nothing.
    """
    order = [ats] if ats else DETECTION_ORDER
    last_error: str | None = None

    for name in order:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            last_error = f"unknown ATS '{name}'"
            continue
        try:
            jobs = await adapter(client, token, want_desc)
        except httpx.HTTPStatusError as exc:
            last_error = f"{name}: HTTP {exc.response.status_code}"
            continue
        except Exception as exc:  # noqa: BLE001 - adapters must never kill the run
            last_error = f"{name}: {type(exc).__name__}"
            continue

        if jobs:
            return jobs, None
        # A valid board with zero openings is a real answer, not a miss.
        if ats:
            return [], None
        last_error = f"{name}: no jobs"

    for alt in (alt_tokens or []):
        if alt == token:
            continue
        jobs, error = await fetch_company(client, ats, alt, want_desc)
        if jobs:
            return jobs, None

    if ats:
        return [], last_error or f"{ats}: no board found for this token"
    # Auto-detection tried every platform. Reporting only the last one it
    # touched would read like a single-platform failure, which misleads.
    return [], (
        f"no job board found on any of {len(DETECTION_ORDER)} supported platforms "
        f"({', '.join(DETECTION_ORDER)})"
    )


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------


def compile_terms(terms: list[str]) -> list[re.Pattern]:
    return [re.compile(re.escape(t.strip()), re.I) for t in terms if str(t).strip()]


def matches_any(patterns: list[re.Pattern], *fields: str | None) -> bool:
    if not patterns:
        return True
    haystack = " ".join(f for f in fields if f)
    return any(p.search(haystack) for p in patterns)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def charge(event: str) -> None:
    try:
        await Actor.charge(event_name=event)
    except Exception as exc:  # pragma: no cover
        Actor.log.debug(f"charge({event}) skipped: {exc}")


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        raw_entries = cfg.get("companies") or []
        targets = []
        for entry in raw_entries:
            ats, token = parse_target(entry)
            if token:
                targets.append((ats, token, target_variants(entry, token)))
        if not targets:
            raise ValueError(
                "Input 'companies' is empty. Add company board tokens, for example "
                "\"stripe\", \"ashby:ramp\", or a full careers URL."
            )

        title_terms = compile_terms(cfg.get("titleKeywords") or [])
        exclude_terms = compile_terms(cfg.get("excludeKeywords") or [])
        location_terms = compile_terms(cfg.get("locations") or [])
        dept_terms = compile_terms(cfg.get("departments") or [])
        remote_only = bool(cfg.get("remoteOnly", False))
        want_desc = bool(cfg.get("includeDescription", False))
        max_per_company = int(cfg.get("maxJobsPerCompany", 0)) or None
        posted_days = int(cfg.get("postedWithinDays", 0)) or None
        concurrency = max(1, min(int(cfg.get("concurrency", 5)), 20))

        # The snapshot is keyed on everything that changes which jobs qualify,
        # so a filter tweak starts a clean baseline instead of reporting the
        # jobs you stopped asking about as newly closed.
        delta = DeltaTracker(
            enabled=cfg.get("onlyNewSinceLastRun", False),
            config={
                "targets": sorted("%s:%s" % (a or "auto", t) for a, t, _ in targets),
                "titleKeywords": cfg.get("titleKeywords") or [],
                "excludeKeywords": cfg.get("excludeKeywords") or [],
                "locations": cfg.get("locations") or [],
                "departments": cfg.get("departments") or [],
                "remoteOnly": remote_only,
                "postedWithinDays": posted_days,
                "maxJobsPerCompany": max_per_company,
            },
        )
        await delta.load()

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_days)
            if posted_days
            else None
        )

        Actor.log.info(f"{len(targets)} company target(s), concurrency {concurrency}")

        semaphore = asyncio.Semaphore(concurrency)
        totals = {"jobs": 0, "companies_ok": 0, "companies_failed": 0}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; CareerPageJobMonitor/1.0)",
                "Accept": "application/json",
            },
        ) as client:

            async def handle(ats: str | None, token: str, alts: list[str]) -> None:
                async with semaphore:
                    jobs, error = await fetch_company(
                        client, ats, token, want_desc, alts
                    )

                if error:
                    totals["companies_failed"] += 1
                    Actor.log.warning(f"{token}: {error}")
                    await Actor.push_data(
                        {
                            "boardToken": token,
                            "ats": ats,
                            "error": error,
                            "hint": (
                                "Check the board token. It is the company slug in their "
                                "careers URL, e.g. boards.greenhouse.io/<token>."
                            ),
                        }
                    )
                    return

                totals["companies_ok"] += 1
                kept = 0
                if max_per_company and delta.enabled:
                    # A cap keeps the first N in whatever order the board's API
                    # happened to return. That order is not guaranteed stable,
                    # so between runs the capped set can shift and the diff
                    # invents jobs that opened and closed. Sorting first makes
                    # the same N jobs qualify every run.
                    jobs = sorted(jobs, key=lambda j: str(j.get("jobId") or ""))
                for item in jobs:
                    if max_per_company and kept >= max_per_company:
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
                    # `see` records the job either way; it returns False only
                    # for a job we already reported on an earlier run.
                    if not delta.see(item):
                        continue

                    await Actor.push_data(item)
                    await charge("apify-default-dataset-item")
                    totals["jobs"] += 1

                Actor.log.info(f"{token}: {kept} job(s) kept out of {len(jobs)} found")

            await asyncio.gather(*(handle(a, t, v) for a, t, v in targets))

        closed = delta.closed()
        for row in closed:
            await Actor.push_data(row)
            await charge("apify-default-dataset-item")
        await delta.save()

        if not delta.enabled:
            summary = (
                f"Done. {totals['jobs']} jobs from {totals['companies_ok']} companies "
                f"({totals['companies_failed']} not found)."
            )
        elif delta.is_baseline:
            summary = (
                f"Baseline recorded: {totals['jobs']} jobs from "
                f"{totals['companies_ok']} companies. The next run returns only changes."
            )
        else:
            summary = (
                f"{totals['jobs']} new, {len(closed)} closed, across "
                f"{totals['companies_ok']} companies."
            )
        await Actor.set_status_message(summary)

