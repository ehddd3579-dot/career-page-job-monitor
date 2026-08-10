# SmartRecruiters Jobs Scraper - Every Opening From Any Company

**A company that is hiring is a company that is spending. Pull every opening from any SmartRecruiters careers site via the official public API, paging through the whole board so large employers come back complete, not truncated.**

---

## What it does

Give it a list of SmartRecruiters board tokens. It calls the official public SmartRecruiters
board API for each one and returns every live opening in a flat,
spreadsheet-ready schema.

This reads the API SmartRecruiters already publishes for its customers' own careers
pages. That means **no proxy, no headless browser, no anti-bot arms race** — and
nothing that silently breaks when a page layout changes.

SmartRecruiters pages its API at 100 postings per request. This Actor walks every page, so large enterprise boards come back complete, not truncated.

---

## Input

```json
{
  "boards": ["Ubisoft2", "Colliers"],
  "titleKeywords": ["engineer"],
  "locations": ["Remote"],
  "postedWithinDays": 7
}
```

The board token is **the identifier in careers.smartrecruiters.com/<Company> (case sensitive)** — but you rarely need to look it up.
Paste the company **domain** (`acme.com`, `www.acme.com`) or the full careers URL
and the Actor pulls the company name out for you.

| Field | Default | Notes |
|---|---|---|
| `boards` | — | Required. Tokens or full careers URLs. |
| `titleKeywords` | — | Keep only titles containing any of these. |
| `excludeKeywords` | — | Drop titles or departments matching any of these. |
| `locations` | — | Keep only matching locations. |
| `departments` | — | Matches department **or** team. |
| `remoteOnly` | `false` | Remote positions only. |
| `postedWithinDays` | `0` | `0` = no limit. Set `1` for a daily new-jobs feed. |
| `includeDescription` | `false` | Adds full description text. Much larger results. |
| `onlyNewSinceLastRun` | `false` | Return only what opened or closed since the last run. |
| `maxJobsPerBoard` | `0` | `0` = no limit. The console starts at `10` so a first trial run stays cheap. |
| `concurrency` | `5` | Boards fetched in parallel. |

---

## Output

One row per opening:

| Field | Description |
|---|---|
| `companyName`, `boardToken`, `ats` | Who, and where it was read from |
| `jobId` | Stable ID on SmartRecruiters |
| `title` | Job title |
| `department`, `team` | Org placement |
| `employmentType` | Full-time, contract, intern… |
| `location`, `isRemote`, `workplaceType` | Where the work happens |
| `salary` | Compensation range, when published |
| `publishedAt`, `updatedAt` | ISO 8601 timestamps |
| `jobUrl`, `applyUrl` | Public posting and application links |
| `globalId` | `{ats}:{token}:{jobId}` — stable key for joining and deduping |
| `description` | Full text, only when requested |

Export as **Excel, CSV, JSON or XML**, or pull it through the API.

A board that cannot be found returns one row with an `error` and a hint, so a bad
token never silently vanishes from your results.

---

## Monitoring: only what changed

Set **`onlyNewSinceLastRun: true`** and schedule it. The first run records a
baseline and returns everything. Every run after that returns only:

- jobs that **opened** since your last run — `isNew: true`
- jobs that have since **closed** — `isClosed: true`

A morning with no hiring activity returns **nothing at all** and costs only the
start fee. You are never billed for re-reading a job you already saw.

Two things worth knowing:

- **Changing the boards or filters starts a fresh baseline.** Otherwise every
  job you stopped asking about would be reported as newly closed, which is a
  wrong answer rather than a noisy one.
- **A closed row carries what was recorded when the job was last seen** —
  company, title and link. Once a posting is gone there is nothing left to
  re-read, so the remaining fields are omitted rather than shown stale.

### Without monitoring

`postedWithinDays: 1` also gives you a daily feed, based on each board's own
posted date. It is simpler, but it cannot tell you when a role closed, and it
depends on the company having set a date at all. Use `onlyNewSinceLastRun` if
you care about either.

---

## Quality notes

- Timestamps are normalised to ISO 8601, so date filters behave predictably.
- Postings that cannot be read are skipped and **reported in the log** rather
  than quietly dropped — and you are not billed for them.
- One malformed posting never takes down the rest of the board.

---

## Limits

- SmartRecruiters boards only. For a company on a different platform, see the
  multi-platform version of this Actor.
- Boards set to private or password-protected are not accessible.
- `department` and `team` are only as good as what the company fills in.

---

## The endpoint this reads

```
https://api.smartrecruiters.com/v1/companies/<Company>/postings
```

That is SmartRecruiters's own public board API - the one that fills its customers'
careers pages. It needs no key, no login and no proxy, which is why this Actor
does not use any.

---

## FAQ

**Does SmartRecruiters have a public jobs API?**
Yes, for job boards. The endpoint above returns the live postings for one
company as JSON. It is public because the careers page itself is public.

**Do I need an API key, a login, or a proxy?**
No. None of the three.

**Where do I find the board token?**
You do not have to. Paste the company's domain, the URL of any job posting, or
the identifier itself - `Ubisoft2` is a working example - and this Actor
resolves it. If you already know the token - the identifier in careers.smartrecruiters.com/<Company> (case sensitive) - that works too.

**Why would I use this instead of calling the endpoint myself?**
For one company, you probably should not - it is one HTTP request, and this
README just told you the URL. This Actor earns its keep at the point where that
stops being true: dozens of companies at once, tokens you would otherwise have
to look up by hand, a flat schema shared across six ATS platforms, a
`globalId` you can join on, and a mode that returns only what changed since the
last run instead of the whole board every morning.

**Can I get only the jobs that opened or closed since last time?**
Yes. Set `onlyNewSinceLastRun` to true and each run returns the new postings,
plus a row for every posting that disappeared. See the monitoring section above.

**What does a run cost?**
You pay per row returned - about a tenth of a cent each - plus a fraction of a
cent to start the run. A company with 40 openings lands around five cents. With
`onlyNewSinceLastRun` on, a scheduled daily run usually returns a handful of
rows rather than the whole board.

**A company is not found. Why?**
Either it is not on SmartRecruiters - most large enterprises use Workday, Taleo, iCIMS
or SuccessFactors - or its board is private. The run tells you which, per
company, in a row you can read rather than a silent gap.

---

## Support

A board that will not resolve? Open an issue on the **Issues** tab with the
careers URL.
