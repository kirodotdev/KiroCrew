"""Single source for the Slack app manifest and its app-create deep link.

Three callers need the same bytes and MUST not disagree about them:

* ``cli_setup`` (``kirocrew manifest``) prints the manifest or the deep link,
* the dashboard's ``GET /api/slack/manifest`` serves both to Settings → Slack,
* ``security._kirocrew_slack_app_link_alias`` decides whether a deep link is
  ours by rebuilding the expected payload and comparing.

The third one is why this module exists rather than the render being inlined at
each call site. The exfil redactor exempts our deep link only when the payload
reproduces this template exactly, so if one emitter changed its strip rule or
trailing newline while the validator kept the old one, the exemption would stop
matching, the ``[REDACTED: suspicious URL to api.slack.com]`` bug would come
back for users, and no test would notice as long as the tests rebuilt the
payload themselves. Sharing the procedure — not just the template file — is what
makes that drift impossible.
"""

from __future__ import annotations

import re
from importlib.resources import files as _pkg_files
from urllib.parse import quote

#: Placeholder substituted with the operator's alias, in two places.
ALIAS_PLACEHOLDER = "{{ALIAS}}"

#: Placeholder substituted with ``slash_command(alias)``. Slack resolves
#: slash-command names across a whole Enterprise Grid org, so two installs that
#: both register ``/kirocrew`` collide and Slack routes the command to the wrong
#: app. Naming the command after the alias keeps each install's command its own.
COMMAND_PLACEHOLDER = "{{COMMAND}}"

#: The alias the dashboard renders with when the caller names none. It keeps
#: the generic ``/kirocrew`` command: that path saves no ``slack.command``, so
#: the gateway listens for the default and a renamed command would go unanswered.
DEFAULT_ALIAS = "kirocrew"

#: Prefix of every alias-named slash command.
SLASH_COMMAND_PREFIX = "kirocrew-"

#: Slack's limit on a slash command, leading ``/`` included.
SLASH_COMMAND_MAX = 32

#: Longest accepted alias. Both emitters enforce this, and the validator's
#: derived pattern is bounded by it. It is deliberately tight: the alias is the
#: ONLY caller-controlled span inside an exempted deep link, so its width is the
#: width of the channel the exemption opens. 32 also matches the Slack app-name
#: budget the manifest spends it on.
ALIAS_MAX = 32

#: Accepted alias shape, anchored. Kept in sync with ``ALIAS_MAX``.
ALIAS_RE = re.compile(rf"\A[A-Za-z0-9_-]{{1,{ALIAS_MAX}}}\Z")

#: Alias body for embedding in a larger pattern (unanchored, no group).
ALIAS_PATTERN = rf"[A-Za-z0-9_-]{{1,{ALIAS_MAX}}}"

APP_CREATE_HOST = "api.slack.com"
APP_CREATE_PATH = "/apps"
_APP_CREATE_BASE = f"https://{APP_CREATE_HOST}{APP_CREATE_PATH}"


def valid_alias(alias: str) -> bool:
    """True when *alias* is a shape both emitters will substitute."""
    return ALIAS_RE.match(alias) is not None


def slash_command(alias: str) -> str:
    """The slash command (no leading ``/``) the manifest registers for *alias*.

    Lowercased, because Slack accepts only lowercase command names, and cut to
    fit ``SLASH_COMMAND_MAX`` so every alias ``valid_alias`` admits still
    renders a command Slack accepts. This is also the ``slack.command`` value the
    gateway must be configured with for that app.
    """
    if alias == DEFAULT_ALIAS:
        return DEFAULT_ALIAS
    room = SLASH_COMMAND_MAX - 1 - len(SLASH_COMMAND_PREFIX)
    return SLASH_COMMAND_PREFIX + alias.lower()[:room]


def raw_template() -> str:
    """The packaged template verbatim, comments and placeholders intact."""
    return str(_pkg_files("kiro_crew").joinpath("slack-manifest.yaml").read_text("utf-8"))


def stripped_template() -> str:
    """The template with comment lines dropped, placeholders still intact.

    This is the one definition of "comment-stripped": the deep-link payload and
    the validator's expected-payload pattern are both built from it, so neither
    can drift from the other.
    """
    lines = [ln for ln in raw_template().splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip() + "\n"


def render(alias: str, *, strip_comments: bool = False) -> str:
    """The manifest with *alias* substituted.

    ``strip_comments=False`` keeps the guidance comments, which is what a human
    reads or pastes into Slack. ``True`` yields exactly the deep-link payload.
    """
    template = stripped_template() if strip_comments else raw_template()
    return template.replace(COMMAND_PLACEHOLDER, slash_command(alias)).replace(
        ALIAS_PLACEHOLDER, alias
    )


def deep_link(alias: str) -> str:
    """Slack's new-app deep link carrying the rendered manifest.

    ``quote`` (not ``quote_plus``): a literal ``+`` would decode back as a space
    under form-encoding, and ``%20`` is unambiguous everywhere Slack parses this.
    """
    encoded = quote(render(alias, strip_comments=True), safe="")
    return f"{_APP_CREATE_BASE}?new_app=1&manifest_yaml={encoded}"
