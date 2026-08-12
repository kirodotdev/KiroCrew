#!/bin/sh
# KiroCrew container entrypoint.
#
# Responsibilities (deliberately minimal — real logic lives in the product):
#
#   1. Sync channel credentials out of the process environment.
#      Compose/`docker run -e` deliver SLACK_BOT_TOKEN etc. as container
#      env, which would otherwise sit in the gateway's /proc/<pid>/environ
#      for the container's whole life, readable by any same-UID process
#      (execve-time snapshot — runtime scrubs don't clean it). The
#      entrypoint moves each credential into the product's supported
#      credential file (<config dir>/.env, chmod 600) and unsets it BEFORE
#      exec'ing the gateway, so the gateway's environ snapshot never
#      contains a credential. The gateway loads .env natively; process env
#      would have overridden .env anyway, so last-write-wins semantics are
#      preserved for operators who change a value and restart.
#
#   2. Decide the sandbox posture on first run — by PROBING, not assuming.
#      The agent exec guard is fail-closed: with no sandbox backend and no
#      explicit opt-out it refuses to run agent commands. Whether the inner
#      Linux user-namespace backend works INSIDE a container depends on the
#      runtime's seccomp/AppArmor policy (modern Docker default profiles
#      permit unshare; hardened ones don't). So on a genuine first run:
#        - probe the product's own backend detector;
#        - if a backend works: seed NOTHING — the inner sandbox stays ON
#          and agent subprocesses cannot read gateway-owned files/environ;
#        - only if no backend works: seed
#          agent.sandbox_allow_unsandboxed_exec=true (the container itself
#          remains the OS isolation boundary) and say so loudly, including
#          how to get the inner sandbox back (a seccomp profile permitting
#          user namespaces + delete the key).
#      First-run only: an existing config.json is operator-owned — never
#      rewrite it, only print guidance. "First run" means config.json is
#      absent (an EMPTY mounted volume is still a first run); a legacy
#      ~/.kirocrew home is owned by the product's migration logic and is
#      never pre-empted here.
#
#   3. Print how to mint a dashboard login link. Fresh links are minted via
#      docker exec rather than parsed out of logs: the gateway's own startup
#      link (printed once at boot, same as every platform) expires minutes
#      after minting, so anything found in `docker logs` later is dead.
#
# Contract: `docker run <image>` starts the gateway; any explicit args are
# passed through to the kirocrew CLI instead (e.g. `docker run <image>
# doctor`, `docker exec <ctr> kirocrew-entrypoint gateway --slack-only`).

set -eu

# ── Data-home resolution: ask the PRODUCT, never re-implement ────────────
# ensure_data_home() (kiro_crew.config.paths) is the backend's single
# source of truth: KIROCREW_HOME validation (system-dir rejection,
# expanduser), legacy-home migration with its completion marker, and the
# new-home default. A shell mirror of that logic drifted from the backend
# twice in review; delegating closes the entire divergence class — the
# entrypoint writes exactly where the gateway will read, by construction.
# This runs the same lazy migration the gateway would run at boot, just
# earlier in the same container (idempotent, cross-process locked).
# KIROCREW_HOME itself passes through unmodified — the backend applies
# the identical interpretation to it.
if ! ACTIVE_HOME=$(python3 -c 'from kiro_crew.config.paths import ensure_data_home; print(ensure_data_home())' 2>/dev/null) || [ -z "$ACTIVE_HOME" ]; then
    ACTIVE_HOME="$HOME/.kiro/crew"
    echo "[entrypoint] WARNING: could not resolve the data home via kiro_crew" \
         "(broken install?); defaulting to $ACTIVE_HOME." >&2
fi
CONFIG="$ACTIVE_HOME/config.json"
ENV_FILE="$ACTIVE_HOME/.env"

# ── 1. Credential sync: env -> .env file, then scrub ─────────────────────
# Derive keys from the product's authoritative credential list. The fallback
# keeps a broken installation diagnosable instead of leaving credentials in the
# gateway environment; a regression test pins it to _CREDENTIAL_KEYS so it
# cannot silently drift. Replace-or-append line-wise so operator-added lines
# and comments in .env survive; grep -v (not sed) avoids escaping issues with
# arbitrary secret bytes.
CRED_KEYS_FALLBACK="SLACK_APP_TOKEN SLACK_BOT_TOKEN KIROCREW_OWNER_ID WECOM_BOT_ID WECOM_SECRET TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN WEBEX_BOT_TOKEN MICROSOFT_APP_ID MICROSOFT_APP_PASSWORD MICROSOFT_APP_TENANT_ID WEIXIN_TOKEN"
if ! CRED_KEYS=$(python3 -c 'from kiro_crew.config.loader import _CREDENTIAL_KEYS; print(" ".join(_CREDENTIAL_KEYS))' 2>/dev/null) || [ -z "$CRED_KEYS" ]; then
    CRED_KEYS="$CRED_KEYS_FALLBACK"
    echo "[entrypoint] WARNING: could not load the product credential key list;" \
         "using the built-in fallback." >&2
fi
for KEY in $CRED_KEYS; do
    VAL=$(eval "printf '%s' \"\${$KEY:-}\"")
    if [ -n "$VAL" ]; then
        mkdir -p "$ACTIVE_HOME"
        TMP="$ENV_FILE.tmp.$$"
        if [ -f "$ENV_FILE" ]; then
            # grep exit 1 just means "no line for this key yet" — fine.
            # Exit >=2 is a read/I/O failure: proceeding would replace the
            # whole .env with only this key, destroying every other stored
            # credential. Abort, keep .env untouched, keep the var in the
            # environment (the gateway still reads env directly).
            set +e
            grep -v "^$KEY=" "$ENV_FILE" > "$TMP"
            GREP_RC=$?
            set -e
            if [ "$GREP_RC" -ge 2 ]; then
                rm -f "$TMP"
                echo "[entrypoint] WARNING: could not read $ENV_FILE (grep exit $GREP_RC);" \
                     "leaving it untouched and keeping $KEY in the environment." >&2
                continue
            fi
        else
            : > "$TMP"
        fi
        printf '%s=%s\n' "$KEY" "$VAL" >> "$TMP"
        chmod 600 "$TMP"
        mv "$TMP" "$ENV_FILE"
        unset "$KEY"
        echo "[entrypoint] Stored $KEY in $ENV_FILE and removed it from the environment."
    fi
