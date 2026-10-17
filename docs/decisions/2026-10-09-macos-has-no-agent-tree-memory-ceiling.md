# macOS has no agent-tree memory ceiling; it stays best-effort

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-10-09

## Decision

On macOS the agent spawn tree gets no memory ceiling, with `resource_limits.max_memory_mb` set or unset; macOS stays best-effort, and no macOS ceiling is built.

## Why

- Linux bounds the tree with a cgroup v2 scope (`memory.max`, 65% of host RAM by default) and Windows with a Job object (`JobMemoryLimit`, same default). macOS has no equivalent of either: no cgroup, no Job object, and no per-subtree memory budget in any public interface.
- The candidates that do exist are not ceilings. macOS accepts `RLIMIT_AS` without enforcing it, and it caps virtual memory, which Node/V8 (kiro-cli and every npm MCP server) overshoots by design. Jetsam is per-process, not per-subtree, and its control interface is private and entitlement-gated. A userspace sampler that kills over a threshold is a reaper racing the growth, not a ceiling.
- The reporter asked for the gap to be either armed or written down. Writing it down is the honest state: a reader of the design doc should see plainly that macOS has no memory ceiling, rather than piece it together from a startup warning.

What a user on macOS can do:

- Run the gateway in a Linux VM or container, where the cgroup scope and slice ceilings apply.
- Keep `resource_limits.xdist_auto_cap` at its default (`-1`), so pytest-xdist `-n auto` inside an agent turn sizes itself to available memory; pass an explicit `-n N` for large suites.
- Set `session.watchdog_rss_max_mb` to recycle idle sessions that grew large. It never stops a session mid-turn, so it is not a ceiling.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/10060 -- the report, the triage that confirms the gap is macOS-only, and the reporter's vote to record that macOS gets no memory ceiling.
- `docs/architecture/resource-protection.md` -- the `macOS memory (none)` mechanism row and the "macOS: a reaper, not a ceiling" section.
- https://github.com/kirodotdev/KiroCrew/pull/18533 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.
