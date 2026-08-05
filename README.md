# ATS Job Scraper — Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters

**You name the companies. It returns their live job openings.**

Give it a list of companies you actually care about. It works out which applicant
tracking system each one uses — Greenhouse, Lever, Ashby, Workable, Recruitee or
SmartRecruiters — reads their official public board API, and returns every opening
in **one normalised schema**.

No proxy. No browser. No API key. Nothing to get blocked.

---

## This is not a job firehose

Most job APIs sell you *everything* — hundreds of thousands of companies — and
leave you to filter down to the handful you care about. That is the right product
if you are building a job board.

It is the wrong product if you already know which companies matter to you.

| | Whole-market job feeds | This Actor |
|---|---|---|
| You get | Every company they index | **The companies you listed** |
| Targeting | Search keywords, hope | You name them |
| Noise | Filter down from 175k+ | There is none to filter |
| Data source | Aggregated | The company's own ATS API |
| Freshness | Whenever they re-crawled | Live, at run time |
| Blocking | Anti-bot, proxies | Public APIs |

If your list is *"our 200 target accounts"*, *"our 40 competitors"*, or *"the 60
companies I would actually work for"*, this is built for you.

---

## Supported platforms

| Platform | Board token is the slug in |
|---|---|
| **Greenhouse** | `boards.greenhouse.io/<token>` · `job-boards.greenhouse.io/<token>` |
| **Lever** | `jobs.lever.co/<token>` |
| **Ashby** | `jobs.ashbyhq.com/<token>` |
| **Workable** | `apply.workable.com/<token>` |
| **Recruitee** | `<token>.recruitee.com` |
| **SmartRecruiters** | `careers.smartrecruiters.com/<token>` |

You do not have to know which one a company uses. Give the plain name and it
tries each platform until it finds the board.

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

**`companies` accepts anything sensible:**

| You write | It understands |
|---|---|
| `stripe` | auto-detects the platform |
| `greenhouse:stripe` | forces Greenhouse |
| `https://jobs.ashbyhq.com/ramp` | Ashby, token `ramp` |
| `https://apply.workable.com/acme/` | Workable, token `acme` |

| Field | Default | Notes |
|---|---|---|
| `companies` | — | Required. |
| `titleKeywords` | — | Keep only titles containing any of these. |
| `excludeKeywords` | — | Drop titles or departments matching any of these. |
| `locations` | — | Keep only matching locations. |
| `departments` | — | Matches department **or** team. |
| `remoteOnly` | `false` | Remote positions only. |
| `postedWithinDays` | `0` | `0` = no limit. Set `1` for a daily new-jobs feed. |
| `includeDescription` | `false` | Adds full description text. Much larger results. |
| `maxJobsPerCompany` | `0` | `0` = no limit. |
| `concurrency` | `5` | Companies fetched in parallel. |

---

## Output

One item per opening, identical shape no matter which platform it came from:

| Field | Description |
|---|---|
| `companyName`, `boardToken`, `ats` | Who, and which platform it was read from |
| `jobId` | Stable ID on the source platform |
| `title` | Job title |
| `department`, `team` | Org placement, when the platform exposes it |
| `employmentType` | Full-time, contract, intern… |
| `location`, `isRemote`, `workplaceType` | Where the work happens |
| `salary` | Compensation range, when the company publishes it |
| `publishedAt`, `updatedAt` | ISO 8601, normalised across all six platforms |
| `jobUrl`, `applyUrl` | Public posting and application links |
| `description` | Full text, only when requested |

Export as **Excel, CSV, JSON or XML**, or pull it through the API.

Companies whose board cannot be found return one row with an `error` and a hint,
so a bad token never silently vanishes from your results.

---

## What people use it for

- **Sales and lead-gen** — a company that is hiring is a company that is spending.
  Watch your target accounts and catch them the week they start growing a team.
- **Competitive intelligence** — see exactly which roles a rival opened, in which
  city, on which team, at what salary.
- **Recruiting and sourcing** — track where talent demand is moving.
- **Job hunting** — follow the 60 companies you would actually join, filtered to
  your title and location.

### Daily hiring-signal recipe

Schedule it once a day with `postedWithinDays: 1` and you get a clean feed of
roles opened in the last 24 hours across every company you track. Wire it to
Slack, Google Sheets or your CRM through Apify integrations.

---

## Why it stays cheap and does not break

It reads the **official public board API** each platform already publishes for
their customers' own career sites. That means no proxy fees, no headless browser,
no anti-bot arms race — and no silent data loss when a page layout changes.

Timestamps are normalised to ISO 8601 across all six platforms, so a date filter
behaves the same everywhere. Departments are joined in for Greenhouse, which
omits them from its jobs endpoint. Postings that cannot be read are skipped and
**reported in the log** rather than quietly dropped — and you are not billed for them.

---

## Limits

- Only the six platforms above. Custom in-house career pages are not covered.
- Boards set to private or password-protected are not accessible.
- `department` and `team` are only as good as what the company fills in.
- Salary appears only where the company publishes it.
- SmartRecruiters boards are read up to 2,000 postings per company.

---

## Pricing

Pay per event: a small start fee per run, plus a charge per job returned.
Companies that return no jobs cost only the start fee. Platform usage is
included — you are not billed for compute on top.

---

## Support

Missing a platform, or a company that will not resolve? Open an issue on the
**Issues** tab with the careers URL. New adapters are quick to add.
