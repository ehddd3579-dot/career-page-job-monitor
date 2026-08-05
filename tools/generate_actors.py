#!/usr/bin/env python3
"""Generate one single-platform Actor per ATS from the canonical adapters.

There is exactly one copy of the adapter logic in this repository: src/ats.py.
Every generated Actor gets that file verbatim. Fix a bug once, re-run this
script, redeploy - no copy can silently drift from the others.

    python3 tools/generate_actors.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL_ATS = ROOT / "src" / "ats.py"
OUT = ROOT / "actors"

PLATFORMS = {
    "lever": {
        "actor_name": "lever-jobs-scraper",
        "brand": "Lever",
        "title": "Lever Jobs Scraper - Every Opening From Any Lever Board",
        "short": ("Pull live job openings straight from any Lever careers board via the "
                  "official public API. Give it company slugs, get every role with title, "
                  "team, location, commitment and apply link. No proxy, no API key."),
        "token_hint": "the company slug in jobs.lever.co/<company>",
        "examples": ["leverdemo", "https://jobs.lever.co/netflix"],
        "fields_note": ("Lever exposes team, commitment and workplace type, so `team`, "
                        "`employmentType` and `workplaceType` are usually populated."),
    },
    "recruitee": {
        "actor_name": "recruitee-jobs-scraper",
        "brand": "Recruitee",
        "title": "Recruitee Jobs Scraper - Every Opening From Any Recruitee Site",
        "short": ("Pull live job openings straight from any Recruitee careers site via the "
                  "official public API. Give it company subdomains, get every role with "
                  "title, department, location and apply link. No proxy, no API key."),
        "token_hint": "the subdomain in <company>.recruitee.com",
        "examples": ["ibexa", "https://enseradesign.recruitee.com"],
        "fields_note": ("Recruitee exposes department and a structured employment type code, "
                        "so `department` and `employmentType` are usually populated."),
    },
    "ashby": {
        "actor_name": "ashby-jobs-scraper",
        "brand": "Ashby",
        "title": "Ashby Jobs Scraper - Every Opening From Any Ashby Board",
        "short": ("Pull live job openings straight from any Ashby job board via the official "
                  "public API, including published salary ranges. Give it company slugs, get "
                  "every role with team, location and apply link. No proxy, no API key."),
        "token_hint": "the company slug in jobs.ashbyhq.com/<company>",
        "examples": ["ramp", "https://jobs.ashbyhq.com/1x"],
        "fields_note": ("Ashby is the richest of the platforms: department, team, employment "
                        "type, workplace type and **published salary ranges** all come through."),
    },
    "workable": {
        "actor_name": "workable-jobs-scraper",
        "brand": "Workable",
        "title": "Workable Jobs Scraper - Every Opening From Any Workable Board",
        "short": ("Pull live job openings straight from any Workable careers page via the "
                  "official public API. Give it company slugs, get every role with title, "
                  "department, location and apply link. No proxy, no API key."),
        "token_hint": "the company slug in apply.workable.com/<company>",
        "examples": ["huggingface", "https://apply.workable.com/loop/"],
        "fields_note": ("Workable exposes department, employment type and a remote flag, so "
                        "`department`, `employmentType` and `isRemote` are usually populated."),
    },
    "smartrecruiters": {
        "actor_name": "smartrecruiters-jobs-scraper",
        "brand": "SmartRecruiters",
        "title": "SmartRecruiters Jobs Scraper - Every Opening From Any Company",
        "short": ("Pull live job openings straight from any SmartRecruiters careers site via "
                  "the official public API, paging through every posting. Give it company "
                  "identifiers, get every role with department and apply link. No proxy, no API key."),
        "token_hint": "the identifier in careers.smartrecruiters.com/<Company> (case sensitive)",
        "examples": ["Visa", "Bosch"],
        "fields_note": ("SmartRecruiters pages its API at 100 postings per request. This Actor "
                        "walks every page, so large enterprise boards come back complete, not truncated."),
    },
    "greenhouse": {
        "actor_name": "greenhouse-jobs-scraper",
        "brand": "Greenhouse",
        "title": "Greenhouse Jobs Scraper - Every Opening From Any Greenhouse Board",
        "short": ("Pull live job openings straight from any Greenhouse job board via the "
                  "official public API, with departments joined in. Give it company slugs, get "
                  "every role with title, department, location and apply link. No proxy, no API key."),
        "token_hint": "the company slug in job-boards.greenhouse.io/<company>",
        "examples": ["stripe", "https://job-boards.greenhouse.io/anthropic"],
        "fields_note": ("Greenhouse omits departments from its jobs endpoint. This Actor also "
                        "reads the departments endpoint and joins them in, so `department` is "
                        "populated where most Greenhouse scrapers leave it empty."),
    },
}

TOKEN_REGEX = {
    "lever": r"jobs\.lever\.co/([\w.-]+)",
    "recruitee": r"([\w-]+)\.recruitee\.com",
    "ashby": r"jobs\.ashbyhq\.com/([\w.-]+)",
    "workable": r"apply\.workable\.com/([\w.-]+)",
    "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com/([\w.-]+)",
    "greenhouse": r"(?:job-boards|boards)\.greenhouse\.io/([\w.-]+)",
}

DOCKERFILE = '''FROM apify/actor-python:3.12

COPY requirements.txt ./

RUN echo "Python version:" \\
 && python --version \\
 && echo "Installing dependencies:" \\
 && pip install --no-cache-dir -r requirements.txt \\
 && echo "All installed Python packages:" \\
 && pip freeze

COPY . ./

RUN python -c "import src.main" \\
 && echo "Compilation check passed."

CMD ["python3", "-m", "src"]
'''

DATASET_FIELDS = [
    ("companyName", "Company"), ("title", "Job title"), ("department", "Department"),
    ("team", "Team"), ("employmentType", "Type"), ("location", "Location"),
    ("isRemote", "Remote"), ("workplaceType", "Workplace"), ("salary", "Salary"),
    ("publishedAt", "Posted"), ("jobUrl", "Link"),
]


def input_schema(meta):
    return {
        "title": meta["title"], "type": "object", "schemaVersion": 1,
        "properties": {
            "boards": {"title": meta["brand"] + " boards", "type": "array",
                       "description": "Board tokens or full careers URLs. The token is " + meta["token_hint"] + ".",
                       "editor": "stringList", "prefill": meta["examples"], "example": meta["examples"]},
            "titleKeywords": {"title": "Title keywords", "type": "array",
                              "description": "Keep only jobs whose title contains any of these.", "editor": "stringList"},
            "excludeKeywords": {"title": "Exclude keywords", "type": "array",
                                "description": "Drop jobs whose title or department matches any of these.", "editor": "stringList"},
            "locations": {"title": "Locations", "type": "array",
                          "description": "Keep only jobs whose location matches any of these.", "editor": "stringList"},
            "departments": {"title": "Departments or teams", "type": "array",
                            "description": "Keep only jobs in a matching department or team.", "editor": "stringList"},
            "remoteOnly": {"title": "Remote only", "type": "boolean",
                           "description": "Return only remote positions.", "default": False},
            "postedWithinDays": {"title": "Posted within days", "type": "integer",
                                 "description": "Only jobs posted in the last N days. 0 means no limit. Set 1 for a daily new-jobs feed.",
                                 "default": 0, "minimum": 0},
            "includeDescription": {"title": "Include full description", "type": "boolean",
                                   "description": "Add the full job description text. Makes results much larger.", "default": False},
            "maxJobsPerBoard": {"title": "Max jobs per board", "type": "integer",
                                "description": "Cap results per board. 0 means no limit.", "default": 0, "minimum": 0},
            "concurrency": {"title": "Concurrency", "type": "integer",
                            "description": "How many boards to fetch in parallel.", "default": 5, "minimum": 1, "maximum": 20},
        },
        "required": ["boards"],
    }


def dataset_schema(meta):
    props = {f: {"label": l} for f, l in DATASET_FIELDS}
    props.update({
        "jobUrl": {"label": "Link", "format": "link"},
        "applyUrl": {"label": "Apply", "format": "link"},
        "isRemote": {"label": "Remote", "format": "boolean"},
        "publishedAt": {"label": "Posted", "format": "date"},
        "updatedAt": {"label": "Updated", "format": "date"},
    })
    return {"actorSpecification": 1, "fields": {}, "views": {"overview": {
        "title": meta["brand"] + " job openings",
        "transformation": {"fields": [f for f, _ in DATASET_FIELDS] +
                           ["applyUrl", "jobId", "ats", "boardToken", "updatedAt"]},
        "display": {"component": "table", "properties": props}}}}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(ats, meta, main_tpl, readme_tpl):
    base = OUT / ats
    if base.exists():
        shutil.rmtree(base)
    write(base / "src" / "__init__.py", "")
    write(base / "src" / "__main__.py", "import asyncio\n\nfrom .main import main\n\nasyncio.run(main())\n")
    shutil.copy2(CANONICAL_ATS, base / "src" / "ats.py")
    write(base / "src" / "main.py", main_tpl.format(
        ats=ats, brand=meta["brand"], token_regex=TOKEN_REGEX[ats], token_hint=meta["token_hint"]))
    write(base / ".actor" / "Dockerfile", DOCKERFILE)
    write(base / ".actor" / "actor.json", json.dumps({
        "actorSpecification": 1, "name": meta["actor_name"], "title": meta["title"],
        "description": meta["short"], "version": "0.0", "buildTag": "latest",
        "meta": {"templateId": "python-empty"}, "dockerfile": "./Dockerfile",
        "input": "./input_schema.json", "storages": {"dataset": "./dataset_schema.json"},
    }, indent=2) + "\n")
    write(base / ".actor" / "input_schema.json", json.dumps(input_schema(meta), separators=(",", ":")) + "\n")
    write(base / ".actor" / "dataset_schema.json", json.dumps(dataset_schema(meta), separators=(",", ":")) + "\n")
    write(base / "requirements.txt", "apify>=2.0.0\nhttpx>=0.27.0\n")
    write(base / "README.md", readme_tpl.format(
        title=meta["title"], short=meta["short"], brand=meta["brand"],
        token_hint=meta["token_hint"], fields_note=meta["fields_note"],
        examples_json=json.dumps(meta["examples"])))
    return base


if __name__ == "__main__":
    tpl_dir = Path(__file__).resolve().parent
    main_tpl = (tpl_dir / "_main_template.py.txt").read_text(encoding="utf-8")
    readme_tpl = (tpl_dir / "_readme_template.md.txt").read_text(encoding="utf-8")
    if not CANONICAL_ATS.exists():
        raise SystemExit("canonical adapters not found: " + str(CANONICAL_ATS))
    OUT.mkdir(exist_ok=True)
    for ats, meta in PLATFORMS.items():
        base = build(ats, meta, main_tpl, readme_tpl)
        n = sum(1 for p in base.rglob("*") if p.is_file())
        print("  %-16s -> %s  (%d files)" % (ats, base.relative_to(ROOT), n))
    print("\n%d Actors generated from one copy of src/ats.py" % len(PLATFORMS))
