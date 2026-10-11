# Changelog

## v0.1.1 — 2026-09-21 → 2026-10-11 (self-audit revision, round 2)

Iterated against ALIGNMENT-16 (rules 85-100). Changes:

- **Correctness (round-2 fix):** removed a leftover dead clock-patching line inside `simulate()` (the "advance virtual time" logic now lives in exactly one place); simulation output keys verified by CLI JSON smoke test.
- **Hardening (round-2 additions):** state-file writes made atomic (`tmp + os.replace`) so a crash mid-freeze cannot leave a half-written agent; state-file names sanitized (`agent/../é中文!` round-trip test added); corrupt state files fail loudly instead of silently resetting an agent; constructor validation for `max_resident`/`idle_timeout_s`.
- **Measured claims:** thaw latency now reported per-request (`thaw_ms`) and aggregated in the simulation (avg/max over a 50-request burst after 200 freezes: ~255 ms / ~577 ms on the test machine); README table carries the repro command and hardware context; the "real runtime" extrapolation is explicitly [estimated].
- **README completeness:** added Minimal Fallback (find/exit/cron shell fallback), Design Trade-offs priority order (no-silent-state-loss first), Limits & Second-order Effects (LRU starving quiet-but-critical agents, over-provisioning rebound), FAQ with "When NOT to use" (4 cases), 5 [Core Assumptions], three explicit confidence ratings with rationale.

## v0.1.0 — 2026-09-21 (initial draft)

- `Scheduler`: register / heartbeat / tick-freeze / submit-thaw / evict_lru with injectable monotonic clock.
- Atomic-per-agent JSON state files in a managed `state_dir`.
- `simulate()` CLI: density measurement (N registered -> cap-resident) + burst thaw-latency measurement.
- self_test with functional, error-path, extreme-edge families (19 checks at draft time).
