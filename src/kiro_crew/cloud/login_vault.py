"""The on-box half of the cloud sign-in when Crew's own vault owns the credential.

:mod:`kiro_crew.cloud.login` signs the remote crew in by running shell scripts on
the instance over SSM. Which PROGRAM those scripts should drive depends on the
instance's own configuration: on the KAS bundle route (``agent.acp_backend ==
"kas"`` with ``agent.kas_bundle`` on) Crew spawns its own engine, kiro-cli is not
installed, and the engine's only credential is Crew's vault; everywhere else the
remote ``kiro-cli`` owns the sign-in exactly as it always has.

The local ``kirocrew cloud`` command cannot read that configuration -- it lives in
the instance's ``config.json`` -- so the decision is made ON THE BOX, by the box's
own ``kirocrew kas route``, inside every script that needs it. This module holds
that prelude and the vault-route lines the scripts splice in; the kiro-cli lines
they keep are :mod:`kiro_crew.cloud.login`'s and are not repeated here. The two
halves share the capture loop, the private login directory and the sentinels, so
the local Python reads one set of outputs whichever program produced them.

Every vault-route verb is a ``kirocrew kas`` subcommand (:mod:`kiro_crew.cli_kas_login`)
that speaks ``kiro-cli``'s vocabulary on purpose: ``login`` prints the same
verification URL + ``code:`` lines the capture loop greps for, ``whoami --format
json`` prints the document :func:`kiro_crew.cloud.login_target.parse_whoami_output`
already parses, and each exits 0 / non-zero where ``kiro-cli`` does.

Nothing here reads, prints or carries a credential; the device code and the token
stay between the box's ``kirocrew`` process and the issuer.
"""

from __future__ import annotations

import shlex

# The one spelling of the route answer, owned by the verb that prints it; the
# scripts compare against the same object, so there is nothing to drift.
from kiro_crew.cli_kas_login import ROUTE_KAS_BUNDLE

__all__ = ["ROUTE_KAS_BUNDLE"]

#: Echoed by the device-login script when it took the vault branch, so the local
#: :func:`kiro_crew.cloud.login.start_device_login` knows the box has no kiro-cli
#: to fall back to (the social callback flow is kiro-cli's) and reports the vault
#: sign-in's own outcome instead.
VAULT_ROUTE_SENTINEL = "__KIROCREW_VAULT_ROUTE__"

#: Echoed (and the command ended) by the callback-login script on a vault-route box:
#: there is no ``kiro-cli login`` callback flow to continue there.
VAULT_NO_CALLBACK_SENTINEL = "__KIROCREW_VAULT_NO_CALLBACK__"

#: Echoed (and the logout script ended non-zero) when the box's ``kirocrew kas
#: logout`` exited non-zero: the slots may be gone, but the sign-out's cross-process
#: fence (the sign-out generation) was not written, so a login pending in the
#: box's gateway could still land. The local side must not report "signed out".
VAULT_LOGOUT_FAILED_SENTINEL = "__KIROCREW_VAULT_LOGOUT_FAILED__"

#: The command-line fragment that identifies a vault sign-in this code started, for
#: the same uid-scoped ``pkill -f`` the kiro-cli pattern is used with. ``$KIROCREW``
#: resolves to a path whose basename is ``kirocrew`` (see :data:`KIROCREW_BIN_RESOLVE`),
#: so the launched command line contains this fragment whatever flags follow. It
#: does not match ``kirocrew kas logout``, ``kirocrew kas whoami`` or the gateway.
VAULT_LOGIN_PROCESS_PATTERN = "kirocrew kas login"

