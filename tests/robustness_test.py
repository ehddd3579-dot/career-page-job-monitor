# -*- coding: utf-8 -*-
"""Robustness suite: malformed payloads, network failures, and scale.

Real ATS responses drift. A missing field, a changed type, or one dead company
out of two hundred must never take down a run. Everything here is offline.
"""
import os
import sys
import types
import asyncio
import time

import httpx

ACTOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ACTOR)

pushed, charges = [], []


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass


class _A:
    log = _Log()
    _input = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get_input(self): return self._input
    async def push_data(self, d): pushed.append(d)
    async def charge(self, event_name=None, **k): charges.append(event_name)
    async def set_status_message(self, m): pass


_ap = types.ModuleType("apify")
_ap.Actor = _A()
sys.modules["apify"] = _ap

fails = []


def check(label, ok, detail=""):
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + str(detail)) if detail else ''}")
    if not ok:
        fails.append(label)


import src.ats as A  # noqa: E402
import src.main as M  # noqa: E402

SCHEMA = {
    "ats", "boardToken", "companyName", "jobId", "title", "department", "team",
    "employmentType", "location", "isRemote", "workplaceType", "salary",
    "publishedAt", "updatedAt", "jobUrl", "applyUrl", "description",
}

NULLKEYS = (
    "id", "title", "location", "content", "absolute_url", "first_published",
    "updated_at", "company_name", "department", "team", "employmentType",
    "isListed", "isRemote", "workplaceType", "jobUrl", "applyUrl",
    "descriptionPlain", "publishedAt", "shortcode", "employment_type",
    "telecommuting", "published_on", "url", "application_url", "city", "state",
    "country", "careers_url", "careers_apply_url", "published_at",
    "employment_type_code", "remote", "name", "releasedDate", "typeOfEmployment",
    "ref", "text", "categories", "createdAt", "hostedUrl",
)

NASTY = {
    "빈 객체": {},
    "jobs 키 없음": {"foo": "bar"},
    "jobs 가 null": {"jobs": None, "offers": None, "content": None},
    "빈 리스트": {"jobs": [], "offers": [], "content": []},
    "필드 전부 null": {
        "jobs": [{k: None for k in NULLKEYS}], "offers": [{}], "content": [{}],
    },
    "타입 뒤바뀜": {
        "jobs": [{"id": {"a": 1}, "title": ["리스트"], "location": "dict기대인데문자열",
                  "company_name": 123, "department": [], "isListed": "yes",
                  "isRemote": "maybe", "publishedAt": {}, "jobUrl": 999,
                  "employmentType": 3.14, "offices": "리스트아님"}],
        "offers": [{"id": [1], "title": {}, "locations": "리스트아님", "city": 0,
                    "remote": "true"}],
        "content": [{"id": None, "name": [], "location": "dict아님",
                     "company": "dict아님", "department": [], "typeOfEmployment": 42}],
    },
    "제어문자·유니코드": {
        "jobs": [{"id": "1", "title": "Engineer 테스트", "location": {"name": "서울\t\n"},
                  "absolute_url": "https://x/1",
                  "content": "<p>a&amp;b</p><script>bad()</script>"}],
        "offers": [{"id": 1, "title": "디자이너 ", "careers_url": "https://x/2"}],
        "content": [{"id": "3", "name": "매니저\r\n", "location": {"city": "서울"},
                     "company": {"name": "회사"}}],
    },
    "초대형 값": {
        "jobs": [{"id": "1", "title": "A" * 50000, "absolute_url": "https://x/1",
                  "location": {"name": "B" * 10000},
                  "content": "<p>" + "C" * 100000 + "</p>"}],
        "offers": [{"id": 1, "title": "D" * 50000, "careers_url": "https://x/2"}],
        "content": [{"id": "3", "name": "E" * 50000, "location": {"city": "F" * 9999}}],
    },
    "말도 안 되는 날짜": {
        "jobs": [{"id": "1", "title": "T", "absolute_url": "https://x/1",
                  "first_published": "내일", "updated_at": -99999999999999}],
        "offers": [{"id": 1, "title": "T", "careers_url": "https://x/2",
                    "published_at": "0000-00-00"}],
        "content": [{"id": "3", "name": "T", "releasedDate": 99999999999999999999}],
    },
}

LEVER_NASTY = {
    "빈 리스트": [], "dict 반환": {"a": 1}, "null 항목": [None], "필드없음": [{}],
    "타입뒤바뀜": [{"id": [], "text": {}, "categories": "dict아님"}],
}


def make(payload):
    return lambda req: httpx.Response(200, json=payload)