done

# ── 2. Sandbox posture (first run only) ──────────────────────────────────
# Signal to the gateway's load_credentials() that credentials were
# deliberately scrubbed from the process environ and must NOT be
# re-injected (which would leak into /proc/<pid>/environ).
export _KIROCREW_CREDS_SCRUBBED=1
# By this point the resolver above has already run the product's legacy-home
# migration if one was pending, so $CONFIG is authoritative: present means
# operator-owned state (never rewrite), absent means genuine first run.
if [ -f "$CONFIG" ]; then
    if ! grep -q sandbox_allow_unsandboxed_exec "$CONFIG"; then
        echo "[entrypoint] Note: agent.sandbox_allow_unsandboxed_exec is not set in $CONFIG." \
             "If agent commands are refused (no sandbox backend under this runtime's" \
             "seccomp profile), either permit user namespaces via a custom seccomp" \
             "profile or set the key to true."
    fi
else
    # Genuine first run (no config.json — fresh container OR empty volume).
    # EVERY branch seeds agent.sandbox="auto": the product default "off"
    # BYPASSES enforcement entirely (wrap_argv returns the raw argv before
    # the fail-closed guard — "off" defers to kiro-cli's internal sandbox,
    # which needs the same kernel facilities and cannot help here). Only
    # mode "auto" routes a missing backend into the audited fail-closed
    # refusal, so "auto" is the floor for all three container postures.
    if python3 -c 'from kiro_crew.sandbox import detect_backend; raise SystemExit(0 if detect_backend() != "none" else 1)' 2>/dev/null; then
        mkdir -p "$ACTIVE_HOME"
        printf '%s\n' '{"agent": {"sandbox": "auto"}}' > "$CONFIG"
        echo "[entrypoint] First run: inner sandbox backend available — seeded" \
             "$CONFIG with agent.sandbox=auto so agent subprocesses run" \
             "namespace-isolated from gateway credentials."
    elif [ "${KIROCREW_ALLOW_UNSANDBOXED:-}" = "1" ]; then
        # EXPLICIT operator consent (-e KIROCREW_ALLOW_UNSANDBOXED=1): the
        # opt-out rides WITH mode=auto so the allowance flows through the
        # guard's audited opt-in path (SEL-logged, governance floors apply)
        # instead of mode-off's silent bypass. The container remains the OS
        # isolation boundary, but agent subprocesses share the container
        # user and can read gateway-owned files.
        mkdir -p "$ACTIVE_HOME"
        printf '%s\n' '{"agent": {"sandbox": "auto", "sandbox_allow_unsandboxed_exec": true}}' > "$CONFIG"
        echo "[entrypoint] First run: NO inner sandbox backend under this runtime's" \
             "seccomp policy; KIROCREW_ALLOW_UNSANDBOXED=1 given — seeded $CONFIG" \
             "with sandbox=auto + sandbox_allow_unsandboxed_exec=true. Agent" \
             "subprocesses share the container user and can read files owned by" \
             "the gateway."
    else
        # FAIL-CLOSED default: no working backend and no explicit consent.
        # sandbox=auto (without the opt-out) makes the exec guard REFUSE
        # agent commands; the gateway and channel bots run fine.
        mkdir -p "$ACTIVE_HOME"
        printf '%s\n' '{"agent": {"sandbox": "auto"}}' > "$CONFIG"
        echo "[entrypoint] First run: NO inner sandbox backend under this runtime's" \
             "seccomp policy. Seeded $CONFIG with sandbox=auto: agent command" \
             "execution is DISABLED (fail-closed) until you choose one of:" \
             "(a) permit user namespaces (--security-opt seccomp=<profile permitting" \
             "unshare/clone>) and restart to get the inner sandbox, or" \
             "(b) restart with -e KIROCREW_ALLOW_UNSANDBOXED=1 to explicitly accept" \
             "unsandboxed agent execution (the container is then the only isolation" \
             "boundary)."
    fi
fi

if [ "${1:-gateway}" = "gateway" ]; then
    PORT="${KIROCREW_PORT:-5476}"
    echo "[entrypoint] Dashboard login: run  docker exec <container> kirocrew token --ttl 2h"
    echo "[entrypoint] then open the printed link, replacing the host with how you reach"
    echo "[entrypoint] this container (e.g. http://localhost:${PORT}/?token=... with -p ${PORT}:${PORT})."
    echo "[entrypoint] Chat sessions need a logged-in kiro-cli:  docker exec -it <container> kiro-cli login"
fi

if [ $# -eq 0 ]; then
    set -- gateway
fi

# tini is exec'd HERE — after the credential scrub — rather than as the
# Dockerfile ENTRYPOINT wrapper. PID 1's /proc/1/environ is an execve-time
# snapshot; a tini born before the scrub would carry every credential for
# the container's lifetime (caught by the docker-smoke environ scan).
# --no-open: no browser in a container. Applies only to the gateway command.
if [ "$1" = "gateway" ]; then
    shift
    exec tini -- kirocrew gateway --no-open "$@"
fi

exec tini -- kirocrew "$@"
