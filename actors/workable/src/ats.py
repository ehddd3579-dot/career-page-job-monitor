"""Adapters for public applicant-tracking-system job board APIs.

Every adapter takes a board token and returns a list of jobs in one shared
schema, so a caller never has to care which ATS a company happens to use.

All six endpoints are public and unauthenticated. No proxy, no browser,
no API key.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("apify")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&nbsp;": " ", "&apos;": "'",
}

REMOTE_HINT = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    text = _TAG.sub(" ", html)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = _WS.sub(" ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip() or None


def blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# Domains a person is likely to paste instead of a board token.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_MULTI_TLD = {
    "co.uk", "com.au", "co.jp", "co.kr", "com.br", "co.nz", "co.za",
    "com.mx", "co.in", "com.sg", "co.il",
}


def normalize_token(raw: Any) -> str:
    """Turn whatever a human pasted into a board token.

    People keep lists of company *domains*, not ATS board slugs. Accepting
    only the slug is the single biggest barrier to using this Actor, so
    stripe.com, www.stripe.com, https://stripe.com/careers and jobs.stripe.com
    all resolve to `stripe`.

    Case is preserved: most platforms use lowercase slugs, but
    SmartRecruiters identifiers are case sensitive.
    """
    text = str(raw or "").strip().lstrip("@")
    if not text:
        return ""
    text = _SCHEME.sub("", text)
    host = text.split("/")[0].split("?")[0].strip()
    if "." not in host:
        return host.strip("/")
    host = re.sub(r"^www\.", "", host, flags=re.I)
    labels = [l for l in host.split(".") if l]
    if len(labels) >= 3 and ".".join(labels[-2:]).lower() in _MULTI_TLD:
        return labels[-3]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0]


def token_variants(raw: Any) -> list[str]:
    """Ordered board-token guesses for one pasted entry.

    Companies do not name their board consistently after their domain:
    stripe.com -> "stripe", but shield.ai -> "shieldai". Trying the obvious
    form first and the dot-stripped form second covers both without asking
    the user to go hunting for a slug.
    """
    text = _SCHEME.sub("", str(raw or "").strip().lstrip("@"))
    host = text.split("/")[0].split("?")[0].strip().strip("/")
    out = []
    primary = normalize_token(raw)
    if primary:
        out.append(primary)
    if "." in host:
        host = re.sub(r"^www\.", "", host, flags=re.I)
        labels = [l for l in host.split(".") if l]
        joined = "".join(labels)
        if joined and joined not in out:
            out.append(joined)
    return out


def as_dict(value: Any) -> dict:
    """Nested objects are not guaranteed. A board that sends a string where the
    schema promises an object must not take the whole company down."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def collect(items: Any, build, label: str = "") -> list[dict]:
    """Normalise raw records, skipping any single record that cannot be read.

    One malformed posting must never cost the caller the other 499. The builder
    may return None to skip a record deliberately.

    A record without a title or a link is not a job anyone can use, and the
    caller is billed per record - so it is dropped rather than returned.
    """
    out: list[dict] = []
    raw = 0
    for item in as_list(items):
        raw += 1
        if not isinstance(item, dict):
            continue
        try:
            record = build(item)
        except Exception:  # noqa: BLE001 - one bad record, not a bad board
            continue
        if not record or not record.get("title"):
            continue
        if not str(record.get("jobUrl") or "").startswith("http"):
            continue
        out.append(record)
    dropped = raw - len(out)
    if dropped:
        # Silent data loss is worse than noisy logs. If a board hands us
        # records we cannot read, say so rather than quietly under-reporting.
        log.warning(
            f"{label or 'board'}: skipped {dropped} of {raw} postings "
            f"(unreadable, unlisted, or missing a title/link)"
        )
    return out


