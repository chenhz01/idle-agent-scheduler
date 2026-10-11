# -*- coding: utf-8 -*-
"""self_test.py for idle-agent-scheduler.

Three test families (ALIGNMENT-16 rule 97):
  A. functional  -- register/idle/freeze/thaw round-trip, payload integrity,
                    LRU eviction, sub-second thaw on 200-agent burst
  B. error paths -- unknown agent, double registration, non-resident
                    heartbeat, corrupt state file, bad constructor args
  C. extreme edge -- empty scheduler tick, 1MB superlong payload, zero
                    timeout starvation, unicode ids, idempotent re-tick

Run:  python self_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import idle_agent_scheduler as ias

PASS = 0
FAIL = 0
RESULTS = []


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append("%-4s %s %s" % ("PASS" if cond else "FAIL", name, info))


def section(t):
    RESULTS.append("")
    RESULTS.append("== %s ==" % t)


tmp = tempfile.mkdtemp(prefix="ias_")


def fresh(**kw):
    kw.setdefault("state_dir", os.path.join(tmp, "s_%d" % time.time_ns()))
    return ias.Scheduler(**kw)


# ---------- A. functional ----------
section("A. functional")
t = {"v": 100.0}
s = fresh(idle_timeout_s=10.0, clock=lambda: t["v"])
s.register("a1", payload={"msgs": [1, 2, 3], "mode": "plan"})
check("register resident", s.resident_count() == 1)

t["v"] += 11.0
frozen = s.tick()
check("silent agent frozen on tick", frozen == ["a1"] and s.frozen_count() == 1
      and s.resident_count() == 0)

rec, ms = s.submit("a1")
check("thaw restores payload", rec["payload"] == {"msgs": [1, 2, 3], "mode": "plan"}
      and rec["state"] == "running")
check("thaw measured", ms >= 0.0 and "thaw_ms" not in rec)

# thaw really read from disk (state file consumed)
check("state file consumed on thaw",
      not os.path.exists(s._state_path("a1")))

# LRU eviction
s2 = fresh(max_resident=2, idle_timeout_s=1e9, clock=lambda: t["v"])
for i, aid in enumerate(["x1", "x2", "x3"]):
    t["v"] += 1
    s2.register(aid, payload={"i": i})
ev = s2.evict_lru()
check("LRU evicts oldest", ev == ["x1"] and s2.resident_count() == 2
      and s2.agents["x1"]["state"] == "frozen")
rec, _ = s2.submit("x1")
check("evicted agent recovers", rec["payload"] == {"i": 0})

# density + sub-second recovery under burst (the headline claim)
sim = ias.simulate(n_agents=200, idle_timeout_s=1.0, burst=50, max_resident=8,
                   quiet=True)
check("200 agents -> <=8 resident after tick", sim["resident_after_tick"] <= 8,
      "resident=%d frozen=%d" % (sim["resident_after_tick"], sim["frozen_on_tick"]))
check("thaw sub-second (avg %.1f ms)" % sim["thaw_ms_avg"], sim["recovery_subsecond"],
      "max=%.1f ms" % sim["thaw_ms_max"])

# CLI smoke
import subprocess
cli = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "idle_agent_scheduler.py")]
p1 = subprocess.run(cli + ["simulate", "--agents", "30"], capture_output=True, text=True)
check("cli simulate exit 0 + json", p1.returncode == 0
      and json.loads(p1.stdout)["agents_registered"] == 30)

# ---------- B. error paths ----------
section("B. error paths")
s3 = fresh(idle_timeout_s=10.0, clock=lambda: t["v"])
s3.register("b1")
try:
    s3.register("b1")
    check("rejects double registration", False, "accepted")
except ias.SchedulerError:
    check("rejects double registration", True)
try:
    s3.submit("ghost")
    check("rejects unknown agent submit", False, "accepted")
except ias.SchedulerError:
    check("rejects unknown agent submit", True)
try:
    s3.heartbeat("ghost")
    check("rejects unknown agent heartbeat", False, "accepted")
except ias.SchedulerError:
    check("rejects unknown agent heartbeat", True)

t["v"] += 50
s3.tick()
try:
    s3.heartbeat("b1")
    check("rejects heartbeat on frozen agent", False, "accepted")
except ias.SchedulerError:
    check("rejects heartbeat on frozen agent", True)

# corrupt state file -> loud failure, not silent reset
s4 = fresh(idle_timeout_s=1.0, clock=lambda: t["v"])
s4.register("c1", payload={"k": 1})
t["v"] += 5
s4.tick()
with open(s4._state_path("c1"), "w", encoding="utf-8") as f:
    f.write("{corrupt json!!")
try:
    s4.submit("c1")
    check("corrupt state file fails loudly", False, "silently thawed")
except (json.JSONDecodeError, ValueError):
    check("corrupt state file fails loudly", True)

for label, kw in [("max_resident 0", {"max_resident": 0}),
                  ("idle_timeout 0", {"idle_timeout_s": 0}),
                  ("negative timeout", {"idle_timeout_s": -5})]:
    try:
        fresh(**kw)
        check("rejects %s" % label, False, "accepted")
    except ValueError:
        check("rejects %s" % label, True)

# ---------- C. extreme edge ----------
section("C. extreme edge")
s5 = fresh(idle_timeout_s=10.0, clock=lambda: t["v"])
check("empty scheduler tick no-op", s5.tick() == [] and s5.stats()["registered"] == 0)

s6 = fresh(idle_timeout_s=10.0, clock=lambda: t["v"])
s6.register("big", payload={"blob": "y" * (1024 * 1024)})
t["v"] += 20
s6.tick()
t0 = time.perf_counter()
rec, ms = s6.submit("big")
dt = time.perf_counter() - t0
check("1MB payload round-trip intact", rec["payload"]["blob"] == "y" * (1024 * 1024))
check("1MB thaw still fast (<1s)", ms < 1000.0, "%.1f ms (wall %.1f ms)" % (ms, dt * 1000))

# zero-idle timeout would freeze everything instantly (starvation semantics,
# documented) -- scheduler must remain functional
s7 = fresh(idle_timeout_s=0.001, clock=lambda: t["v"])
s7.register("z1", payload={})
t["v"] += 0.002
check("tiny timeout freezes", s7.tick() == ["z1"])
rec, _ = s7.submit("z1")
check("...and still recovers", rec["state"] == "running")

# unicode + weird ids survive the state-file name sanitizer
s8 = fresh(idle_timeout_s=1.0, clock=lambda: t["v"])
s8.register("agent/../é中文!", payload={"ok": True})
t["v"] += 5
s8.tick()
rec, _ = s8.submit("agent/../é中文!")
check("unicode/path-ish id round-trip", rec["payload"] == {"ok": True})

# idempotent re-tick: frozen agents stay frozen, nothing breaks
s9 = fresh(idle_timeout_s=1.0, clock=lambda: t["v"])
s9.register("d1", payload={"n": 1})
t["v"] += 5
first = s9.tick()
second = s9.tick()
check("re-tick idempotent", first == ["d1"] and second == [])

# ---------- report ----------
print("\n".join(RESULTS))
print("")
print("idle-agent-scheduler self-test: %d/%d PASS (families A/B/C)" % (PASS, PASS + FAIL))
print("simulate(200 agents, burst 50): thaw avg=%.1f ms max=%.1f ms | repro: python idle_agent_scheduler.py simulate --agents 200 --burst 50"
      % (sim["thaw_ms_avg"], sim["thaw_ms_max"]))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
