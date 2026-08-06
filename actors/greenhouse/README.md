# Greenhouse Jobs Scraper - Every Opening From Any Greenhouse Board

**Pull live job openings straight from any Greenhouse job board via the official public API, with departments joined in. Give it company slugs, get every role with title, department, location and apply link. No proxy, no API key.**

---

## What it does

Give it a list of Greenhouse board tokens. It calls the official public Greenhouse
board API for each one and returns every live opening in a flat,
spreadsheet-ready schema.

This reads the API Greenhouse already publishes for its customers' own careers
pages. That means **no proxy, no headless browser, no anti-bot arms race** — and
nothing that silently breaks when a page layout changes.

Greenhouse omits departments from its jobs endpoint. This Actor also reads the departments endpoint and joins them in, so `department` is populated where most Greenhouse scrapers leave it empty.

---

## Input

```json
{
  "boards": ["stripe", "https://job-boards.greenhouse.io/anthropic"],
  "titleKeywords": ["engineer"],
  "locations": ["Remote"],
  "postedWithinDays": 7
}
```

The board token is **the company slug in job-boards.greenhouse.io/<company>**. You can paste the full careers URL instead
and it will pull the token out for you.

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
| `maxJobsPerBoard` | `0` | `0` = no limit. |
| `concurrency` | `5` | Boards fetched in parallel. |

---

## Output

One row per opening:

| Field | Description |
|---|---|
| `companyName`, `boardToken`, `ats` | Who, and where it was read from |
| `jobId` | Stable ID on Greenhouse |
| `title` | Job title |
| `department`, `team` | Org placement |
| `employmentType` | Full-time, contract, intern… |
| `location`, `isRemote`, `workplaceType` | Where the work happens |
| `salary` | Compensation range, when published |
| `publishedAt`, `updatedAt` | ISO 8601 timestamps |
| `jobUrl`, `applyUrl` | Public posting and application links |
| `description` | Full text, only when requested |

Export as **Excel, CSV, JSON or XML**, or pull it through the API.

A board that cannot be found returns one row with an `error` and a hint, so a bad
token never silently vanishes from your results.

---

## Daily new-jobs feed

Schedule it once a day with `postedWithinDays: 1` and you get a clean feed of
roles opened in the last 24 hours across every board you track. Wire it to Slack,
Google Sheets or your CRM through Apify integrations.

---

## Quality notes

- Timestamps are normalised to ISO 8601, so date filters behave predictably.
- Postings that cannot be read are skipped and **reported in the log** rather
  than quietly dropped — and you are not billed for them.
- One malformed posting never takes down the rest of the board.

---

## Limits

- Greenhouse boards only. For a company on a different platform, see the
  multi-platform version of this Actor.
- Boards set to private or password-protected are not accessible.
- `department` and `team` are only as good as what the company fills in.

---

## Support

A board that will not resolve? Open an issue on the **Issues** tab with the
careers URL.
