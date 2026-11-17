"""What a click, type or select changed: the ``changes`` report (``browser.report_changes``).

The walker's ``glance`` reads the page cheaply before and after the action; this
module turns the two reads into one short report, so the agent can see an opened
dialog, an error message, a field that failed validation or a new page without a
``snapshot`` call. It is pure: the reads, the settle and the gateway wiring live
in ``browser_harness.ops``.

Everything in the report is page text, so each piece is untrusted: it is stripped
of control characters, passed through the credential and exfiltration-link
redaction every egress site shares (``redact_via_context``) and only then cut to
a bounded single line. Text too long to read whole is left out, never cut, here
and in the walker, and so is text that holds a token split by white space. A field's value is never read (a field is named by its
label), and neither is a validation message, which can repeat what was typed.
The address is cleaned by the recordings' ``clean_url``: no credentials, query,
fragment or ``;`` path parameters, since those can carry a token.
"""

from __future__ import annotations

import bisect
import functools
from collections import Counter
from typing import Any

from kiro_crew.browser_harness.recording import clean_url
from kiro_crew.terminal_safe import safe_terminal_line

#: Entries kept per list in the report; the rest are counted, not listed.
LIST_CAP = 8
#: Longest control or field name, and longest dialog or alert text, in characters.
NAME_CAP = 60
TEXT_CAP = 160
URL_CAP = 200
#: Longest page-written string read at all: a longer one is left out, never cut, so
#: a credential reaches the redaction whole. At most ``safe_terminal_line``'s own
#: bound, so that step never cuts either.
_INPUT_CAP = 2000
#: Most entries read from one list of a census: a hostile page cannot grow the report.
_READ_CAP = 700
#: The census lists the walker can cut short, and what each one counts.
_CAPPED_LISTS = {
    "dialogs": "open dialogs",
    "alerts": "alert and status messages",
    "invalid": "fields",
}


def clean(value: Any, cap: int) -> str:
    """One bounded line of page text: no controls, no credentials, no exfiltration link.

    Nothing is cut before it is redacted: a cut can split a credential below the
    length the redaction matches. A string longer than ``_INPUT_CAP`` is left out
    (``""``), and so is one that holds a token once its white space is removed,
    whether a prefixed rule or the structured detectors' token formats find it;
    the cut to *cap* comes after the redaction.
    """
    if not isinstance(value, str) or len(value) > _INPUT_CAP:
        return ""
    # Imported here: the MCP servers' tool listing imports the ops module.
    from kiro_crew.platform import context as redaction

    read = safe_terminal_line(" ".join(value.split()))
    line = redaction.redact_via_context(read)
    if _holds_split_token(line) or _holds_split_format(read):
        return ""
    return line if len(line) <= cap else line[: cap - 1] + "…"


#: The pass-1 rules whose match starts with a fixed prefix (``ghp_``, ``xoxb-``,
#: ``eyJ`` and the like). Only these read page text with its white space removed:
#: the others (a query's ``token=``, a link, digits and a colon) also match ordinary
#: prose run together.
_PREFIXED_RULES = frozenset(
    {
        "slack_token",
        "github_token",
        "gitlab_token",
        "stripe_key",
        "sendgrid_key",
        "openai_key",
        "anthropic_key",
        "npm_token",
        "pypi_token",
        "digitalocean_token",
        "google_oauth_secret",
        "jwt",
    }
)
#: Longest stretch read from a token prefix: past every published form the
#: detectors know (the longest, Slack's, is 255 characters).
_TOKEN_SPAN = 300
#: Most stretches read in one line; a line that needs more is left out.
_SPLIT_READS = 64


def _holds_split_token(line: str) -> bool:
    """Whether *line*, already redacted, holds a prefixed token once its white space is gone.

    Page markup can put white space inside a token the page draws whole (a line
    break, a positioned span); the pieces then match no rule. The redactor's tags
    match none of these rules, so only a token the first pass missed is found.
    """
    from kiro_crew.security.redaction import redact_credentials_with_records

    found = redact_credentials_with_records("".join(line.split()))[2]
    return any(match.rule in _PREFIXED_RULES for match in found)


@functools.lru_cache(maxsize=1)
def _fixed_prefixes() -> tuple[str, ...]:
    """Every prefix the structured detectors know a secret token by (``AIza``, ``hvs.``).

    Read from the detectors' own table, so a format added there counts here too,
    whatever rule name its redaction record carries. A start shorter than
    :data:`_MIN_FIXED_PREFIX` (a Telegram token's first digit, a Discord token's
    first letter) is no fixed prefix: run-together prose holds one at every word.
    """
    from kiro_crew.security import redaction_structured as detectors

    return tuple(
        prefix
        for row in detectors.ROWS
        if row["detector"] == "token_format" and row["tier"] == detectors.SECRET
        for prefix in row["prefixes"]
        if len(prefix) >= _MIN_FIXED_PREFIX
    )


#: The shortest token start read as a fixed prefix (``eyJ``, ``hf_``).
_MIN_FIXED_PREFIX = 3


