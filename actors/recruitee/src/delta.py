"""Tell me what changed since last time, not what exists.

Someone who schedules this daily does not want 500 rows every morning. They
want the three roles that opened overnight and the two that closed. Returning
the full board every run buries the signal and bills them for the burial.

So this keeps a snapshot of which jobs were present on the previous run, in a
named key-value store that survives between runs, and diffs against it.

Two design decisions worth stating plainly:

* **The snapshot is scoped to the exact input.** Change the company list or a
  filter and you get a fresh baseline rather than a flood of phantom "closed"
  rows for jobs you simply stopped asking about. The cost is that tweaking a
  filter re-emits everything once. That is the honest trade: a re-emitted run
  is annoying, a run that reports 400 jobs as closed when they are still open
  is a wrong answer.

* **Closed jobs are reported from the snapshot, not re-fetched.** By the time a
  posting disappears there is nothing left to fetch, so the row carries what
  was recorded when the job was last seen. Fields beyond identity are
  deliberately absent rather than stale.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from apify import Actor

# Named stores persist between runs; the default (unnamed) store is wiped with
# the run that created it, which would make every run look like the first one.
#
# The name has to be per-Actor. Actors run under limited permissions by
# default, and a limited Actor may only touch named storages it created
# itself. Every Actor here originally asked for the same
# "ats-job-monitor-state"; the first one to run created it, and the other five
# then got a ForbiddenError on open. That failure is caught below, so nothing
# crashed - change detection just quietly degraded to "everything is new" on
# every run, which bills the caller for the whole board daily instead of the
# handful of roles that actually moved. Silent and expensive is the worst
# combination, so the store is namespaced and the fallback is explicit.
STORE_PREFIX = "ats-job-monitor-state"


def store_name() -> str:
    """A store name unique to this Actor, stable across its runs."""
    try:
        actor_id = (Actor.get_env() or {}).get("actor_id")
    except Exception:  # noqa: BLE001 - env is absent outside the platform
        actor_id = None
    return f"{STORE_PREFIX}-{actor_id}" if actor_id else STORE_PREFIX

# Kept per remembered job. Enough to make a "this closed" row actionable
# without turning the snapshot into a second copy of the dataset.
_REMEMBERED = ("companyName", "boardToken", "ats", "jobId", "title", "jobUrl")

# A key-value store record has a size limit, and a snapshot far past this is a
# sign someone is tracking the whole market rather than a company list - the
# case this feature is not for.
MAX_REMEMBERED = 50_000


def global_id(item: dict) -> str:
    """`{ats}:{token}:{jobId}` - stable across runs and unique across platforms.

    Job ids are only unique within one board, so an id alone collides the
    moment you track two companies. Callers use this to join, dedupe and diff
    without inventing a key of their own.
    """
    return "%s:%s:%s" % (
        item.get("ats") or "",
        item.get("boardToken") or "",
        item.get("jobId") or "",
    )


def scope_key(config: Any) -> str:
    """A stable store key for one input shape.

    Hashed rather than spelled out because key-value store keys have a limited
    character set and a company list does not fit in one.
    """
    blob = json.dumps(config, sort_keys=True, default=str)
    return "snapshot-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class DeltaTracker:
    """Diffs this run against the previous one for the same input.

    Disabled by default: `enabled=False` makes every method a no-op that keeps
    the full result set, so the normal path costs nothing.
    """

    def __init__(self, enabled: bool, config: Any) -> None:
        self.enabled = bool(enabled)
        self.key = scope_key(config)
        self.previous: dict[str, dict] = {}
        self.current: dict[str, dict] = {}
        self.is_baseline = False
        self._store = None

    async def load(self) -> None:
        if not self.enabled:
            return
        try:
            self._store = await Actor.open_key_value_store(name=store_name())
            saved = await self._store.get_value(self.key)
        except Exception as exc:  # noqa: BLE001
            # A storage problem must not cost the caller their run. Falling
            # back to "everything is new" returns more than they asked for,
            # which is recoverable; crashing is not.
            Actor.log.warning(
                "Could not read the previous snapshot (%s). This run returns "
                "the whole board and bills for it, instead of only what "
                "changed. If this repeats every run, change detection is not "
                "working and the schedule should be paused."
                % type(exc).__name__
            )
            saved = None

        if isinstance(saved, dict) and isinstance(saved.get("jobs"), dict):
            self.previous = saved["jobs"]
            Actor.log.info(
                "Comparing against %d job(s) seen on %s"
                % (len(self.previous), saved.get("updatedAt", "the previous run"))
            )
        else:
            self.is_baseline = True
            Actor.log.info(
                "No previous snapshot for this input - this run records the "
                "baseline and returns everything. Later runs return only "
                "changes."
            )

    def see(self, item: dict) -> bool:
        """Record a live job. Returns True if the caller should emit it.

        Called for every job that survived filtering, whether or not delta mode
        is on, so the snapshot always reflects what the caller actually asked
        for rather than the raw board.
        """
        gid = global_id(item)
        item["globalId"] = gid

        if not self.enabled:
            return True

        if len(self.current) < MAX_REMEMBERED:
            self.current[gid] = {k: item.get(k) for k in _REMEMBERED}

        if gid in self.previous:
            return False          # unchanged since last run - nothing to say

        item["isNew"] = True
        return True

    def closed(self) -> list[dict]:
        """Jobs present last run and absent now.

        Only meaningful once a baseline exists; on the first run every job is
        new and nothing can have closed.
        """
        if not self.enabled or self.is_baseline:
            return []
        rows = []
        for gid, remembered in self.previous.items():
            if gid in self.current:
                continue
            row = dict(remembered)
            row["recordType"] = "job"
            row["globalId"] = gid
            row["isClosed"] = True
            rows.append(row)
        return rows

    async def save(self) -> None:
        if not self.enabled or self._store is None:
            return
        try:
            await self._store.set_value(
                self.key,
                {
                    "jobs": self.current,
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            # The run's data is already delivered. Losing the snapshot only
            # means the next run re-baselines, so warn rather than fail.
            Actor.log.warning(
                "Could not save the snapshot (%s). The next run will start a "
                "fresh baseline." % type(exc).__name__
            )




