"""What Dev Fleet asks of the host, as data for the core's ``apps.builtin_host_grants``.

A data module by contract: the core reads it at import in the MCP servers and the
config loader, so it imports nothing. Each field's meaning is documented on
``kiro_crew.apps.builtin_host_grants.HostGrants``; the reasons below are Dev Fleet's.
"""

HOST_GRANTS = {
    "app": "dev-fleet",
    # The backend is the ANCESTOR of the vite/pip builds a sync or pod-up runs.
    "build_rlimit": True,
    # The backend reads live-target pointer state through an in-gateway route
    # (``gateway_routes.py``), because the pointer itself is masked from its namespace.
    # ``pod/runtime.py`` scrubs this variable from spawns that must not aim at the live
    # gateway.
    "bound_port": True,
    # Operator-declared main-checkout override: the highest-priority repo discovery hint
    # in ``repository.py``, ahead of ``KIROCREW_PROJECT_DIR`` — which packaged installs
    # point at the app bundle (no .git). A path, not a secret.
    "backend_env_names": ("KIROCREW_DEVFLEET_REPO",),
    # Operator-declared trusted-binary overrides (unit-file owned): ``runtime.py``
    # resolves credential-bearing tools through these instead of the inherited PATH.
    "backend_env_prefixes": ("KIROCREW_DEVFLEET_BIN_",),
    # ``dev_fleet.repo_path`` names the checkout the app manages; read by
    # ``repository.py::_load_dev_fleet_cfg`` and prescribed by the dashboard's
    # "no checkout found" banner.
    "config_top_keys": ("dev_fleet",),
    # The agent surface behind the ``pod_up`` / ``pod_restart`` / ``pod_down`` /
    # ``pod_status`` / ``pod_ls`` MCP tools (``agent_pod_api.py``). An agent session runs
    # behind a sandbox with its own user namespace, so its shells cannot connect the
    # systemd user bus every pod verb needs; the gateway holds the host bus and does the
    # systemd part on the agent's behalf. Without these entries the tools 403: an agent
    # has no dashboard cookie, the gateway mints its internal secret in-process and never
    # puts it in an agent's env, and ``.local_secret`` is on the sensitive-path denylist.
    #
    # STRICT, not mixed: no browser calls these. The dashboard's own pod buttons go to
    # the app backend through the ``/apps/dev-fleet/api/*`` reverse proxy, which is a
    # different surface with cookie auth. Each handler re-asserts loopback AND
    # ``internal_auth`` itself, because a ``local_only=False`` deployment reclassifies
    # strict paths as mixed.
    #
    # FIVE EXACT paths, never the ``/api/apps/dev-fleet/pod`` prefix. The match is
    # ``path == p or path.startswith(p + "/")``, so a prefix entry would silently admit
    # every future route under that segment — and this app's neighbourhood includes
    # worktree PRUNE and the Make Live cutover, which must never become reachable by
    # holding the internal secret.
    "strict_internal_paths": (
        "/api/apps/dev-fleet/pod/up",
        "/api/apps/dev-fleet/pod/restart",
        "/api/apps/dev-fleet/pod/down",
        "/api/apps/dev-fleet/pod/status",
        "/api/apps/dev-fleet/pod/list",
    ),
    # Mint and return a pod's dashboard token, through the gateway's own route and
    # through the app's backend. No relayed client may reach either.
    "relay_credential_paths": (
        "/api/apps/dev-fleet/pod-token",
        "/apps/dev-fleet/api/pod/token",
    ),
}
