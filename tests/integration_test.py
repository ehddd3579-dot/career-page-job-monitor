# -*- coding: utf-8 -*-
"""Career Page Job Monitor - offline integration test against real ATS payloads."""
import sys, json, types, asyncio, importlib
import httpx

ACTOR = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.path.insert(0, ACTOR)

# ---------------- stub the Apify SDK ----------------
pushed, charges = [], []
class _Log:
    def info(self,*a,**k): pass
    def warning(self,*a,**k): pass
    def debug(self,*a,**k): pass
class _ActorCls:
    log = _Log(); _input = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get_input(self): return self._input
    async def push_data(self, d): pushed.append(d)
    async def charge(self, event_name=None, **k): charges.append(event_name)
    async def set_status_message(self, m): pass
_apify = types.ModuleType("apify"); _apify.Actor = _ActorCls(); sys.modules["apify"] = _apify

# ---------------- serve real captured payloads ----------------
FX = {k: json.load(open(f"tests/fixtures/{k}.json")) for k in
      ("greenhouse","ashby","lever","workable","recruitee","smartrecruiters","gh_departments")}

def handler(req: httpx.Request) -> httpx.Response:
    u = str(req.url)
    if "boards-api.greenhouse.io" in u and "/stripe/" in u:
        return httpx.Response(200, json=FX["gh_departments"] if u.endswith("departments") else FX["greenhouse"])
    if "api.ashbyhq.com" in u and "/ramp" in u:            return httpx.Response(200, json=FX["ashby"])
    if "api.lever.co" in u and "/leverdemo" in u:          return httpx.Response(200, json=FX["lever"])
    if "apply.workable.com" in u and "/huggingface" in u:  return httpx.Response(200, json=FX["workable"])
    if u.startswith("https://demo.recruitee.com/api/offers/"): return httpx.Response(200, json=FX["recruitee"])
    if "api.smartrecruiters.com" in u and "/acme/" in u:   return httpx.Response(200, json=FX["smartrecruiters"])
    return httpx.Response(404, json={"error": "not found"})

_orig_client = httpx.AsyncClient
def _patched(*a, **kw):
    kw["transport"] = httpx.MockTransport(handler)
    return _orig_client(*a, **kw)
httpx.AsyncClient = _patched

import src.ats as A
import src.main as M

BOARDS = [("greenhouse","stripe"),("ashby","ramp"),("lever","leverdemo"),
          ("workable","huggingface"),("recruitee","demo"),("smartrecruiters","acme")]
fails = []
def check(label, ok, detail=""):
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")
    if not ok: fails.append(label)

