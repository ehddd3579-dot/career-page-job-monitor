# SmartRecruiters Jobs Scraper - Every Opening From Any Company

**Pull live job openings straight from any SmartRecruiters careers site via the official public API, paging through every posting. Give it company identifiers, get every role with department and apply link. No proxy, no API key.**

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
| `maxJobsPerBoard` | `0` | `0` = no limit. |
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

- SmartRecruiters boards only. For a company on a different platform, see the
  multi-platform version of this Actor.
- Boards set to private or password-protected are not accessible.
- `department` and `team` are only as good as what the company fills in.

---

## Support

A board that will not resolve? Open an issue on the **Issues** tab with the
careers URL.