def iso_time(value: Any) -> str | None:
    """Normalise a timestamp to ISO 8601.

    Lever returns epoch milliseconds; Greenhouse and Ashby return ISO strings.
    Callers should never have to know the difference.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit()
    ):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return (
                datetime.fromtimestamp(number, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    return blank(value)


def job(
    *,
    ats: str,
    token: str,
    job_id: Any,
    title: str | None,
    url: str | None,
    company: str | None = None,
    department: str | None = None,
    team: str | None = None,
    employment_type: str | None = None,
    location: str | None = None,
    is_remote: bool | None = None,
    workplace_type: str | None = None,
    published_at: Any = None,
    updated_at: Any = None,
    apply_url: str | None = None,
    salary: str | None = None,
    description: str | None = None,
) -> dict:
    """Build one normalised job record."""
    loc = blank(location)
    if is_remote is None:
        is_remote = bool(loc and REMOTE_HINT.search(loc))
    return {
        # A single run can now emit job rows, company summaries and
        # not-found rows into one dataset. Exported to CSV that is a ragged
        # sheet unless every row says which kind it is.
        "recordType": "job",
        "ats": ats,
        "boardToken": token,
        "companyName": blank(company) or token,
        "jobId": str(job_id),
        "title": blank(title),
        "department": blank(department),
        "team": blank(team),
        "employmentType": blank(employment_type),
        "location": loc,
        "isRemote": bool(is_remote),
        "workplaceType": blank(workplace_type),
        "salary": blank(salary),
        "publishedAt": iso_time(published_at),
        "updatedAt": iso_time(updated_at),
        "jobUrl": blank(url),
        "applyUrl": blank(apply_url) or blank(url),
        "description": description,
    }


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------


async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> Any:
    resp = await client.get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()


async def _greenhouse_departments(client: httpx.AsyncClient, token: str) -> dict[str, str]:
    """Map job id -> department name.

    The Greenhouse /jobs endpoint omits departments entirely, so without this
    the department filter would silently match nothing. Best effort: if the
    endpoint is unavailable we simply return no mapping.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{token.lower()}/departments"
    try:
        data = await _get(client, url)
    except Exception:  # noqa: BLE001 - enrichment must never fail the fetch
        return {}
    mapping: dict[str, str] = {}
    for dept in as_list(as_dict(data).get("departments")):
        name = blank(as_dict(dept).get("name"))
        if not name:
            continue
        for item in as_list(as_dict(dept).get("jobs")):
            job_id = as_dict(item).get("id")
            if job_id is not None:
                mapping[str(job_id)] = name
    return mapping


async def greenhouse(client: httpx.AsyncClient, token: str, want_desc: bool) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token.lower()}/jobs"
    params = {"content": "true"} if want_desc else None
    data = await _get(client, url, params=params)
    raw_jobs = as_list(as_dict(data).get("jobs"))
    if not raw_jobs:
        # Greenhouse is tried first during auto-detection, so most companies
        # miss here. Do not spend a second request on departments for them.
        return []
    departments = await _greenhouse_departments(client, token)

    def build(item: dict) -> dict:
        job_id = item.get("id")
        offices = [
            blank(as_dict(o).get("name")) for o in as_list(item.get("offices"))
        ]
        return job(
            ats="greenhouse",
            token=token,
            job_id=job_id,
            title=item.get("title"),
            company=item.get("company_name"),
            department=departments.get(str(job_id)),
            location=as_dict(item.get("location")).get("name")
            or ", ".join(o for o in offices if o),
            published_at=item.get("first_published"),
            updated_at=item.get("updated_at"),
            url=item.get("absolute_url"),
            description=html_to_text(item.get("content")) if want_desc else None,
        )

    return collect(raw_jobs, build, f"greenhouse/{token}")


async def lever(client: httpx.AsyncClient, token: str, want_desc: bool) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token.lower()}"
    data = await _get(client, url, params={"mode": "json"})

    def build(item: dict) -> dict:
        cat = as_dict(item.get("categories"))
        workplace = item.get("workplaceType")
        return job(
            ats="lever",
            token=token,
            job_id=item.get("id"),
            title=item.get("text"),
            department=cat.get("department"),
            team=cat.get("team"),
            employment_type=cat.get("commitment"),
            location=cat.get("location"),
            workplace_type=workplace,
            is_remote=(str(workplace).lower() == "remote") or None,
            published_at=item.get("createdAt"),
            url=item.get("hostedUrl"),
            apply_url=item.get("applyUrl"),
            description=(item.get("descriptionPlain") if want_desc else None),
        )

    return collect(data, build, f"lever/{token}")


async def ashby(client: httpx.AsyncClient, token: str, want_desc: bool) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token.lower()}"
    data = await _get(client, url, params={"includeCompensation": "true"})

    def build(item: dict) -> dict | None:
        if item.get("isListed") is False:
            return None
        remote = item.get("isRemote")
        return job(
            ats="ashby",
            token=token,
            job_id=item.get("id"),
            title=item.get("title"),
            department=item.get("department"),
            team=item.get("team"),
            employment_type=item.get("employmentType"),
            location=item.get("location"),
            is_remote=remote if isinstance(remote, bool) else None,
            workplace_type=item.get("workplaceType"),
            salary=as_dict(item.get("compensation")).get("compensationTierSummary"),
            published_at=item.get("publishedAt"),
            updated_at=item.get("updatedAt"),
            url=item.get("jobUrl"),
            apply_url=item.get("applyUrl"),
            description=(item.get("descriptionPlain") if want_desc else None),
        )

    return collect(as_dict(data).get("jobs"), build, f"ashby/{token}")


