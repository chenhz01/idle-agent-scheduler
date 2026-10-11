# -*- coding: utf-8 -*-
"""
idle-agent-scheduler -- Single-machine idle-reuse scheduler for agent sandboxes.

Core idea (from the K8s-scale substrate line of work, miniaturized to one
box): treat *agent idle time* as a schedulable resource instead of giving
every agent a permanent resident slot. When an agent goes silent past the
idle timeout, freeze it -- serialize its state to disk and release the slot.
When a request arrives, thaw it back in (target: sub-second on a local SSD).

Pure stdlib. MIT. v0.1.1
"""
import argparse
import json
import os
import time


class SchedulerError(Exception):
    pass


class Scheduler:
    """Time is injectable (`clock`) for deterministic tests.
    Frozen agents live as one JSON file each in `state_dir`."""

    def __init__(self, max_resident=8, idle_timeout_s=300.0, state_dir=None,
                 clock=time.monotonic):
        if max_resident < 1:
            raise ValueError("max_resident must be >= 1")
        if idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s must be > 0")
        self.max_resident = int(max_resident)
        self.idle_timeout_s = float(idle_timeout_s)
        self.state_dir = state_dir or os.path.join(os.getcwd(), ".agent_states")
        os.makedirs(self.state_dir, exist_ok=True)
        self.clock = clock
        self.agents = {}   # id -> {"payload": dict, "last_seen": t, "state": str, "frozen_at": t|None}

    # -- lifecycle -------------------------------------------------------
    def register(self, agent_id, payload=None):
        if agent_id in self.agents and self.agents[agent_id]["state"] != "frozen":
            raise SchedulerError("agent %r already resident" % agent_id)
        rec = {"payload": payload if payload is not None else {},
               "last_seen": self.clock(), "state": "running", "frozen_at": None}
        self.agents[agent_id] = rec
        return rec

    def heartbeat(self, agent_id):
        rec = self._get_resident(agent_id)
        rec["last_seen"] = self.clock()
        rec["state"] = "running"

    def tick(self, now=None):
        """Freeze every resident agent silent longer than idle_timeout_s.
        Returns list of frozen agent ids."""
        now = self.clock() if now is None else now
        frozen = []
        for aid, rec in self.agents.items():
            if rec["state"] == "running" and now - rec["last_seen"] > self.idle_timeout_s:
                self._freeze(aid, rec, now)
                frozen.append(aid)
        return frozen

    def _freeze(self, aid, rec, now):
        path = self._state_path(aid)
        tmp = path + ".tmp"
        blob = json.dumps({"payload": rec["payload"], "last_seen": rec["last_seen"]},
                          ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, path)  # atomic on POSIX & Windows
        rec["state"] = "frozen"
        rec["frozen_at"] = now
        rec["payload"] = None

    def _thaw(self, aid):
        path = self._state_path(aid)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(path)
        rec = self.agents[aid]
        rec["payload"] = data["payload"]
        rec["last_seen"] = self.clock()
        rec["state"] = "running"
        rec["frozen_at"] = None
        return rec

    # -- request path ----------------------------------------------------
    def submit(self, agent_id, force_wake=False):
        """Wake an agent for a request. Thaws from disk if frozen.
        Returns (record, thaw_ms). Raises SchedulerError if unknown."""
        if agent_id not in self.agents:
            raise SchedulerError("unknown agent %r" % agent_id)
        rec = self.agents[agent_id]
        thaw_ms = 0.0
        if rec["state"] == "frozen":
            t0 = self.clock()
            self._thaw(agent_id)
            thaw_ms = (self.clock() - t0) * 1000.0
        elif rec["state"] == "running" and not force_wake:
            pass  # already warm
        return rec, thaw_ms

    def evict_lru(self):
        """If over max_resident, freeze the least-recently-active running
        agents until back under the cap. Returns evicted ids."""
        evicted = []
        while self.resident_count() > self.max_resident:
            candidates = [(r["last_seen"], aid) for aid, r in self.agents.items()
                          if r["state"] == "running"]
            if not candidates:
                break
            _, victim = min(candidates)
            self._freeze(victim, self.agents[victim], self.clock())
            evicted.append(victim)
        return evicted

    # -- introspection ----------------------------------------------------
    def _get_resident(self, agent_id):
        rec = self.agents.get(agent_id)
        if rec is None or rec["state"] == "frozen":
            raise SchedulerError("agent %r is not resident" % agent_id)
        return rec

    def _state_path(self, agent_id):
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in agent_id)
        return os.path.join(self.state_dir, "%s.state.json" % safe)

    def resident_count(self):
        return sum(1 for r in self.agents.values() if r["state"] == "running")

    def frozen_count(self):
        return sum(1 for r in self.agents.values() if r["state"] == "frozen")

    def stats(self):
        return {"registered": len(self.agents),
                "resident": self.resident_count(),
                "frozen": self.frozen_count(),
                "state_dir": self.state_dir}


# ---------------- simulation CLI ----------------

def simulate(n_agents=60, idle_timeout_s=1.0, burst=5, max_resident=8, quiet=False):
    """Simulate: register N agents, let them go idle, tick (freeze), then a
    burst of requests measures thaw latency. Pure-local, deterministic."""
    s = Scheduler(max_resident=max_resident, idle_timeout_s=idle_timeout_s)
    for i in range(n_agents):
        s.register("agent-%03d" % i, payload={"ctx": "x" * 200, "step": i})
    # advance virtual time so every agent is past the idle timeout
    holder = {"offset": idle_timeout_s + 0.5}
    base = s.clock
    s.clock = lambda: base() + holder["offset"]
    frozen = s.tick()
    thaw_ms = []
    for i in range(burst):
        aid = "agent-%03d" % i
        rec, ms = s.submit(aid)
        thaw_ms.append(ms)
        rec["payload"]["step"] += 1
    # double-freeze / idempotency probe
    s.tick()
    result = {
        "agents_registered": n_agents,
        "frozen_on_tick": len(frozen),
        "resident_after_tick": s.resident_count(),
        "burst_requests": burst,
        "thaw_ms_avg": round(sum(thaw_ms) / len(thaw_ms), 3) if thaw_ms else 0.0,
        "thaw_ms_max": round(max(thaw_ms), 3) if thaw_ms else 0.0,
        "recovery_subsecond": all(ms < 1000.0 for ms in thaw_ms),
        "stats": s.stats(),
    }
    if not quiet:
        print(json.dumps(result, indent=2))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="idle-agent-scheduler",
        description="Single-machine idle-reuse scheduler: freeze silent agents, thaw sub-second on request.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sim = sub.add_parser("simulate", help="freeze/thaw simulation (~1 s)")
    p_sim.add_argument("--agents", type=int, default=60)
    p_sim.add_argument("--idle-timeout", type=float, default=1.0)
    p_sim.add_argument("--burst", type=int, default=5)
    p_sim.add_argument("--max-resident", type=int, default=8)

    sub.add_parser("demo", help="run simulate with defaults (~1 s)")

    args = ap.parse_args(argv)
    if args.cmd == "demo":
        simulate()
        return 0
    if args.cmd == "simulate":
        simulate(n_agents=args.agents, idle_timeout_s=args.idle_timeout,
                 burst=args.burst, max_resident=args.max_resident)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
