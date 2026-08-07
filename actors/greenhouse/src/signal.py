"""One row per company: what their hiring says about them.

A list of 500 job rows is a developer's input. A sales team wants one line per
account - how many roles are open, which teams are growing, what opened this
week - so they can decide who to call. Same data, different unit of analysis.

Everything here is counted from postings already fetched, so it costs no extra
requests.

**On honesty of the derived fields.** `departments` and `locations` come
straight from what the company published. `roleFamilies` and `seniorityMix` are
*inferred from job titles* and are therefore guesses. Two safeguards keep them
from reading as facts they are not:

* Anything that does not match cleanly lands in `other`, rather than being
  forced into the nearest bucket to make the numbers look tidy.
* `sampleTitles` ships the real titles alongside, so a reader can check the
  inference instead of trusting it.

There is deliberately no single composite "hiring score". A number like that
looks precise and is not; the counts underneath are what a person can actually
act on.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# The first family that matches wins, so order encodes precedence.
#
# Note there is a leading \b but deliberately no trailing one. An earlier
# version wrapped each group as `\b(engineer|...)\b` and quietly sent
# "Engineering Manager" and "Data Scientist" to `other`, because the trailing
# boundary fails the moment a stem grows a suffix. Checked against 1,061 real
# titles from three live boards, that single mistake was misfiling 30% of
# postings. Match stems from the front and let the suffix run.
_ROLE_FAMILIES: list[tuple[str, re.Pattern]] = [
    ("sales", re.compile(
        r"\b(account executive|account manager|sales|business development|"
        r"partner development|strategic alliance|bdr\b|sdr\b|partnership|revenue|"
        r"customer success|deal strateg|deal pricing|pricing strateg|"
        r"business value consultant|solutions consultant|technical account manage)", re.I)),
    ("marketing", re.compile(
        r"\b(marketing|brand |content strateg|content design|content manager|seo\b|"
        r"growth |demand gen|communications|public relations|art director|"
        r"creative director|social media|paid media)", re.I)),
    # `billing` alone is not enough: "Backend / API Engineer, Metronome (Billing)"
    # is an engineering role that happens to work on a billing product.
    ("finance", re.compile(
        r"\b(finance|financial|accounting|accountant|accounts receivable|"
        r"accounts payable|controller|treasury|audit|\btax |corporate development|"
        r"m&a|billing operations|billing specialist|payroll|procurement|fp&a|"
        r"investment)", re.I)),
    ("people", re.compile(
        r"\b(recruit|talent|people |human resources|\bhr |compensation|benefits|"
        r"employee relations|workplace|learning & )", re.I)),
    ("legal", re.compile(
        r"\b(legal|counsel|attorney|compliance|privacy|regulatory|policy|"
        r"sanctions|investigations)", re.I)),
    ("design", re.compile(r"\b(designer|design |\bux |\bui |user experience|user research)", re.I)),
    ("product", re.compile(r"\bproduct (manager|management|owner|lead|specialist)", re.I)),
    ("data", re.compile(
        r"\b(data scien|data engineer|data analy|data platform|analytics|analyst|"
        r"machine learning|\bml engineer|research scien|quantitative)", re.I)),
    ("engineering", re.compile(
        r"\b(engineer|developer|programmer|architect|\bsre\b|devops|infrastructure|"
        r"security|\bqa\b|software|technical program|site reliability)", re.I)),
    ("support", re.compile(r"\b(support|help desk|service desk|customer experience)", re.I)),
    ("operations", re.compile(
        r"\b(operation|logistics|supply chain|program manager|project manager|"
        r"facilities|administrative|executive assistant|strategy|strategist|"
        r"coordinator|specialist|events)", re.I)),
]

# Titles that carry "manager" without managing anyone. Counting these as
# management is how a board like Anthropic's ends up looking 32% managerial:
# of 125 hits, a third were Account, Program and Contracts Managers, which are
# individual contributors everywhere. Checked against the live board.
_IC_MANAGER = re.compile(
    r"\b(account|program|project|product|contracts?|community|customer success)\s+"
    r"(programs?\s+)?manager", re.I)

# Checked in this order: a "Senior Director" is leadership, not senior.
_SENIORITY: list[tuple[str, re.Pattern]] = [
    ("leadership", re.compile(r"\b(chief|cto|ceo|cfo|coo|cmo|vp|vice president|"
                              r"head of|director|president)\b", re.I)),
    ("management", re.compile(r"\b(manager|management|lead|supervisor)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|staff|principal|distinguished|"
                          r"expert|iii|iv)\b", re.I)),
    ("junior", re.compile(r"\b(intern|internship|graduate|junior|jr\.?|entry|"
                          r"apprentice|trainee|working student)\b", re.I)),
]

MAX_BUCKETS = 10          # a hundred one-off department names help nobody
SAMPLE_TITLES = 5


def role_family(title: str | None) -> str:
    for name, pattern in _ROLE_FAMILIES:
        if title and pattern.search(title):
            return name
    return "other"


def seniority(title: str | None) -> str:
    for name, pattern in _SENIORITY:
        if title and pattern.search(title):
            if name == "management" and _IC_MANAGER.search(title):
                # "Manager" in the title, nobody reporting to them. Keep
                # checking the remaining levels rather than stopping, so a
                # Senior Account Manager still lands in `senior` instead of
                # being flattened to mid.
                continue
            return name
    # Not "junior". A title with no level word is most often a normal
    # individual-contributor role, and guessing junior would understate a
    # company's hiring seniority.
    return "mid"


def _top(counter: Counter, limit: int = MAX_BUCKETS) -> dict[str, int]:
    return dict(counter.most_common(limit))


def summarise(
    company: str,
    token: str,
    ats: str | None,
    jobs: list[dict],
    new_ids: set[str] | None = None,
    closed_count: int | None = None,
) -> dict:
    """Build one company row from that company's postings.

    `jobs` are the postings that survived the caller's filters, so the summary
    describes what they asked about rather than the whole board. `new_ids` and
    `closed_count` are only meaningful in change-detection mode; without it the
    change fields are left as None rather than reported as zero, because "no
    new roles" and "we did not look" are different statements.
    """
    families, levels, depts, locs = Counter(), Counter(), Counter(), Counter()
    remote = with_salary = 0
    newest = None

    for item in jobs:
        title = item.get("title")
        families[role_family(title)] += 1
        levels[seniority(title)] += 1
        if item.get("department"):
            depts[str(item["department"])] += 1
        if item.get("location"):
            locs[str(item["location"])] += 1
        if item.get("isRemote"):
            remote += 1
        if item.get("salary"):
            with_salary += 1
        posted = item.get("publishedAt")
        if posted and (newest is None or str(posted) > str(newest)):
            newest = posted

    open_roles = len(jobs)
    new_count = len(new_ids) if new_ids is not None else None
    net = None
    if new_count is not None and closed_count is not None:
        net = new_count - closed_count

    return {
        "recordType": "companySummary",
        "companyName": company or token,
        "boardToken": token,
        "ats": ats,
        "openRoles": open_roles,
        "newRoles": new_count,
        "closedRoles": closed_count,
        "netChange": net,
        # Published by the company - these are facts.
        "departments": _top(depts),
        "locations": _top(locs),
        "remoteRoles": remote,
        "rolesWithPublishedSalary": with_salary,
        "newestPostedAt": newest,
        # Inferred from titles - these are estimates. sampleTitles is here so
        # the estimate can be checked rather than taken on trust.
        "roleFamilies": _top(families),
        "seniorityMix": _top(levels),
        "sampleTitles": [j.get("title") for j in jobs[:SAMPLE_TITLES] if j.get("title")],
    }