async def main():
    async with _orig_client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        print("=" * 70); print("1) 6개 어댑터 정규화")
        results = {}
        for name, tok in BOARDS:
            jobs = await A.ADAPTERS[name](c, tok, True)
            results[name] = jobs
            j = jobs[0]
            print(f"\n[{name}] {len(jobs)}건 | {j['companyName']}")
            print(f"   title={j['title']!r}")
            print(f"   dept={j['department']!r} type={j['employmentType']!r} loc={j['location']!r} remote={j['isRemote']}")
            print(f"   publishedAt={j['publishedAt']!r}")
            print(f"   jobUrl={j['jobUrl']}")
            check(f"{name}: 필수 필드(title/jobUrl/jobId)",
                  all(x["title"] and x["jobUrl"] and x["jobId"] for x in jobs))
            check(f"{name}: 스키마 키 동일",
                  all(set(x) == set(results['greenhouse'][0]) for x in jobs))

        print("\n" + "=" * 70); print("2) 회귀 방지 - 이번에 고친 버그들")
        gh = results["greenhouse"]
        check("Greenhouse 부서 보강(/departments 조인)",
              any(x["department"] for x in gh),
              f"{sum(1 for x in gh if x['department'])}/{len(gh)}건 부서 확보")
        lv = results["lever"][0]["publishedAt"]
        check("Lever 밀리초 타임스탬프 -> ISO", isinstance(lv,str) and lv.startswith("20") and "T" in lv, lv)
        sr = results["smartrecruiters"][0]["jobUrl"]
        check("SmartRecruiters 사람이 여는 URL(api.* 아님)",
              "jobs.smartrecruiters.com" in sr and "api.smartrecruiters" not in sr, sr)
        check("모든 publishedAt 이 날짜로 파싱됨",
              all(M.parse_dt(x["publishedAt"]) for r in results.values() for x in r if x["publishedAt"]))

        print("\n" + "=" * 70); print("3) 입력 파싱")
        cases = [("stripe",(None,"stripe")),("greenhouse:stripe",("greenhouse","stripe")),
                 ("https://jobs.ashbyhq.com/ramp",("ashby","ramp")),
                 ("https://boards.greenhouse.io/airbnb",("greenhouse","airbnb")),
                 ("https://job-boards.greenhouse.io/anthropic",("greenhouse","anthropic")),
                 ("https://jobs.lever.co/netflix",("lever","netflix")),
                 ("https://apply.workable.com/acme/",("workable","acme")),
                 ("https://demo.recruitee.com/",("recruitee","demo")),
                 ({"ats":"lever","token":"netflix"},("lever","netflix"))]
        for raw, exp in cases:
            check(f"parse_target({raw!r})", M.parse_target(raw) == exp, str(M.parse_target(raw)))

        print("\n" + "=" * 70); print("4) ATS 자동 감지")
        for name, tok in BOARDS:
            jobs, err = await M.fetch_company(c, None, tok, False)
            got = jobs[0]["ats"] if jobs else None
            check(f"{tok} -> {got}", got == name, err or "")

        print("\n" + "=" * 70); print("5) 오류 처리")
        for ats, tok in ((None,"doesnotexist123"),("greenhouse","badtoken")):
            jobs, err = await M.fetch_company(c, ats, tok, False)
            check(f"ats={ats} token={tok}: 빈 결과 + 에러메시지", jobs == [] and bool(err), err)

    print("\n" + "=" * 70); print("6) 필터 (main() 전체 경로)")
    async def run(cfg):
        pushed.clear(); charges.clear()
        _apify.Actor._input = cfg
        await M.main()
        return [p for p in pushed if "error" not in p]

    r = await run({"companies":["stripe","ashby:ramp","huggingface","demo","acme","leverdemo"]})
    check("필터 없음: 6개 보드 전량 수집(6+6+6+3+2+2)", len(r) == 25, f"{len(r)}건")
    check("과금 건수 == 결과 건수", len(charges) == len(r), f"charge {len(charges)}회")
    check("includeDescription=false 면 description 제거", all("description" not in x for x in r))

    r = await run({"companies":["stripe"],"titleKeywords":["Enterprise"]})
    check("titleKeywords: 6건 중 1건만 통과", len(r)==1, f"{len(r)}건 -> {[x['title'] for x in r]}")

    r = await run({"companies":["huggingface"],"titleKeywords":["Engineer"]})
    check("titleKeywords: 3건 중 2건만 통과", len(r)==2, f"{len(r)}건")

    r = await run({"companies":["stripe"],"excludeKeywords":["Japanese"]})
    check("excludeKeywords: 6건 중 2건 제외", len(r)==4, f"{len(r)}건")

    r = await run({"companies":["leverdemo"],"locations":["Amsterdam"]})
    check("locations: 6건 중 1건만 통과", len(r)==1, f"{len(r)}건 -> {[x['location'] for x in r]}")

    r = await run({"companies":["ashby:ramp"],"departments":["Mobile"]})
    check("departments(team까지 매칭): 6건 중 1건", len(r)==1, f"{len(r)}건 -> {[x['team'] for x in r]}")

    r = await run({"companies":["huggingface"],"remoteOnly":True})
    check("remoteOnly", len(r) == 3 and all(x["isRemote"] for x in r), f"{len(r)}건")

    r = await run({"companies":["huggingface"],"postedWithinDays":10})
    check("postedWithinDays=10 (오늘 2026-08-05)", len(r) == 1, f"{len(r)}건 -> {[x['publishedAt'] for x in r]}")

    r = await run({"companies":["stripe"],"maxJobsPerCompany":2})
    check("maxJobsPerCompany=2", len(r) == 2, f"{len(r)}건")

    r = await run({"companies":["stripe","huggingface"],"titleKeywords":["Engineer","Account"],
                   "excludeKeywords":["Japanese"],"remoteOnly":True})
    check("복합 필터(제목+제외+리모트)", len(r)==3, f"{len(r)}건 -> {[x['title'][:30] for x in r]}")

    r = await run({"companies":["stripe"],"remoteOnly":True})
    check("location 문자열에서 remote 추론(\"US-Remote, ...\")",
          len(r)==1 and r[0]["isRemote"], f"{len(r)}건 -> {[x['location'][:20] for x in r]}")

    r = await run({"companies":["huggingface"],"includeDescription":True})
    check("includeDescription=true 면 description 키 존재", all("description" in x for x in r))

    pushed.clear(); _apify.Actor._input = {"companies":["nosuchcompany999"]}
    await M.main()
    check("없는 회사는 error 행으로 남음", len(pushed)==1 and "error" in pushed[0], str(pushed[0].get("error")))

    try:
        _apify.Actor._input = {"companies":[]}
        await M.main(); check("빈 입력은 예외", False)
    except ValueError as e:
        check("빈 입력은 예외", True, str(e)[:50])

    print("\n" + "=" * 70)
    print("전부 통과" if not fails else f"실패 {len(fails)}건: {fails}")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
