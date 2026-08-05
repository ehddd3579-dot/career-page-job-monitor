# -*- coding: utf-8 -*-
"""Run every generated single-platform Actor against the real captured payloads."""
import os, sys, json, types, asyncio, importlib
import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = os.path.join(ROOT, "tests", "fixtures")

pushed, charges = [], []
class _Log:
    def info(self,*a,**k): pass
    def warning(self,*a,**k): pass
    def debug(self,*a,**k): pass
class _A:
    log=_Log(); _input={}
    async def __aenter__(self): return self
    async def __aexit__(self,*a): return False
    async def get_input(self): return self._input
    async def push_data(self,d): pushed.append(d)
    async def charge(self,event_name=None,**k): charges.append(event_name)
    async def set_status_message(self,m): pass
_ap=types.ModuleType("apify"); _ap.Actor=_A(); sys.modules["apify"]=_ap

fx = {k: json.load(open(os.path.join(FX, k+".json"))) for k in
      ("greenhouse","ashby","lever","workable","recruitee","smartrecruiters","gh_departments")}

# board token -> which fixture, per platform
CASES = {
  "greenhouse":      ("stripe",       6,  "https://stripe.com/jobs"),
  "ashby":           ("ramp",         6,  "https://jobs.ashbyhq.com/"),
  "lever":           ("leverdemo",    6,  "https://jobs.lever.co/"),
  "workable":        ("huggingface",  3,  "https://apply.workable.com/"),
  "recruitee":       ("demo",         2,  "https://demo.recruitee.com/"),
  "smartrecruiters": ("acme",         2,  "https://jobs.smartrecruiters.com/"),
}
# URL forms that must resolve to the same token
URLCASES = {
  "greenhouse": "https://job-boards.greenhouse.io/stripe",
  "ashby": "https://jobs.ashbyhq.com/ramp",
  "lever": "https://jobs.lever.co/leverdemo",
  "workable": "https://apply.workable.com/huggingface/",
  "recruitee": "https://demo.recruitee.com/",
  "smartrecruiters": "https://careers.smartrecruiters.com/acme",
}

def handler(req):
    u=str(req.url)
    if "boards-api.greenhouse.io" in u and "/stripe/" in u:
        return httpx.Response(200, json=fx["gh_departments"] if u.endswith("departments") else fx["greenhouse"])
    if "api.ashbyhq.com" in u and "/ramp" in u:           return httpx.Response(200, json=fx["ashby"])
    if "api.lever.co" in u and "/leverdemo" in u:         return httpx.Response(200, json=fx["lever"])
    if "apply.workable.com" in u and "/huggingface" in u: return httpx.Response(200, json=fx["workable"])
    if u.startswith("https://demo.recruitee.com/api/offers/"): return httpx.Response(200, json=fx["recruitee"])
    if "api.smartrecruiters.com" in u and "/acme/" in u:  return httpx.Response(200, json=fx["smartrecruiters"])
    return httpx.Response(404, json={"error":"not found"})

orig = httpx.AsyncClient
httpx.AsyncClient = lambda *a, **kw: orig(*a, **{**kw, "transport": httpx.MockTransport(handler)})

fails=[]
def check(label, ok, detail=""):
    print(f"   {'PASS' if ok else 'FAIL'}  {label}{('  -> '+str(detail)) if detail else ''}")
    if not ok: fails.append(label)

def load(ats):
    for m in [k for k in sys.modules if k=="src" or k.startswith("src.")]:
        del sys.modules[m]
    p = os.path.join(ROOT, "actors", ats)
    sys.path.insert(0, p)
    try:
        return importlib.import_module("src.main")
    finally:
        sys.path.remove(p)

async def run():
    print("="*70); print("생성된 6개 액터 - 실제 ATS 응답으로 검증\n")
    for ats,(token, expected, url_prefix) in CASES.items():
        M = load(ats)
        # 1) token parsing, plain + URL form
        check(f"[{ats}] 토큰 파싱 (plain)", M.parse_board(token)==token, M.parse_board(token))
        check(f"[{ats}] 토큰 파싱 (URL)", M.parse_board(URLCASES[ats])==token, M.parse_board(URLCASES[ats]))
        check(f"[{ats}] ATS 고정값", M.ATS==ats, M.ATS)
        # 2) full run
        pushed.clear(); charges.clear()
        _ap.Actor._input={"boards":[URLCASES[ats]]}
        await M.main()
        rows=[p for p in pushed if "error" not in p]
        check(f"[{ats}] 수집 {expected}건", len(rows)==expected, f"{len(rows)}건")
        check(f"[{ats}] 과금=결과", len(charges)==len(rows), f"charge {len(charges)}")
        if rows:
            r=rows[0]
            check(f"[{ats}] ats 필드", r["ats"]==ats, r["ats"])
            check(f"[{ats}] 링크 형식", str(r["jobUrl"]).startswith(url_prefix), str(r["jobUrl"])[:52])
            check(f"[{ats}] 날짜 ISO", r["publishedAt"] is None or M.parse_dt(r["publishedAt"]) is not None, r["publishedAt"])
        # 3) filter still works
        pushed.clear(); _ap.Actor._input={"boards":[token],"maxJobsPerBoard":1}
        await M.main()
        check(f"[{ats}] maxJobsPerBoard=1", len([p for p in pushed if "error" not in p])==1)
        # 4) bad token -> error row, no crash
        pushed.clear(); _ap.Actor._input={"boards":["nope-does-not-exist"]}
        await M.main()
        errs=[p for p in pushed if "error" in p]
        check(f"[{ats}] 잘못된 토큰은 error 행", len(errs)==1, errs[0].get("error") if errs else "")
        # 5) empty input raises
        try:
            _ap.Actor._input={"boards":[]}; await M.main(); check(f"[{ats}] 빈 입력 예외", False)
        except ValueError: check(f"[{ats}] 빈 입력 예외", True)
        print()
    print("="*70)
    print("전부 통과" if not fails else f"실패 {len(fails)}건:\n  - " + "\n  - ".join(fails))
    return 1 if fails else 0

sys.exit(asyncio.run(run()))
