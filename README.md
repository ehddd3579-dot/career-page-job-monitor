# ATS Job Scraper — Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters

**You name the companies. It returns their live job openings.**

![Sample output: one normalised table across all six ATS platforms](https://raw.githubusercontent.com/udaninn/career-page-job-monitor/main/docs/output-sample.svg)

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
    "stripe.com",
    "ashby:ramp",
    "https://jobs.lever.co/shieldai",
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
| **`stripe.com`** | **the domain — paste your account list as-is** |
| **`www.stripe.com`, `https://stripe.com/careers`** | **same company** |
| `greenhouse:stripe` | forces Greenhouse |
| `https://jobs.ashbyhq.com/ramp` | Ashby, token `ramp` |
| `https://apply.workable.com/acme/` | Workable, token `acme` |

**You do not need to look up board tokens.** Most people keep a list of company
domains, not ATS slugs — so paste the domains. The Actor pulls the company name
out and finds the board itself.

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
| `onlyNewSinceLastRun` | `false` | Return only what opened or closed since the last run. |
| `maxJobsPerCompany` | `0` | `0` = no limit. The console starts at `10` so a first trial run stays cheap. |
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
| `globalId` | `{ats}:{token}:{jobId}` — stable key for joining and deduping |
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

### Monitoring: only what changed

Set **`onlyNewSinceLastRun: true`** and schedule it. The first run records a
baseline and returns everything. Every run after that returns only:

- roles that **opened** since your last run — `isNew: true`
- roles that have since **closed** — `isClosed: true`

A morning with no hiring activity returns **nothing at all** and costs only the
start fee. You are never billed for re-reading a job you already saw. Wire it to
Slack, Google Sheets or your CRM through Apify integrations and it stays quiet
until an account actually moves.

Two things worth knowing:

- **Changing the companies or filters starts a fresh baseline.** Otherwise every
  job you stopped asking about would be reported as newly closed, which is a
  wrong answer rather than a noisy one.
- **A closed row carries what was recorded when the job was last seen** —
  company, title and link. Once a posting is gone there is nothing left to
  re-read, so the remaining fields are omitted rather than shown stale.

`postedWithinDays: 1` also gives you a daily feed, based on each company's own
posted date. It is simpler, but it cannot tell you when a role closed, and it
depends on the company having set a date at all.

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

## How this works, in full

The six endpoints this reads are public and documented. If you would rather
build it yourself than pay for it, the whole method is written up here — every
endpoint, and the four traps that cost the most time:

**[Six ATS platforms publish their job boards as open JSON. Here are the
endpoints.](https://dev.to/udaninn/six-ats-platforms-publish-their-job-boards-as-open-json-here-are-the-endpoints-2d3k)**

For one company you probably should build it yourself — it is a single HTTP
request. This Actor earns its keep at the point where that stops being true.

---

## Support

Missing a platform, or a company that will not resolve? Open an issue on the
**Issues** tab with the careers URL. New adapters are quick to add.