#: Resolve the box's ``kirocrew`` to an absolute path, PATH-independent, for the same
#: reason ``login._KIRO_BIN_RESOLVE`` does it for kiro-cli: under SSM's login-shell
#: wrapper PATH is sometimes unreliable. The EC2 template installs it under
#: ``$HOME/.local/bin`` (``install.sh``) and runs the gateway from there.
# Shell text that runs on a Linux instance; the candidate paths are the box's, not
# this process's, which is why no pathlib call shapes them.
KIROCREW_BIN_RESOLVE = (
    'KIROCREW="$(command -v kirocrew 2>/dev/null || true)"; '
    '[ -n "$KIROCREW" ] || for c in "$HOME/.local/bin/kirocrew" /usr/local/bin/kirocrew'
    ' /usr/bin/kirocrew; do [ -x "$c" ] && KIROCREW="$c" && break; done; '
    '[ -n "$KIROCREW" ] || KIROCREW=kirocrew'
)


def route_prelude() -> str:
    """Shell that sets ``$KIROCREW`` and ``$KC_ROUTE`` on the box.

    ``$KC_ROUTE`` is whatever the box's ``kirocrew kas route`` prints, or empty when
    it prints nothing: a box whose Kiro Crew predates the verb, or has no
    ``kirocrew`` at all, answers empty and every script then takes its kiro-cli
    branch -- the behaviour every box had before the verb existed. Only the exact
    spelling :data:`ROUTE_KAS_BUNDLE` selects the vault branch, so the vault is never
    chosen by an absence.
    """
    return (
        f"{KIROCREW_BIN_RESOLVE}\n"
        'KC_ROUTE="$("$KIROCREW" kas route < /dev/null 2>/dev/null || true)"'
    )


def is_vault_route_test() -> str:
    """The ``[ ... ]`` test the scripts branch on."""
    return f'[ "$KC_ROUTE" = "{ROUTE_KAS_BUNDLE}" ]'


def whoami_line(*, as_json: bool) -> str:
    """The vault-route replacement for ``"$KIRO" whoami [--format json]``."""
    fmt = " --format json" if as_json else ""
    return f'"$KIROCREW" kas whoami{fmt} < /dev/null 2>&1'


def logout_lines() -> str:
    """The vault-route replacement for the kiro-cli kill-then-logout block.

    Stops a vault sign-in still polling (it would otherwise re-fill the vault the
    moment it was emptied), then removes every stored identity. No engine process
    is killed here: the vault is part of the gateway's identity fingerprint, and
    the gateway's pre-turn identity-change check retires the running agent
    processes on the next turn (``dashboard/handlers/kas_login._retire_runtimes_after_sign_out``
    documents the same sweep), so no process keeps serving the signed-out account
    past that turn.

    The verb's exit status is kept in ``$KC_LOGOUT_RC`` for the caller's script
    to act on: a non-zero one means the sign-out did not complete (its fence was
    not written), and ``kiro-cli logout``'s "non-zero when there was nothing to
    drop" convention does not apply to it, so the script must not fold it into
    the unconditional ``exit 0`` the kiro-cli branch ends with.
    """
    return f"""
if command -v pkill >/dev/null 2>&1; then
  pkill -u "$(id -u)" -f "{VAULT_LOGIN_PROCESS_PATTERN}" 2>/dev/null || true
fi
"$KIROCREW" kas logout < /dev/null 2>&1
KC_LOGOUT_RC=$?
""".strip()


def login_launch(*, log_path: str, identity_provider: str = "", idp_region: str = "") -> str:
    """The vault-route background launch of ``kirocrew kas login``, writing to *log_path*.

    One shape for every target: Builder ID is the flagless default, an Identity
    Center target adds ``--provider idc`` with its start URL and region. The verb
    takes its answers from flags, prompts for nothing and prints the verification
    URL + code itself, so no pseudo-terminal driver is needed -- the kiro-cli
    branch's IdC pty dance has no counterpart here. Each value is shell-quoted
    because it flows from a CLI argument into a script run on the instance. The
    kiro-cli ``--license`` has no vault equivalent: the identity decides the tier.
    """
    flags = ""
    if identity_provider:
        flags += f" --provider idc --start-url {shlex.quote(identity_provider)}"
        if idp_region:
            flags += f" --region {shlex.quote(idp_region)}"
    return f"""
echo "{VAULT_ROUTE_SENTINEL}"
nohup "$KIROCREW" kas login{flags} >"{log_path}" 2>&1 </dev/null &
""".strip()
