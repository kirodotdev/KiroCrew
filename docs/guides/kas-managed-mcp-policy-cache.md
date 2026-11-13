# KAS managed MCPs fail with `Connection closed`

This runbook records how to diagnose and fix KAS managed MCP startup failures
when the server log only reports `Connection closed`. It was verified with the
Kiro Crew v0.12 local Docker setup on 2026-11-11.

## Symptom

Starting a chat may report one or more of:

```text
KAS managed MCP initialization failed: kirocrew-core: failed (Connection closed)
KAS managed MCP initialization failed: kirocrew-dashboard: failed (Connection closed)
```

The failure can move between `kirocrew-core`, `kirocrew-dashboard`, `kirocrew-panel`,
`kirocrew-cron`, and other first-party servers. KAS starts several servers in parallel,
so the first reported name can vary.

## Diagnose from the active container

First confirm which checkout built the running container. This matters when multiple
worktrees exist:

```bash
docker inspect kirocrew-local-kirocrew-1 \
  --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
```

Read the MCP log for the newest KAS run:

```bash
docker exec kirocrew-local-kirocrew-1 sh -lc \
  'run_dir=$(find /home/kirocrew/.kiro/logs -mindepth 1 -maxdepth 1 -type d | sort | tail -1); echo "$run_dir"; rg "mcp.connect.(start|error|ok)|Failed to connect" "$run_dir/mcp.log"'
```

`mcp.log` identifies which subprocesses KAS could not initialize, but usually does not
include the subprocess exception. If it only says `Connection closed`, temporarily add
diagnostics around `boot_platform()` in `kiro_crew.mcp_entry.slim_server()` and around
the `_load_healing(...slim_server...)` call in `kiro_crew._bootstrap.main()`. Write the
exception and traceback to a file under `/home/kirocrew/.kiro/logs/`; do not log policy
contents, URLs, request headers, or credentials. The bootstrap catch is important because
the exception can happen while loading the server, before `server()` is called.

The revealing exception in this case was:

```text
PlatformCompositionError: cache-only policy resolution found no cached ceiling.
```

The MCP environment already had `KIROCREW_POLICY_CACHE_ONLY=1`, a maximum cache age,
and the admission-policy path. Those values were not the problem. A diagnostic from
inside the failing child showed:

```text
cache_dir=/var/lib/kirocrew/policy_cache
dir_exists=True
doc=FileNotFoundError
meta=FileNotFoundError
cache_readable=False
```

An ordinary process in the container could read the same cache. That difference means
the child had a sandboxed filesystem view: the policy-cache directory was present, but
the policy document and metadata were masked from that process.

For a quick comparison in the container, check only cache availability; do not print the
policy body:

```bash
docker exec kirocrew-local-kirocrew-1 python -c \
  'from kiro_crew.platform.policy_distribution_cache import cache_dir, read_cache; print(cache_dir(), read_cache() is not None)'
```

You can also ask the normal container environment to load the MCP server entry point
without starting its stdio loop:

```bash
docker exec kirocrew-local-kirocrew-1 env \
  HOME=/home/kirocrew \
  KIROCREW_HOME=/var/lib/kirocrew \
  KIROCREW_POLICY_CACHE_ONLY=1 \
  KIROCREW_POLICY_MAX_CACHE_AGE_SECS=86400 \
  KIROCREW_ADMISSION_POLICY=/etc/kirocrew/admission-policy.json \
  python -c 'from kiro_crew.mcp_entry import slim_server; print(slim_server(["mcp-core"]) is not None)'
```

If this returns `True` while the KAS child fails, the cache contents and MCP entry point
are valid outside KAS's sandbox; continue by checking the child filesystem view.

## Why the cache must be visible

When the gateway installs its governance ceiling from central distribution, its managed
MCP children receive cache-only policy markers. They intentionally do not receive the
control-plane URL or request headers. Each child independently boots the platform and
verifies the signed ceiling from the shared policy cache. If its sandbox masks the cache,
the child fails closed before the MCP handshake, and KAS reports the resulting process
exit as `Connection closed`.

Kiro Crew's sandbox planner treats any visible `policy_cache` directory specially: when
`extra_visible_dirs` lifts the mask, the planner seals that directory read-only. This is
the required API for granting cache reads without granting cache writes.

## The fix

The sandbox already lifted the cache read-only for an LTS build, through
`enterprise_policy_cache_view` in
[`src/kiro_crew/sandbox_tiers.py`](../../src/kiro_crew/sandbox_tiers.py). The fix
applies that same lift when the gateway's ceiling came from central distribution
(`central_ceiling_installed()`). Every sandboxed spawn then sees the cache read-only,
so the KAS runtime and the MCP children it starts inside its sandbox can read it. The
lift keeps its existing refusals: a cache beneath a foreign mask, a caller mask over the
cache, or older metadata that still names its source stays hidden.

The gateway also derives the cache-only marker and the effective maximum cache age from
its own state, in three places:

| Site | Why |
|---|---|
| `acp/launch.py` | the harness process environment, after the agent env scrub |
| `agent_materialization/managed_mcp.py` | managed MCP declarations, for harnesses that build the child env from the declaration |
| `acp/kas_agents.py`, `mcp_discovery_probe.py` | re-applied after their env filters strip Kiro Crew's reserved names |

The gateway's URL and headers stay scrubbed. Do not solve this failure by disabling
cache-only mode, weakening signature verification, or falling through to a local policy.

## Rebuild and verify

Build from the checkout printed by `docker inspect`, preserving the existing volume
settings. For a local Compose deployment, for example:

```bash
docker compose up -d --build kirocrew
```

Wait for the container to report healthy, then send a short message in a new chat. The
successful run logged `mcp.connect.ok` for all seven managed servers:

```text
kirocrew-dashboard, kirocrew-panel, kirocrew-core, kirocrew-cron,
kirocrew-guide, kirocrew-mate, kirocrew-work
```

The original verification was a user-triggered chat on 2026-11-11, on v0.12 with the
first version of this fix. KAS negotiated stdio MCP protocol `2024-11-05` and connected
all seven servers; no `mcp.connect.error` entries appeared in that run's `mcp.log`.

Temporary diagnostic code in the running image was replaced by the rebuild. Its output
file, `/home/kirocrew/.kiro/logs/kas-child-env.log`, is under the persistent home volume
and may remain from the investigation. It contains launch paths, selected policy-env
flags, and startup tracebacks, not policy contents or policy URLs.

## Replication checklist

1. Inspect the active container's Compose working-directory label; edit that checkout.
2. Find the newest KAS `mcp.log` and record the first failing server names.
3. If logs only say `Connection closed`, capture the Python exception before the process
   exits, including exceptions raised while `slim_server()` is loading.
4. Check cache visibility from inside the failing child, including `policy.json` and
   `policy.meta.json` existence, without printing their contents.
5. Compare that view with `read_cache()` from the ordinary container process.
6. If only the child misses the files, trace the sandbox namespace inherited by KAS.
7. Confirm `enterprise_policy_cache_view()` returns the cache directory on the
   governed host, and that the sandbox planner makes it read-only.
8. Rebuild, restart, send a new chat message, and confirm every expected
   `kirocrew-*` server logs `mcp.connect.ok`.