async def run_all():
    print("=" * 70)
    print("1) 파손된 응답 - 크래시 없이 스키마 유지하는가")
    for label, payload in NASTY.items():
        for name in ("greenhouse", "ashby", "workable", "recruitee", "smartrecruiters"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(make(payload))) as c:
                try:
                    jobs = await A.ADAPTERS[name](c, "tok", True)
                except Exception as e:
                    check(f"{label} / {name}", False, f"{type(e).__name__}: {e}")
                    continue
                ok = isinstance(jobs, list) and all(
                    isinstance(j, dict) and set(j) == SCHEMA
                    and isinstance(j["jobId"], str) and isinstance(j["isRemote"], bool)
                    for j in jobs
                )
                check(f"{label} / {name}", ok, f"{len(jobs)}건")
    for label, payload in LEVER_NASTY.items():
        async with httpx.AsyncClient(transport=httpx.MockTransport(make(payload))) as c:
            try:
                jobs = await A.ADAPTERS["lever"](c, "tok", True)
                check(f"{label} / lever",
                      isinstance(jobs, list) and all(set(j) == SCHEMA for j in jobs),
                      f"{len(jobs)}건")
            except Exception as e:
                check(f"{label} / lever", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    print("2) 네트워크 장애 유형")

    def boom(kind):
        def h(req):
            if kind == "500":
                return httpx.Response(500, text="server error")
            if kind == "429":
                return httpx.Response(429, text="rate limited")
            if kind == "html":
                return httpx.Response(200, text="<html>로그인하세요</html>")
            if kind == "truncated":
                return httpx.Response(200, text='{"jobs":[{"id":1,')
            if kind == "timeout":
                raise httpx.ConnectTimeout("timed out")
            if kind == "dns":
                raise httpx.ConnectError("name resolution failed")
            return httpx.Response(404)
        return h

    for kind in ("500", "429", "html", "truncated", "timeout", "dns"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom(kind))) as c:
            try:
                jobs, err = await M.fetch_company(c, None, "any", False)
                check(f"{kind}: 예외 없이 에러 반환", jobs == [] and bool(err), err)
            except Exception as e:
                check(f"{kind}: 예외 없이 에러 반환", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    print("3) 200개 회사 중 20곳 고장 - 나머지가 끝까지 도는가")
    GOOD = {"jobs": [{"id": i, "title": f"Engineer {i}",
                      "absolute_url": f"https://x/{i}",
                      "location": {"name": "Remote"}} for i in range(3)]}
    fl = {"now": 0, "max": 0}

    async def h2(req):
        fl["now"] += 1
        fl["max"] = max(fl["max"], fl["now"])
        await asyncio.sleep(0.005)
        u = str(req.url)
        fl["now"] -= 1
        if "departments" in u:
            return httpx.Response(200, json={"departments": []})
        if "bad" in u:
            return httpx.Response(500)
        if "slow" in u:
            raise httpx.ConnectTimeout("t")
        if "junk" in u:
            return httpx.Response(200, text="not json")
        if "boards-api.greenhouse.io" in u:
            return httpx.Response(200, json=GOOD)
        return httpx.Response(404)

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **kw: orig(
        *a, **{**kw, "transport": httpx.MockTransport(h2)})
    comps = ([f"ok{i}" for i in range(180)] + [f"bad{i}" for i in range(10)]
             + [f"slow{i}" for i in range(5)] + [f"junk{i}" for i in range(5)])
    pushed.clear()
    charges.clear()
    _ap.Actor._input = {"companies": comps, "concurrency": 5}
    t0 = time.time()
    try:
        await M.main()
        crashed = None
    except Exception as e:
        crashed = f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    httpx.AsyncClient = orig
    jobs = [p for p in pushed if "error" not in p]
    errs = [p for p in pushed if "error" in p]
    check("고장난 회사가 있어도 실행 완주", crashed is None, crashed or "")
    check("정상 180개사 x 3건 = 540건", len(jobs) == 540, f"{len(jobs)}건")
    check("고장 20개사는 error 행", len(errs) == 20, f"{len(errs)}건")
    check("과금은 정상 결과에만", len(charges) == len(jobs), f"charge {len(charges)}")
    check("concurrency=5 준수", fl["max"] <= 5, f"최대 동시 {fl['max']}")
    print(f"      (200개사 {dt:.1f}초)")

    print("\n" + "=" * 70)
    print("4) 헬퍼 경계값")
    for v, none_exp in ((None, True), ("", True), ("abc", False), (0, False),
                        (-1, False), (1e20, True), ("1553186035299", False),
                        (1553186035, False)):
        r = A.iso_time(v)
        check(f"iso_time({v!r})", (r is None) == none_exp, repr(r))
    check("iso_time 초/밀리초 같은 날짜",
          A.iso_time(1553186035)[:10] == A.iso_time(1553186035299)[:10])
    for v in (None, "", "<p></p>", "<script>x</script>", "a&amp;b&nbsp;c",
              "<b>중첩<i>태그</i></b>"):
        try:
            check(f"html_to_text({v!r})", True, repr(A.html_to_text(v))[:40])
        except Exception as e:
            check(f"html_to_text({v!r})", False, str(e))
    for raw in ("", "   ", "///", "@@@", "http://", "ftp://x/y", "a:b:c", None,
                123, [], {}):
        try:
            r = M.parse_target(raw)
            check(f"parse_target({raw!r})",
                  isinstance(r, tuple) and len(r) == 2, str(r))
        except Exception as e:
            check(f"parse_target({raw!r})", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    print("전부 통과" if not fails else f"실패 {len(fails)}건:\n  - " + "\n  - ".join(fails))
    return 1 if fails else 0


sys.exit(asyncio.run(run_all()))
