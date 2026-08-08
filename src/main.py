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
from .signal import summarise

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


SR_AMBIGUOUS = (
    "smartrecruiters: the board came back empty. SmartRecruiters answers an "
    "identifier that does not exist exactly the same way it answers a company "
    "with nothing open, so this is more likely a spelling or capitalisation "
    "problem than a company that has stopped hiring. Identifiers are case "
    "sensitive: Bosch is BoschGroup, Ubisoft is Ubisoft2."
)


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
        # A valid board with zero openings is a real answer, not a miss -
        # everywhere except SmartRecruiters.
        #
        # Verified against the live API: `acme-nope-xyz-123` returns
        # HTTP 200 with `totalFound: 0`, byte for byte what a real company
        # with nothing open returns. There is no companies endpoint that
        # separates the two - it 404s for real identifiers as well. So the
        # platform genuinely cannot tell us, and answering "no openings"
        # would be a confident wrong answer to someone who simply mistyped.
        if ats:
            if name == "smartrecruiters":
                return [], SR_AMBIGUOUS
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


# Big platforms this Actor deliberately does not read. Naming them turns
# "nothing found" into an explanation: NVIDIA is not missing from the internet,
# it is on Workday. Without this the honest answer reads like a broken tool.
UNSUPPORTED = "Workday, Taleo, iCIMS, SuccessFactors, BambooHR, Personio or an in-house careers page"


def miss_hint(raw: Any, ats: str | None) -> str:
    """What to tell someone whose company came back empty.

    Never "go look up the board token". Not having to do that is the whole
    point of this Actor, and saying it at the moment of failure is the worst
    possible time to go back on it.
    """
    if ats == "smartrecruiters":
        return ("SmartRecruiters identifiers are case sensitive and often are not the "
                "plain company name - Bosch is BoschGroup, Ubisoft is Ubisoft2. Copy the "
                "exact name from careers.smartrecruiters.com/<Company>.")
    if ats:
        return (f"No {ats} board answered for this entry. If the company is on a different "
                f"platform, remove the '{ats}:' prefix and let it auto-detect.")
    return (f"Either the company is on a platform this Actor does not read ({UNSUPPORTED}), "
            f"or the board is private. Pasting the full careers URL usually resolves it - "
            f"the link a job posting sits on is enough.")


# --------------------------------------------------------------------------
# reading the input
# --------------------------------------------------------------------------


def read_companies(cfg: dict) -> list:
    """Turn whatever arrived in `companies` into clean, deduplicated targets.

    Every branch here is something a real caller sent, and each one used to be
    billed for:

    * A bare string. `"companies": "stripe"` is the natural thing to write by
      hand against the API. Python iterates it one character at a time, so the
      run fetched six companies named s, t, r, i, p, e and charged for six.
    * Duplicates. The same company twice - or once as `Stripe` and once as
      `stripe` - was fetched twice, charged twice, and returned every job
      twice.
    * Junk. A stray `null` or number became a token via `str()` and went out
      to the network as one.

    Every row this Actor pushes is charged for, so a parsing mistake here is
    not a wrong answer the caller can shrug at. It is a wrong answer they paid
    for.
    """
    raw = cfg.get("companies")
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

    targets, seen, skipped, dupes = [], set(), [], 0
    for entry in raw:
        if isinstance(entry, bool) or isinstance(entry, (int, float)):
            skipped.append(repr(entry))
            continue
        ats, token = parse_target(entry)
        if not token:
            if entry not in (None, "", [], {}):
                skipped.append(repr(entry))
            continue
        key = (ats or "auto", token.lower())
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        targets.append((ats, token, target_variants(entry, token)))

    if skipped:
        Actor.log.warning(
            "Ignored %d entry/entries in 'companies' that are not usable "
            "tokens: %s. Nothing was fetched or charged for them."
            % (len(skipped), ", ".join(skipped[:5]))
        )
    if dupes:
        Actor.log.info(
            "Removed %d duplicate compan%s from 'companies'; each company is "
            "fetched and charged once."
            % (dupes, "y" if dupes == 1 else "ies")
        )
    return targets


def read_int(cfg: dict, key: str, default: int = 0) -> int:
    """A number field that survives a caller typing words into it.

    The input form constrains these, but the API does not, and a
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

        targets = read_companies(cfg)
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
        # 0 means no limit. A negative used to slip through and slice the list
        # to nothing, so the run "succeeded" with zero rows and no explanation
        # - treat it as the no-limit it was meant to be.
        max_per_company = read_int(cfg, "maxJobsPerCompany", 0)
        if max_per_company < 0:
            Actor.log.warning(
                "'maxJobsPerCompany' was %d; a negative limit is read as no "
                "limit rather than as zero jobs." % max_per_company
            )
        max_per_company = max_per_company if max_per_company > 0 else None
        posted_days = read_int(cfg, "postedWithinDays", 0)
        posted_days = posted_days if posted_days > 0 else None
        concurrency = max(1, min(read_int(cfg, "concurrency", 5) or 5, 20))
        # "jobs" is one row per opening, "companies" is one row per account,
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
        summaries: list[dict] = []

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
                            "recordType": "notFound",
                            "boardToken": token,
                            "ats": ats,
                            "error": error,
                            "hint": miss_hint(token, ats),
                        }
                    )
                    return

                totals["companies_ok"] += 1
                kept = 0
                matched: list[dict] = []
                new_ids: set[str] = set()
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
                    # The summary describes what passed the caller's filters,
                    # so it is built from the same set the job rows come from -
                    # including in delta mode, where a role that is still open
                    # but unchanged is part of the picture even though it is
                    # not worth a row of its own.
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
                        token=token, ats=ats or (matched[0].get("ats") if matched else None),
                        jobs=matched,
                        new_ids=new_ids if delta.enabled else None,
                        closed_count=None,   # filled in once every board is known
                    ))

                Actor.log.info(f"{token}: {kept} job(s) kept out of {len(jobs)} found")

            await asyncio.gather(*(handle(a, t, v) for a, t, v in targets))

        closed = delta.closed()
        if want_jobs:
            for row in closed:
                await Actor.push_data(row)
                await charge("apify-default-dataset-item")

        if want_summary:
            # Closed roles are only known once every board has been read, so
            # the count is attached here rather than inside handle().
            per_company: dict[str, int] = {}
            for row in closed:
                key = str(row.get("boardToken") or "")
                per_company[key] = per_company.get(key, 0) + 1
            for row in summaries:
                if delta.enabled:
                    shut = per_company.get(str(row.get("boardToken") or ""), 0)
                    row["closedRoles"] = shut
                    if row["newRoles"] is not None:
                        row["netChange"] = row["newRoles"] - shut
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
