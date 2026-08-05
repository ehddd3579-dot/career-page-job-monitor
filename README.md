# Career Page Job Monitor — jobs straight from company career pages

Give it a list of companies. It works out which applicant tracking system each one
uses, pulls their live openings from the official public job board API, and returns
everything in **one normalised schema**.

Supports **Greenhouse, Lever, Ashby, Workable, Recruitee and SmartRecruiters** —
six platforms, one output format, no API keys.

---

## Why this instead of a job board scraper

Job aggregators show you a stale, deduplicated, partial copy of the market. This
Actor reads the **source of truth**: the company's own board API.

| | Aggregator scrapers | This Actor |
|---|---|---|
| Data source | Third-party listing site | Company's own ATS API |
| Freshness | Whenever the aggregator re-crawled | Live |
| Coverage | Whatever the aggregator indexed | Every open role on the board |
| Targeting | Search keywords, hope | **You name the companies** |
| Blocking | Anti-bot, proxies, CAPTCHAs | Public APIs, nothing to block |

You pick the companies. That is the point: you usually already know who you care
about — your target accounts, your competitors, your portfolio, your shortlist.

---

## What you can do with it

- **Sales and lead-gen signals** — a company that is hiring is a company that is
  spending. Watch your target accounts and catch them when they start growing a team.
- **Competitive intelligence** — see exactly which roles a rival is opening, in which
  city, on which team.
- **Recruiting and sourcing** — track where talent demand is moving.
- **Job hunting** — follow 200 companies you would actually work for, filtered to
  your title and location, instead of refreshing job boards.
- **Market research** — headcount plans are public if you know where to look.

---

## Input

```json
{
  "companies": [
    "stripe",
    "ashby:ramp",
    "https://jobs.lever.co/netflix",
    "https://job-boards.greenhouse.io/anthropic"
  ],
  "titleKeywords": ["engineer", "designer"],
  "excludeKeywords": ["intern"],
  "locations": ["New York", "Remote"],
  "remoteOnly": false,
  "postedWithinDays": 7
}
```

**Companies accepts anything sensible:**

| You write | It understands |
|---|---|
| `stripe` | auto-detects the ATS |
| `greenhouse:stripe` | forces Greenhouse |
| `https://jobs.ashbyhq.com/ramp` | Ashby, token `ramp` |
| `https://boards.greenhouse.io/airbnb` | Greenhouse, token `airbnb` |
| `https://apply.workable.com/acme/` | Workable, token `acme` |

The board token is just the company slug in their careers URL.

| Field | Default | Notes |
|---|---|---|
| `companies` | — | Required. |
| `titleKeywords` | — | Keep only titles containing any of these. |
| `excludeKeywords` | — | Drop titles or departments matching any of these. |
| `locations` | — | Keep only matching locations. |
| `departments` | — | Keep only matching departments or teams. |
| `remoteOnly` | `false` | Remote positions only. |
| `postedWithinDays` | `0` | `0` = no limit. Set to `1` for a daily new-jobs feed. |
| `includeDescription` | `false` | Adds full description text. Much larger results. |
| `maxJobsPerCompany` | `0` | `0` = no limit. |
| `concurrency` | `5` | Companies fetched in parallel. |

---

## Output

One item per job opening, identical shape no matter which ATS it came from:

| Field | Description |
|---|---|
| `companyName`, `boardToken`, `ats` | Who, and where it was read from |
| `jobId` | Stable ID on the source platform |
| `title` | Job title |
| `department`, `team` | Org placement, when the ATS exposes it |
| `employmentType` | Full-time, contract, intern… |
| `location`, `isRemote`, `workplaceType` | Where the work happens |
| `salary` | Compensation summary, when published |
| `publishedAt`, `updatedAt` | Timestamps |
| `jobUrl`, `applyUrl` | Public posting and application links |
| `description` | Full text, only when requested |

Export as **Excel, CSV, JSON or XML** from the Console, or pull it through the API.

Companies whose board cannot be found return a single row with an `error` and a
hint, so a bad token never silently disappears from your results.

---

## Daily hiring-signal recipe

Schedule the Actor once a day with `postedWithinDays: 1`, and you get a clean feed
of roles opened in the last 24 hours across every company you track. Wire it to
Slack, a Google Sheet, or your CRM through Apify integrations.

---

## Limits

- Only companies using one of the six supported platforms. Custom in-house career
  pages are not covered.
- Boards set to private or password-protected are not accessible.
- `department` and `team` are only as good as what the company fills in.
- Salary appears only where the company publishes it.

---

## Pricing

Pay per event: a small start fee per run plus a charge per job returned. Companies
that return no jobs cost only the start fee.

---

## Support

Missing an ATS platform, or a company that will not resolve? Open an issue on the
**Issues** tab with the careers URL. New adapters are quick to add.
