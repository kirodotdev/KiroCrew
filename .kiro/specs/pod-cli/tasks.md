# `kirocrew pod` — Implementation tasks

## 1. Package scaffold
- [ ] 1.1 Create `src/kiro_crew/pod/` package.
- [ ] 1.2 `config.py` — `PodConfig` dataclass + `KIROCREW_POD_*` env resolution;
      `home_dir()`, `env_file()`; optional `repo_hint` (`KIROCREW_POD_REPO`) and
      optional `worktrees_root` (`KIROCREW_POD_WORKTREES_ROOT`, unset by default);
      defaults: live port `5476`, base `7810`, prefix `kirocrew-pod`, roots under
      `~/.kirocrew-pods` / `~/.kirocrew/pods`. No `checkout()` — resolution is
      git-native (see 2.0).
- [ ] 1.3 `__init__.py` — re-export `PodConfig, PodError, derive_port,
      resolve_checkout, pod_home, pod_unit`.

## 2. Runtime
- [ ] 2.0 Git-native resolution — `_git_worktrees(ref)` (parse
      `git worktree list --porcelain` → {basename|branch|path → checkout}),
      `resolve_checkout(cfg, name, *, cwd)` (pinned `CHECKOUT=` → git → root
      fallback → teaching `PodError`), `read_env_file`/`write_env_file` (merge),
      `pin_checkout`.
- [ ] 2.1 `runtime.py` — `validate_name`, `derive_port` (shell `cksum`), `pod_unit`,
      `pod_home`, systemctl/journalctl wrappers, `is_active`, `unit_state`,
      `recent_journal`, `active_names`, `health` (GET `/api/health`).
- [ ] 2.2 `mint_token` — read isolated `<home>/.local_secret`, call `/api/token/local`
      with `X-Local-Secret`.
- [ ] 2.3 `sanitized_seed_config` — sensitive-path guard via
      `kiro_crew.security.is_sensitive_path`, force `tunnel.enabled=false`.
- [ ] 2.4 `build_pod_env` — `KIROCREW_HOME/PORT/PROJECT_DIR`, drop `DEVSPACE_ID`,
      scrub `SLACK_*` + non-AWS `*_TOKEN`, keep `AWS_*`.
- [ ] 2.5 `write_pod_config` (HOME `0700`, config `0600`), `cleanup_home`
      (validate + child-of-pod-root check), `boot` (exec `kirocrew gateway --no-crons`).

## 3. Provisioning
- [ ] 3.1 `provision.py` — `has_venv`/`has_dist`/`worktree_exists`, `_find_python("3.12")`.
- [ ] 3.2 `ensure_venv` — `python3.12 -m venv` + `pip install -e .`.
- [ ] 3.3 `build_dist` — `npm run build` in `website/`, stage `website/dist` →
      `src/kiro_crew/static/dist`; progress to stderr.
- [ ] 3.4 `provision` — venv (always) + dist (when build=True).

## 4. systemd unit
- [ ] 4.1 `unit.py` — template with `ExecStart pod _run %i`, `ExecStopPost pod _cleanup %i`,
      MemoryMax/CPUQuota/Restart, `Environment=` block for non-default `KIROCREW_POD_*`,
      `_kirocrew_bin()` resolution (`KIROCREW_POD_BIN` → PATH → `python -m kiro_crew`),
      `install_unit`.

## 5. CLI verbs
- [ ] 5.1 `cli.py` — verb handlers `_up/_down/_ls/_status/_token/_url/_logs/_install/`
      `_provision/_run/_cleanup`, `_wait_healthy` (fast-fail on crash-loop), `_audit`
      via `kiro_crew.sel.sel().log_api_access`, `dispatch(args)`.
- [ ] 5.2 Wire into `src/kiro_crew/cli.py`: `pod` sub-parser + `pod_action` sub-parsers.
- [ ] 5.3 `cli_commands._pod(args)` → `pod.cli.dispatch`; add `elif args.command == "pod"`.

## 6. Docs
- [ ] 6.1 `src/kiro_crew/pod/README.md` — interface, on-ramp, mechanism, config table
      (OSS-native wording).

## 7. Tests
- [ ] 7.1 Port the pod unit tests: name validation, port derivation (pinned + cksum +
      live-port refusal), seed sanitization (sensitive path / bad JSON / tunnel-off),
      `cleanup_home` traversal refusal, env scrub (`SLACK_*`/`*_TOKEN` gone, `AWS_*`
      kept), unit render (env block, bin resolution).
- [ ] 7.2 Run the suite from the worktree `.venv`; lint/typecheck (pinned tools).

## 8. Ship
- [ ] 8.1 Local provenance note (uncommitted, outside repo) — upstream mapping.
- [ ] 8.2 Grep the diff for internal-reference leakage before commit.
- [ ] 8.3 Commit on `feat/pod-cli`; open PR; drive review to zero comments.
