# Workday Jobs Scraper - Every Opening From Any Careers Site

**Paste any Workday careers URL. It returns every live opening.**

![Sample output table](https://raw.githubusercontent.com/udaninn/career-page-job-monitor/main/docs/output-sample.svg)

Most Fortune 500 careers sites run on Workday, and unlike Greenhouse or
Lever, their API addresses cannot be guessed: the tenant, the wd-number
shard and the site name are all set per company. This Actor reads all three
out of whatever URL you paste - the careers home page, a search page, or a
single job posting link.

No proxy. No browser. No API key.

---

## Input

```json
{
  "careersUrls": [
    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
  ],
  "searchText": "machine learning",
  "titleKeywords": ["engineer"],
  "postedWithinDays": 7
}
```

Any link from the company's Workday site works - if you can open it in a
browser, this Actor can decompose it. A compact `tenant/wd5/SiteName` triple
works too.

`searchText` is passed to Workday's own server-side search (the same box on
the careers page), so filtering happens before fetching and you are billed
only for what matches.

---

## Output

One row per opening, in the same normalised schema as the sibling ATS
Actors - so Workday rows join cleanly against Greenhouse, Lever, Ashby,
Workable, Recruitee and SmartRecruiters rows on the same columns.

| Field | Notes |
|---|---|
| `companyName`, `boardToken`, `ats` | Who, and which board it was read from |
| `title`, `location`, `isRemote` | From the listing |
| `publishedAt` | Real date derived from Workday's posted-on text |
| `postedOnText` | The original display string, kept for auditing |
| `jobUrl`, `applyUrl` | The public posting link |
| `globalId` | `workday:{tenant/shard/site}:{jobId}` - stable join key |
| `description`, `employmentType` | Only with `includeDescription` |

Export as Excel, CSV, JSON or XML, or pull it through the API.

---

## Three traps this Actor absorbs for you

These are verified behaviours of the live endpoint, not documentation
quotes - the endpoint is not documented anywhere.

**1. Asking for more than 20 jobs returns zero.** The endpoint pages at
exactly 20 records. `limit: 21` answers HTTP 200 with an empty list and
`total` still filled in - indistinguishable from a company that is not
hiring. This Actor always pages at 20 and steps `offset` until `total`.

**2. The posting date is a sentence, in your language.** `postedOn` is a
localized display string ("Posted Today", "Posted 3 Days Ago"). Every
request this Actor makes pins `Accept-Language: en-US` and derives a real
date from the English strings. Past 30 days, Workday only says
"30+ Days Ago" - turn on `includeDescription` to get each job's true start
date from the detail record instead.

**3. The listing is thin.** Description, employment type and the real
posting date live one request deeper, per job. `includeDescription` fetches
them - one extra request per job, priced accordingly by your own choice.

---

### Monitoring: only what changed

Set **`onlyNewSinceLastRun: true`** and schedule it. The first run records a
baseline and returns everything. After that, every run returns only roles
that **opened** (`isNew: true`) or **closed** (`isClosed: true`) since the
last one. A morning with no hiring activity returns nothing and costs only
the start fee.

Changing the URLs or filters starts a fresh baseline - otherwise every job
you stopped asking about would be reported as newly closed, which is a wrong
answer rather than a noisy one.

---

## Limits

- This is the API behind the public careers page: read-only,
  unauthenticated, and **not documented as a public contract**. Workday can
  reshape it without notice; this Actor tolerates missing fields and reports
  boards it cannot read as `notFound` rows rather than dropping them.
- The list response has no department or salary. Both may appear in the
  detail record (`includeDescription`), when the company fills them in.
- Some tenants run heavily customized sites; the mainstream layout is what
  this Actor reads.
- It pages politely - one POST per 20 jobs, bounded concurrency.

---

## How this works, in full

The endpoint, the URL decomposition and all three traps are written up here,
with live examples:

**[Workday job boards have a JSON API too. It's just better hidden.](https://dev.to/udaninn/workday-job-boards-have-a-json-api-too-its-just-better-hidden-23fl)**

For one company you can build this yourself - the article gives you the
code. This Actor earns its keep at the point where that stops being true.

---

## Support

A tenant that will not resolve? Open an issue on the **Issues** tab with the
careers URL.