def _holds_split_format(text: str) -> bool:
    """Whether *text*, as read, holds a secret token the detectors find only across white space.

    The text is run together, and the detectors read it from each prefix they
    know a secret token by to each word end within ``_TOKEN_SPAN``, so the words
    around a token do not hide its edges. A hit counts by the detector that made
    it (a published token format, secret tier) and only when it covers white
    space that was removed; one that covers none is a whole token, which the
    redaction already replaced. Past ``_SPLIT_READS`` reads the text is left out.
    """
    words = text.split()
    if len(words) < 2:
        return False
    from kiro_crew.security import redaction_structured as detectors

    compact = "".join(words)
    ends: list[int] = []
    for word in words:
        ends.append((ends[-1] if ends else 0) + len(word))
    gaps = ends[:-1]
    reads = 0
    for prefix in _fixed_prefixes():
        at = compact.find(prefix)
        while at != -1:
            index = bisect.bisect_right(ends, at + len(prefix))
            while index < len(ends) and ends[index] - at <= _TOKEN_SPAN:
                reads += 1
                if reads > _SPLIT_READS:
                    return True
                for hit in detectors.redact_hits(compact[at : ends[index]])[1]:
                    if hit.tier != detectors.SECRET or hit.detector != "token_format":
                        continue
                    gap = bisect.bisect_right(gaps, at + hit.start)
                    if gap < len(gaps) and gaps[gap] < at + hit.end:
                        return True
                index += 1
            at = compact.find(prefix, at + 1)
    return False


def _strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw[:_READ_CAP] if isinstance(item, str)]


def census(raw: Any) -> dict[str, Any] | None:
    """The walker's ``glance`` answer in the shape :func:`diff` reads, or ``None``."""
    if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
        return None
    return {
        "url": raw["url"],
        "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
        "controls": _strings(raw.get("controls")),
        "dialogs": _strings(raw.get("dialogs")),
        "alerts": _strings(raw.get("alerts")),
        "invalid": _strings(raw.get("invalid")),
        "truncated": raw.get("truncated") is True,
        "capped": _capped(raw.get("capped")),
    }


def _capped(raw: Any) -> frozenset[str]:
    """The lists the walker stopped reading before the page ended."""
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(name for name in _CAPPED_LISTS if raw.get(name) is True)


def _added(before: list[str], after: list[str]) -> list[str]:
    """Entries of *after* not matched by one of *before*, in page order."""
    left = Counter(before)
    out: list[str] = []
    for item in after:
        if left[item] > 0:
            left[item] -= 1
        else:
            out.append(item)
    return out


def _listed(items: list[str], cap: int, withheld: str = "") -> list[str]:
    """*items* cleaned; an entry left out reads as *withheld*, or is dropped when it is ``""``."""
    cleaned = (clean(item, cap) or (withheld if item.strip() else "") for item in items[:LIST_CAP])
    shown = [text for text in cleaned if text]
    if len(items) > LIST_CAP:
        shown.append(f"... and {len(items) - LIST_CAP} more")
    return shown


def _control(key: str) -> str:
    role, _, name = key.partition("|")
    kind, label = clean(role, NAME_CAP) or "generic", clean(name, NAME_CAP)
    return f'{kind} "{label}"' if label else kind


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The report of what differs between two censuses; ``{"no_change_seen": True}`` if none."""
    out: dict[str, Any] = {}
    moved = False
    if before["url"] != after["url"]:
        now, was = clean(clean_url(after["url"]), URL_CAP), clean(clean_url(before["url"]), URL_CAP)
        # An address too long to read whole reads as "": say it is new, not unchanged.
        moved = now != was or not now
        if moved:
            out["url"] = now or "an address too long to show"
        else:
            out["url"] = f"{now} (query, fragment or path parameters changed)"
    if before["title"] != after["title"] and (title := clean(after["title"], TEXT_CAP)):
        out["title"] = title
    # A list cut short on either side hides its tail, so it is said to be uncompared.
    capped = before["capped"] | after["capped"]
    for name in sorted(capped):
        out[name] = f"not compared: the page has more {_CAPPED_LISTS[name]} than the report reads"
    if "dialogs" not in capped:
        for key, names in (
            ("dialog_opened", _added(before["dialogs"], after["dialogs"])),
            ("dialog_closed", _added(after["dialogs"], before["dialogs"])),
        ):
            if names:
                out[key] = _listed(names, NAME_CAP) or ["dialog"]
    for name, cap, withheld in (
        ("alerts", TEXT_CAP, "a message that could not be shown"),
        ("invalid", NAME_CAP, ""),
    ):
        if name not in capped and (
            listed := _listed(_added(before[name], after[name]), cap, withheld)
        ):
            out[name] = listed
    if before["truncated"] or after["truncated"]:
        out["controls"] = "not compared: the page has more controls than the report reads"
    elif moved:
        out["controls"] = "a new page: take a snapshot for its refs"
    else:
        appeared = _added(before["controls"], after["controls"])
        gone_now = _added(after["controls"], before["controls"])
        if appeared:
            out["appeared"] = [_control(k) for k in appeared[:LIST_CAP]] + _more(appeared)
        if gone_now:
            out["disappeared"] = [_control(k) for k in gone_now[:LIST_CAP]] + _more(gone_now)
    return out or {"no_change_seen": True}


def _more(items: list[str]) -> list[str]:
    return [f"... and {len(items) - LIST_CAP} more"] if len(items) > LIST_CAP else []


def dialog_report(shown: dict[str, Any]) -> dict[str, Any]:
    """The report when a JavaScript dialog holds the page: it cannot be read until answered.

    *shown* is the dialog as the page wrote it, uncut: :func:`clean` redacts the
    whole message, or leaves it out when it is too long to read whole.
    """
    return {
        "js_dialog": {
            "type": clean(shown.get("type"), NAME_CAP) or "alert",
            "message": clean(shown.get("message"), TEXT_CAP),
        },
        "note": "the page is held by this dialog; call `dialog` to answer it",
    }
