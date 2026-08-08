"""Delta mode has to be right or it is worse than not existing.

If it wrongly reports a job as closed, someone stops chasing a live lead. If it
wrongly suppresses a new job, they never see it at all - and unlike a crash,
neither failure announces itself. So the diffing logic is tested directly, with
a fake store standing in for Apify's.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_logged: list[str] = []
if "apify" not in sys.modules:
    stub = types.ModuleType("apify")

    class _FakeStore:
        """Stands in for a named key-value store, including its persistence."""
        shelves: dict = {}
        owners: dict = {}

        def __init__(self, name):
            self.data = _FakeStore.shelves.setdefault(name, {})

        async def get_value(self, key):
            return self.data.get(key)

        async def set_value(self, key, value):
            self.data[key] = value

    class _Actor:
        log = types.SimpleNamespace(
            info=lambda m: _logged.append(m),
            warning=lambda m: _logged.append(m),
            debug=lambda m: None,
        )
        fail_open = False
        # Shaped like the real `Actor.get_env()`, which keys the Actor id
        # under "id" - see the store-name section at the bottom.
        env = {"id": "ACTOR_A"}
        opened: list = []

        @staticmethod
        def get_env():
            return _Actor.env

        @staticmethod
        async def open_key_value_store(name=None):
            _Actor.opened.append(name)
            # The platform refuses a named store an Actor did not create.
            # Model that: a store first opened under one actor_id is off
            # limits to another.
            owner = _FakeStore.owners.setdefault(name, _Actor.env.get("id"))
            if owner != _Actor.env.get("id"):
                raise PermissionError("ForbiddenError")
            if _Actor.fail_open:
                raise RuntimeError("storage unavailable")
            return _FakeStore(name)

    stub.Actor = _Actor
    sys.modules["apify"] = stub

sys.path.insert(0, str(ROOT))
from src.delta import DeltaTracker, global_id, scope_key  # noqa: E402

FakeStore = sys.modules["apify"].Actor  # noqa: N816
Store = sys.modules["apify"].__dict__["Actor"]

passed, failed = 0, 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print("   PASS  %s" % label)
    else:
        failed += 1
        print("   FAIL  %s\n         got  %r\n         want %r" % (label, got, want))


def job(ats, token, jid, title="Engineer"):
    return {"ats": ats, "boardToken": token, "jobId": jid, "title": title,
            "companyName": token, "jobUrl": "https://x/%s" % jid}


async def run_cycle(config, jobs, enabled=True):
    """One full run: load, see every job, collect closed rows, save."""
    d = DeltaTracker(enabled=enabled, config=config)
    await d.load()
    emitted = [j for j in jobs if d.see(j)]
    closed = d.closed()
    await d.save()
    return emitted, closed, d


async def main():
    cfg = {"targets": ["greenhouse:stripe"]}

    # --- identity ---------------------------------------------------------
    check("globalId is ats:token:jobId",
          global_id(job("lever", "shieldai", "42")), "lever:shieldai:42")
    check("globalId survives missing pieces without raising",
          global_id({"ats": "lever"}), "lever::")
    check("same job on two boards gets two ids",
          global_id(job("greenhouse", "a", "1")) != global_id(job("greenhouse", "b", "1")),
          True)

    # --- disabled is a true no-op ----------------------------------------
    emitted, closed, d = await run_cycle(cfg, [job("greenhouse", "stripe", "1")], enabled=False)
    check("disabled: everything emitted", len(emitted), 1)
    check("disabled: nothing reported closed", closed, [])
    check("disabled: no isNew flag added", "isNew" in emitted[0], False)
    check("disabled: globalId still added", emitted[0]["globalId"], "greenhouse:stripe:1")

    # --- first run is a baseline -----------------------------------------
    jobs1 = [job("greenhouse", "stripe", "1"), job("greenhouse", "stripe", "2")]
    emitted, closed, d = await run_cycle(cfg, jobs1)
    check("baseline: returns everything", len(emitted), 2)
    check("baseline: marks them new", all(j.get("isNew") for j in emitted), True)
    check("baseline: nothing can have closed", closed, [])
    check("baseline: flagged as baseline", d.is_baseline, True)

    # --- second run, nothing changed -------------------------------------
    jobs2 = [job("greenhouse", "stripe", "1"), job("greenhouse", "stripe", "2")]
    emitted, closed, d = await run_cycle(cfg, jobs2)
    check("no change: emits nothing", emitted, [])
    check("no change: closes nothing", closed, [])
    check("no change: not a baseline", d.is_baseline, False)

    # --- one opens, one closes -------------------------------------------
    jobs3 = [job("greenhouse", "stripe", "2"), job("greenhouse", "stripe", "3")]
    emitted, closed, d = await run_cycle(cfg, jobs3)
    check("change: only the new job is emitted",
          [j["jobId"] for j in emitted], ["3"])
    check("change: the vanished job is reported closed",
          [r["jobId"] for r in closed], ["1"])
    check("change: closed row carries the link", closed[0]["jobUrl"], "https://x/1")
    check("change: closed row is flagged", closed[0]["isClosed"], True)
    check("change: closed row has the join key", closed[0]["globalId"], "greenhouse:stripe:1")

    # --- a reopened job counts as new again ------------------------------
    jobs4 = [job("greenhouse", "stripe", "1"), job("greenhouse", "stripe", "2"),
             job("greenhouse", "stripe", "3")]
    emitted, closed, d = await run_cycle(cfg, jobs4)
    check("reopened job is emitted as new", [j["jobId"] for j in emitted], ["1"])
    check("reopened run closes nothing", closed, [])

    # --- a different input must not disturb the first --------------------
    other = {"targets": ["greenhouse:ramp"]}
    emitted, closed, d = await run_cycle(other, [job("greenhouse", "ramp", "9")])
    check("new input starts its own baseline", d.is_baseline, True)
    check("new input does not close the other input's jobs", closed, [])

    emitted, closed, d = await run_cycle(cfg, jobs4)
    check("original input still remembers its snapshot", emitted, [])
    check("original input still reports nothing closed", closed, [])

    # --- changing a filter re-baselines rather than lying ----------------
    tweaked = dict(cfg, remoteOnly=True)
    emitted, closed, d = await run_cycle(tweaked, [job("greenhouse", "stripe", "2")])
    check("filter change starts a fresh baseline", d.is_baseline, True)
    check("filter change does not report 2 jobs as closed", closed, [])
    check("scope key changes with the filter",
          scope_key(cfg) != scope_key(tweaked), True)
    check("scope key ignores key order",
          scope_key({"a": 1, "b": 2}), scope_key({"b": 2, "a": 1}))

    # --- storage failure degrades, never crashes -------------------------
    Store.fail_open = True
    _logged.clear()
    emitted, closed, d = await run_cycle(cfg, jobs4)
    Store.fail_open = False
    check("storage failure still returns the jobs", len(emitted), 3)
    check("storage failure reports nothing closed", closed, [])
    check("storage failure is warned about",
          any("Could not read" in m for m in _logged), True)
    check("the warning says what it will cost",
          any("bills for it" in m for m in _logged), True)

    # --- the snapshot is bounded -----------------------------------------
    import src.delta as delta_mod
    original, delta_mod.MAX_REMEMBERED = delta_mod.MAX_REMEMBERED, 2
    big = {"targets": ["greenhouse:big"]}
    _, _, d = await run_cycle(big, [job("greenhouse", "big", str(i)) for i in range(10)])
    check("snapshot stops growing at the cap", len(d.current), 2)
    delta_mod.MAX_REMEMBERED = original

    # --- one store per Actor ---------------------------------------------
    # Every Actor asked for the same store name at first. Actors run under
    # limited permissions and may only touch storages they created, so the
    # first Actor to run owned it and the rest got ForbiddenError on open.
    # Nothing crashed - the except above swallowed it - and change detection
    # silently degraded to "everything is new" on every run, billing the
    # caller for the whole board daily. Caught only by reading a live log.
    # The first attempt at this fix passed its own test and still did nothing
    # in production, because the test fed the fake env the same wrong key the
    # code read - `actor_id`. `Actor.get_env()` is keyed by option name, so the
    # Actor id is under `id`; `APIFY_ACTOR_ID` is the variable it came from.
    # Agreeing with itself is not evidence, so the fake env below is shaped
    # like a real one (verified against apify 3.4.1) and the wrong key is
    # tested explicitly.
    from src.delta import STORE_PREFIX, store_name

    Store.env = {"id": "HxYLsL6iMYguw8v4h"}
    name_a = store_name()
    Store.env = {"id": "VwSROT18HEXHL9wS9"}
    name_b = store_name()
    check("an Actor id under the real SDK key is used",
          name_a != STORE_PREFIX, True)
    check("two Actors get two store names", name_a != name_b, True)
    check("the same Actor gets the same name twice", store_name(), name_b)

    # Store names are lower case; real Actor ids are not. Appending the id
    # verbatim would have produced an invalid name even once the key was right.
    check("the name is lower case", name_b, name_b.lower())
    check("the raw Actor id is not in the name", "VwSROT" in name_b, False)

    Store.env = {}
    check("no actor id still yields a usable name", store_name(), STORE_PREFIX)

    # Prove it end to end: Actor B must be able to keep its own baseline
    # even though Actor A already created a store.
    shared = {"targets": ["greenhouse:acme"]}
    Store.env = {"id": "ACTOR_A"}
    _logged.clear()
    _, _, da = await run_cycle(shared, [job("greenhouse", "acme", "1")])
    Store.env = {"id": "ACTOR_B"}
    _, _, db = await run_cycle(shared, [job("greenhouse", "acme", "1")])
    check("Actor B is not locked out by Actor A",
          any("Could not read" in m for m in _logged), False)
    emitted, _, _ = await run_cycle(shared, [job("greenhouse", "acme", "1")])
    check("Actor B's own second run sees its baseline", emitted, [])
    Store.env = {"id": "ACTOR_A"}
    emitted, _, _ = await run_cycle(shared, [job("greenhouse", "acme", "1")])
    check("Actor A's snapshot survived alongside it", emitted, [])

    print("\n" + "=" * 70)
    print("전부 통과" if not failed else "%d개 실패" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
