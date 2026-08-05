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

from .ats import ADAPTERS, DETECTION_ORDER, REMOTE_HINT

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
      {"ats": "lever", "token": "netflix"}
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
            return head.strip().lower(), tail.strip()

    return None, text.lstrip("@").strip("/")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


async def fetch_company(
    client: httpx.AsyncClient, ats: str | None, token: str, want_desc: bool
) -> tuple[list[dict], str | None]:
    """Fetch one company's jobs. Auto-detects the ATS when it is not given."""
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

        targets = [parse_target(entry) for entry in (cfg.get("companies") or [])]
        targets = [(ats, token) for ats, token in targets if token]
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

            async def handle(ats: str | None, token: str) -> None:
                async with semaphore:
                    jobs, error = await fetch_company(client, ats, token, want_desc)

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

                    await Actor.push_data(item)
                    await charge("apify-default-dataset-item")
                    kept += 1
                    totals["jobs"] += 1

                Actor.log.info(f"{token}: {kept} job(s) kept out of {len(jobs)} found")

            await asyncio.gather(*(handle(a, t) for a, t in targets))

        await Actor.set_status_message(
            f"Done. {totals['jobs']} jobs from {totals['companies_ok']} companies "
            f"({totals['companies_failed']} not found)."
        )
