"""Lever Jobs Scraper.

Reads the official public Lever board API. One Actor, one platform, no
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

ATS = "lever"
_TOKEN_FROM_URL = re.compile(r"jobs\.lever\.co/([\w.-]+)", re.I)


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
                "Input 'boards' is empty. Add Lever board tokens - the company slug in jobs.lever.co/<company>."
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

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_days)
            if posted_days else None
        )

        Actor.log.info("%d Lever board(s), concurrency %d" % (len(boards), concurrency))
        adapter = ADAPTERS[ATS]
        semaphore = asyncio.Semaphore(concurrency)
        totals = {"jobs": 0, "ok": 0, "failed": 0}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LeverJobsScraper/1.0)",
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

                if error:
                    totals["failed"] += 1
                    Actor.log.warning("%s: %s" % (token, error))
                    await Actor.push_data({
                        "boardToken": token,
                        "ats": ATS,
                        "error": error,
                        "hint": "Check the token - it is the company slug in jobs.lever.co/<company>.",
                    })
                    return

                totals["ok"] += 1
                kept = 0
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

                    await Actor.push_data(item)
                    await charge("apify-default-dataset-item")
                    kept += 1
                    totals["jobs"] += 1

                Actor.log.info("%s: %d job(s) kept out of %d found" % (token, kept, len(jobs)))

            await asyncio.gather(*(handle(e) for e in raw_entries))

        await Actor.set_status_message(
            "Done. %d jobs from %d Lever board(s) (%d not found)."
            % (totals["jobs"], totals["ok"], totals["failed"])
        )
