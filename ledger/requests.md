# Cross-lane change requests (append-only entries; status updated in place by the Orchestrator)

Format: `[UTC timestamp] REQ-n | from lane → to lane | request | status (OPEN/ROUTED/DONE/DECLINED) | resolution`

- [2026-09-02T23:05:43Z] REQ-1 | ORCH → all lanes | Deferred lint debt: ruff reports ~124 style findings under mizan/ (93 E501 over the 110-char limit, 30 UP037 redundant quoted annotations, 6 UP035, 3 UP017, 1 I001). Not correctness. Each lane must run `python -m ruff check` on ITS OWN paths and fix its own findings before its merge; the Orchestrator sweeps mizan/contracts at the next checkpoint. | OPEN | Raised after the Sprint 1 checkpoint.
