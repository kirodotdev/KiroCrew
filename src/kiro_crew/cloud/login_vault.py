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

# The one spelling of the route answer and the one exit code for "unknown", owned by
# the verb that prints them; the scripts compare against the same objects, so there
# is nothing to drift.
from kiro_crew.cli_kas_login import ROUTE_KAS_BUNDLE, ROUTE_UNKNOWN_EXIT

__all__ = ["ROUTE_KAS_BUNDLE", "ROUTE_UNKNOWN_EXIT"]

#: Echoed by the device-login script when it took the vault branch, so the local
#: :func:`kiro_crew.cloud.login.start_device_login` knows the box has no kiro-cli
#: to fall back to (the social callback flow is kiro-cli's) and reports the vault
#: sign-in's own outcome instead.
VAULT_ROUTE_SENTINEL = "__KIROCREW_VAULT_ROUTE__"

#: Echoed (and the command ended) by the callback-login script on a vault-route box:
#: there is no ``kiro-cli login`` callback flow to continue there.
VAULT_NO_CALLBACK_SENTINEL = "__KIROCREW_VAULT_NO_CALLBACK__"

#: Echoed (and the script ended non-zero) when the box's ``kirocrew kas route`` ran
#: and could NOT decide (exit :data:`ROUTE_UNKNOWN_EXIT`: an unreadable config, or a
#: crash). Neither program is touched then, kiro-cli installed or not: a box whose
#: config just broke may well be on the bundle route, and running ``kiro-cli logout``
#: there would report "signed out" while the vault credential stays usable. The
#: probe's own output follows the sentinel.
ROUTE_UNKNOWN_SENTINEL = "__KIROCREW_ROUTE_UNKNOWN__"

#: Echoed (and the script ended non-zero) when the box has NO sign-in program: its
#: ``kirocrew kas route`` did not answer ``kas-bundle`` and it has no ``kiro-cli``
#: either. Without this the kiro-cli branch would run on a box that has none and the
#: operator would read ``kiro-cli: command not found`` where the real cause is a
#: bundle-route box whose route probe failed (an unreadable config, a ``kirocrew``
#: too old for the verb). The script prints the probe's own output after the
#: sentinel so the local side can show it.
NO_SIGNIN_PROGRAM_SENTINEL = "__KIROCREW_NO_SIGNIN_PROGRAM__"

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
    """Shell that sets ``$KIROCREW``, ``$KC_ROUTE`` and ``$KC_ROUTE_OUT`` on the box.

    ``$KC_ROUTE`` is what the box's ``kirocrew kas route`` printed on STDOUT when it
    exited 0, or empty otherwise. Three exit codes mean three things, and the
    scripts keep them apart: ``0`` is an answer; :data:`ROUTE_UNKNOWN_EXIT` (``1``) is
    the verb saying it could not decide (``$KC_ROUTE_UNKNOWN=1``, and
    :func:`require_sign_in_program` then refuses before either program runs);
    anything else -- argparse's ``2`` from a ``kirocrew`` too old to have the verb,
    the shell's ``127`` for no ``kirocrew`` at all -- is "no route answer", and the
    script takes its kiro-cli branch, the behaviour every box had before the verb
    existed. Only the exact spelling :data:`ROUTE_KAS_BUNDLE` selects the vault
    branch, so the vault is never chosen by an absence. Stderr is captured APART (``$KC_ROUTE_ERR``, through a
    private temp file) and never joins the compared value: the config loader warns
    there about a loose ``config.local.json`` mode while the verb still answers
    ``kas-bundle`` on stdout, and a merged stream would fail the exact comparison
    on a working bundle-route box. ``$KC_ROUTE_OUT`` and ``$KC_ROUTE_ERR`` are both
    shown by :func:`require_sign_in_program` when the probe did not answer.
    """
    return (
        f"{KIROCREW_BIN_RESOLVE}\n"
        'KC_ROUTE_ERR_FILE="$(mktemp 2>/dev/null || echo /dev/null)"\n'
        'KC_ROUTE_OUT="$("$KIROCREW" kas route < /dev/null 2>"$KC_ROUTE_ERR_FILE")"; KC_ROUTE_RC=$?\n'
        'KC_ROUTE_ERR="$(cat "$KC_ROUTE_ERR_FILE" 2>/dev/null)"; '
        '[ "$KC_ROUTE_ERR_FILE" = /dev/null ] || rm -f "$KC_ROUTE_ERR_FILE"\n'
        'KC_ROUTE=""; KC_ROUTE_UNKNOWN=0\n'
        'if [ "$KC_ROUTE_RC" -eq 0 ]; then KC_ROUTE="$KC_ROUTE_OUT"; '
        f'elif [ "$KC_ROUTE_RC" -eq {ROUTE_UNKNOWN_EXIT} ]; then KC_ROUTE_UNKNOWN=1; fi'
    )


def require_sign_in_program() -> str:
    """Shell that refuses, readably, when the box has neither sign-in program.

    Spliced in right after :func:`route_prelude` and before the ``$KIRO`` /
    ``$KIROCREW`` branch of every script. Two refusals:

    - the probe RAN and could not decide (``$KC_ROUTE_UNKNOWN=1``): neither program
      is touched, whether or not kiro-cli is installed -- a box whose config broke
      may be on the bundle route, and signing kiro-cli's store out there would
      report a sign-out the vault never saw;
    - the route is not ``kas-bundle`` AND ``kiro-cli`` did not resolve: the kiro-cli
      branch would only print ``command not found``.

    Each names the real situation instead -- a box with no kiro-cli whose
    route probe did not say ``kas-bundle`` -- echoes the probe's own output (the
    ``kas route`` error text, or the shell's "not found" for a ``kirocrew`` the box
    lacks), and names the remedy. The sentinel comes first so the local side can
    classify the answer without parsing the prose. ``$KC_ROUTE_OUT`` and
    ``$KC_ROUTE_ERR`` are both printed here (blank lines dropped), since a failed
    probe's explanation is on stderr while a shell's "not found" may land on either.
    """
    return f"""
if [ "$KC_ROUTE_UNKNOWN" = 1 ]; then
  echo "{ROUTE_UNKNOWN_SENTINEL}"
  echo "This instance's 'kirocrew kas route' could not tell which program owns the Kiro sign-in (exit $KC_ROUTE_RC), so nothing was changed on it:"
  printf '%s\n%s\n' "$KC_ROUTE_OUT" "$KC_ROUTE_ERR" | sed '/^$/d; s/^/  /'
  echo "Fix the instance's Kiro Crew config.json (run 'kirocrew kas route' on the instance to see the error), then retry."
  exit 1
fi
if ! {is_vault_route_test()} && [ ! -x "$KIRO" ] && ! command -v "$KIRO" >/dev/null 2>&1; then
  echo "{NO_SIGNIN_PROGRAM_SENTINEL}"
  echo "This instance has no kiro-cli, and its 'kirocrew kas route' did not answer {ROUTE_KAS_BUNDLE} (exit $KC_ROUTE_RC):"
  printf '%s\n%s\n' "$KC_ROUTE_OUT" "$KC_ROUTE_ERR" | sed '/^$/d; s/^/  /'
  echo "If this instance runs the KAS bundle route, fix its Kiro Crew config.json (run 'kirocrew kas route' on the instance to see the error); otherwise install kiro-cli on it."
  exit 1
fi
""".strip()


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