async def workable(client: httpx.AsyncClient, token: str, want_desc: bool) -> list[dict]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token.lower()}"
    data = await _get(client, url, params={"details": "true"})
    company = as_dict(data).get("name")

    def build(item: dict) -> dict:
        parts = [blank(item.get(k)) for k in ("city", "state", "country")]
        remote = item.get("telecommuting")
        return job(
            ats="workable",
            token=token,
            job_id=item.get("shortcode") or item.get("id"),
            title=item.get("title"),
            company=company,
            department=item.get("department"),
            employment_type=item.get("employment_type"),
            location=", ".join(p for p in parts if p),
            is_remote=remote if isinstance(remote, bool) else None,
            published_at=item.get("published_on") or item.get("created_at"),
            url=item.get("url") or item.get("application_url"),
            apply_url=item.get("application_url"),
            description=html_to_text(item.get("description")) if want_desc else None,
        )

    return collect(as_dict(data).get("jobs"), build, f"workable/{token}")


async def recruitee(client: httpx.AsyncClient, token: str, want_desc: bool) -> list[dict]:
    url = f"https://{token.lower()}.recruitee.com/api/offers/"
    data = await _get(client, url)

    def build(item: dict) -> dict:
        parts = [blank(item.get("city")), blank(item.get("country"))]
        loc = ", ".join(p for p in parts if p) or blank(item.get("location"))
        if not loc:
            names = [
                blank(as_dict(l).get("city") or as_dict(l).get("name"))
                for l in as_list(item.get("locations"))
            ]
            loc = ", ".join(n for n in names if n)
        remote = item.get("remote")
        return job(
            ats="recruitee",
            token=token,
            job_id=item.get("id"),
            title=item.get("title") or item.get("position"),
            company=item.get("company_name"),
            department=item.get("department"),
            employment_type=item.get("employment_type_code"),
            location=loc,
            is_remote=remote if isinstance(remote, bool) else None,
            published_at=item.get("published_at") or item.get("created_at"),
            updated_at=item.get("updated_at"),
            url=item.get("careers_url") or item.get("careers_apply_url"),
            apply_url=item.get("careers_apply_url"),
            description=html_to_text(item.get("description")) if want_desc else None,
        )

    return collect(as_dict(data).get("offers"), build, f"recruitee/{token}")


async def smartrecruiters(client: httpx.AsyncClient, token: str, want_desc: bool) -> list[dict]:
    def build(item: dict) -> dict:
        loc = as_dict(item.get("location"))
        country = blank(loc.get("country"))
        if country and len(country) == 2:
            country = country.upper()  # the API returns "us", humans expect "US"
        parts = [blank(loc.get("city")), blank(loc.get("region")), country]
        posting_id = item.get("id")
        # The list endpoint returns `ref`, which is an API URL a human cannot
        # open, and omits postingUrl/applyUrl. Build the public careers link.
        public_url = item.get("postingUrl") or (
            f"https://jobs.smartrecruiters.com/{token}/{posting_id}"
            if posting_id else None
        )
        remote = loc.get("remote")
        return job(
            ats="smartrecruiters",
            token=token,
            job_id=posting_id,
            title=item.get("name"),
            company=as_dict(item.get("company")).get("name"),
            department=as_dict(item.get("department")).get("label"),
            employment_type=as_dict(item.get("typeOfEmployment")).get("label"),
            location=", ".join(p for p in parts if p),
            is_remote=remote if isinstance(remote, bool) else None,
            published_at=item.get("releasedDate"),
            url=public_url,
            apply_url=item.get("applyUrl") or public_url,
        )

    # SmartRecruiters company identifiers are case sensitive, unlike every
    # other platform here. A person pasting "visa" or "visa.com" should still
    # find "Visa", so try the sensible casings before giving up.
    candidates = [token]
    for variant in (token.capitalize(), token.upper(), token.lower()):
        if variant not in candidates:
            candidates.append(variant)

    resolved = None
    last_error: Exception | None = None
    for candidate in candidates:
        probe = f"https://api.smartrecruiters.com/v1/companies/{candidate}/postings"
        try:
            first = as_dict(await _get(client, probe, params={"limit": 100, "offset": 0}))
        except Exception as exc:  # noqa: BLE001 - try the next casing
            last_error = exc
            continue
        resolved, url = candidate, probe
        break
    if resolved is None:
        raise last_error if last_error else httpx.HTTPError("smartrecruiters: no match")

    out: list[dict] = collect(as_list(first.get("content")), build,
                              f"smartrecruiters/{resolved}")
    offset = len(as_list(first.get("content")))
    total = first.get("totalFound")
    if not out and not offset:
        return out
    while isinstance(total, int) and offset < total and offset < 2000:
        data = as_dict(await _get(client, url, params={"limit": 100, "offset": offset}))
        page = as_list(data.get("content"))
        if not page:
            break
        out.extend(collect(page, build, f"smartrecruiters/{resolved}"))
        offset += len(page)
        total = data.get("totalFound")
    return out


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "recruitee": recruitee,
    "smartrecruiters": smartrecruiters,
}

# Order matters for auto-detection: cheapest and most common first.
DETECTION_ORDER = ["greenhouse", "ashby", "lever", "workable", "recruitee", "smartrecruiters"]


