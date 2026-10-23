"""Liveness tests for the shell-command gate.

``is_sensitive_bash_command`` runs synchronously on the gateway's event loop,
under a loop-stall watchdog that hard-exits the process after 25 s of silence
(``dashboard.loop_stall_exit_after_secs``). Path-matching passes over the command
are quadratic, so a ~9 KB command full of ``https://`` URLs can stall the loop
past that budget. The gate does not match paths in command text at all -- and
what it still runs
(the size ceiling, the IMDS detector and the environment-credential detector) is
pinned here at the crash size, under the ceiling, where it is what the loop
actually pays. The ceiling itself is pinned as a refusal, not a skip: a command
too long to scan is denied rather than let through unscanned.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

from kiro_crew import security
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    is_sensitive_bash_command,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shapes
# ─────────────────────────────────────────────────────────────────────────────

_SEG = "a" * 60


def _double_separator_command(n: int) -> str:
    """n path operands, each with a doubled ``//`` -- the shape that would send
    the command through a separator-collapsed re-scan."""
    return "ls " + " ".join(f"/opt//{_SEG}" for _ in range(n))


def _url_payload_command(n: int) -> str:
    """The field shape: a JSON body of ``https://`` URLs handed to curl."""
    urls = [f"https://tasks.example.test/T{100000 + i}?view=full&x={_SEG[:20]}" for i in range(n)]
    return "curl -s -X POST -d " + json.dumps({"items": urls})


# ─────────────────────────────────────────────────────────────────────────────
# Package shape: the split must not grow back into a monolith
# ─────────────────────────────────────────────────────────────────────────────


#: Ceiling on the whole security PACKAGE, not on any one file in it. The controls
#: were one module of about 21,800 lines, and the split adds a re-export block, an
#: export manifest and the mirroring facade on top of the code it relocates, so the
#: budget is that size plus room for the machinery, plus the redaction record,
#: credential-source and allowed-host modules, plus the resolver child script
#: (``_child_realpath.py``, ~190 lines) that lives beside the resolver it serves
#: rather than in the pool package. It is a bound on total volume:
#: relocating a declaration between submodules moves nothing across it.
#:
#: Raised again, from 27,200, when the facade stopped binding re-exported names
#: eagerly and began resolving each through its owner. That trades one import block
#: for two name lists -- an owner table and a ``TYPE_CHECKING`` block, one line per
#: exported name in each -- which measured 618 lines at the current surface and is
#: machinery, not control logic.
#:
#: Raised again, from 27,751, for the write-protected home entries covering the MCP
#: launch-approval directory and ``mcp/resolved``: gatewayd spawns an approved stub's
#: backend outside the sandbox, so a session must not be able to write either path.
#:
#: Raised again, from 27,761, for the ssh self-target refusal note: it says how long
#: a retry can still land inside the background check and names the IP-literal case
#: where this machine's address list cannot be read, so a refused agent knows when
#: to stop retrying and what to use instead.
#:
#: Raised again, from 27,766, for the write-protected home entry covering the kiro-cli
#: global MCP registry (``~/.kiro/settings/mcp.json``) and its ``KIRO_HOME``
#: re-anchoring: an ``autoApprove`` on an entry there is honoured by default and skips
#: the tool gate entirely, while the entry that decides it is admitted on its name
#: shape rather than on who wrote the file -- so an agent-writable registry grants its
#: own verbs a standing bypass. The reasoning for one leaf is most of the cost, which
#: is the shape every entry on this tier has.
#:
#: Raised again, from 27,814, for resolving the ``$HOME``-rooted form of both kiro-cli
#: write-tier leaves rather than only their ``KIRO_HOME`` copies. Anchoring them
#: lexically covered a symlinked ``$HOME`` itself but not one further down the path, so
#: a dotfile-managed ``~/.kiro`` left the real spec dir and the real MCP registry outside
#: the fence while their ``~``-spelled paths stayed inside it. The cost is the reasoning
#: plus one shared tuple, which is what replaces a second per-leaf arm.
#:
#: Lowered to 27,851 by SUBTRACTION. An earlier revision of this branch also emitted
#: every kiro-cli target in a second, all-forward-slash spelling, on the premise that a
#: Windows root could reach the anchor builder carrying the operator's own separators.
#: That premise is false: every root arrives through ``_resolve_root_anchors``, which
#: returns ``_realpath_or_none(expanded) or _lexical_root(expanded)``, and both answer
#: in the native spelling -- ``_lexical_root``'s ``os.path.normpath`` converts
#: ``C:/Users/x`` to ``C:\Users\x``. With no input that fails without it, the second
#: spelling was decoration, and on POSIX it would fence a bogus neighbour for any file
#: whose name contains a backslash. It is removed together with the two tests that
#: existed only for it; the ``$HOME``-symlink coverage passes without it, which is what
#: shows it was never load-bearing.
#:
#: Raised again, from 27,851, because the ``KIRO_HOME`` half was two hardcoded
#: per-leaf arms while the ``$HOME`` half looped the tuple -- so the tuple's own
#: comment ("a third leaf joins both halves by landing here") was false, and a third
#: leaf would have been fenced under ``$HOME`` and writable under the override. Both
#: halves now loop the same tuple, each leaf's tail is spelled once, and a test adds a
#: probe leaf and asserts BOTH spellings refuse -- it fails on the old code.
#:
#: Re-pinned again, on top of every raise above, for the ``panel-dismissals`` leaf
#: added to ``_CREW_SECRET_LEAVES`` in ``paths.py``: one entry plus the comment
#: stating why nothing a run can reach may forge or delete the operator's dismissal
#: records. Ten lines, all of them the fence declaration and its reason -- no new
#: control logic and no new matching pass. This branch's raise and the ones above it
#: are independent additions to the same ratchet, so the number below is re-MEASURED
#: off the tree rather than being the arithmetic sum of the deltas.
#: Raised again, from 27,863, for the own-address startup warm in ``argv_floor``:
#: the gateway starts the netlink read at boot, the worker reads and publishes that
#: table before any DNS lookup, and the publish merges the addresses and opens the
#: IP-literal window in one lock hold while each check reads the window flag before
#: the names, so the first ssh after a restart is not refused as this machine and a
#: secondary own IP is never admitted mid-publish. A dump that ends without
#: NLMSG_DONE, or that the kernel flags NLM_F_DUMP_INTR, counts as unread, so a
#: partial table never opens the window. So does an NLMSG_DONE whose errno is not 0.
#: Three incomplete dumps in a row log one warning, so a host whose table never
#: reads can be told apart from a target that is really this machine.
#:
#: Re-pinned from 27,942 for the NUL blanking in ``inline_payload._lex``: one line
#: that swaps each NUL for a space before tokenizing, plus the docstring saying why.
#: CPython 3.12 raises ``SystemError`` for a NUL after an indented block, which
#: escaped the lexer and crashed the gate on an ordinary ``b'\0'`` in a payload.
#: No new rule and no new matching pass.
#: Raised again, from 27,948, by one line: the ``vouched-executions`` entry in
#: ``_SENSITIVE_HOME_DIRS``. Each file there is the gateway's restart-surviving
#: word that a session may reach its member's private store, so no file tool may
#: write it. The fuller reason lives beside its ``sandbox._CREW_HIDDEN_LEAVES`` mask.
#:
#: Re-pinned from 27,949 for ``redaction._DOCUMENT_LINK_RE``: pass 3 skips a run
#: wholly inside a Google Docs, Drive or Confluence link of a fixed route, because a
#: document id is the same random base64 a key is and no gate can split the two.
#: One route regex, one span helper, a four-line check in pass 3, and the comment
#: naming the residual. No pass widened and no threshold moved.
#:
#: Raised again, from 28,025, for the ssh self-target floor's boot-time warm-up: the
#: own-address table is read at gateway startup and published before the DNS
#: lookups, and background threads parse the hosts-file table (in bounded chunks,
#: keyed on the own-address set it was judged by). On a miss the gate path parses
#: only a file that fits in one read chunk; a larger file is refused as pending
#: until the background parse is cached. On Windows, where ``st_ctime`` is creation
#: time, the key also carries a content digest (``hosts_file.py``, which holds the
#: line parser and digest helpers apart from ``argv_floor``'s per-module cap). A
#: Windows file too large to hash on the gate is never served, so a dotless target
#: there is pending, and the ssh self-target refusal note says so.
#:
#: Raised again, from 28,399, by two lines: case (1) of the ssh self-target refusal
#: note names a dotless target refused while a hosts file over 64 KiB is still read
#: in the background, so a caller retries it rather than treating it as settled.
#:
#: Raised again, from 28,401, for the containment gate's ``pre_resolved`` keyword:
#: one keyword on ``path_contains_sensitive``, forwarded to the two helpers that
#: already take it, plus the preconditions it carries written on the keyword
#: itself. The claim is ``is_sensitive_resolved_path``'s, unchanged: the caller
#: holds the canonical spelling and is off the event loop, so the anchors resolve
#: inline. No new entry point, no target, no matching rule and no threshold moved.
#:
#: The number IS the package's measured total, carrying no spare room: a ratchet with
#: headroom admits exactly the unreviewed growth it exists to catch, so the next line
#: added here fails this gate and has to be re-pinned deliberately, with its reason
#: written above. The guards that detect a monolith growing back are the per-file cap
#: and the facade's share below, and both must stay untouched.
#:
#: Raised for the ``registry_trust.json`` leaf added to ``_CREW_SECRET_LEAVES`` in
#: ``paths.py``: the operator's grants of ``owner`` trust to a hand-configured app
#: registry live in a keystone file on the same read+write floor as
#: ``denied_commands.json``, so the leaf and its two-line reason are three lines the gate
#: cannot avoid.
#:
#: Raised for the read-only bash gate's refusal of variable-assigning expansions
#: (`$[...]`, an `=` after `${`): one pattern alternative plus its reason comment.
#:
#: Raised for six stdout-only filters on the read-only bash allowlist (`tr`, `nl`,
#: `rev`, `comm`, `od`, `column`) and their reason comment.
#:
#: Raised for pass 3's macOS per-user directory exemption in ``redaction``:
#: withholding this host's own ``confstr`` id from the bare-secret scan, so a macOS
#: temp path (a computer-use screenshot among them) is not read as a key, costs the
#: id lookup, its grammar, the per-id pattern, the reason only the host's own id is
#: safe to withhold, and window classification with whole-run context that exempts
#: only windows sharing ≥ 24 bytes with that id while every other positive window
#: redacts each piece it touches. One mechanism, no new pass.
#:
#: Raised again, from 28,551, for the Windows alias fold in ``paths.py``: one lexical
#: helper strips a local-drive namespace prefix and a default-stream suffix, and
#: ``_candidate_forms`` resolves the folded spelling while keeping the raw one as a
#: candidate. No target, no matching rule and no threshold moved.
#:
#: Raised again, from 28,572, for ``StreamRedactor``'s two read-only properties,
#: ``held`` and ``discarding``, which the Slack stream reads at a ``wait`` instead
#: of the private fields. No pattern moved.
#:
#: Raised again, from 28,582, for ``redaction._ROUTED_COMMIT_RE``: a key-shaped window cut
#: out of a longer run is declined when twelve of its characters are digits of a
#: commit right after a code-host route (``blob/``, ``tree/`` and the rest), because
#: the window of a commit permalink that straddles the repository name and the
#: commit clears every gate. One regex and one threshold with their measured reason,
#: a helper that reads only the first commit starting after a window and declines a
#: window crossing that commit's opening ``/`` with twelve of its digits, one clause
#: beside the separator ceiling, and the docstring naming them. No pass widened and
#: no existing threshold moved.
#:
#: Raised again, from 28,631, for the ``-d @`` benign-program carve-out in
#: ``exfil.py``: the bare-substring data-exfil denial false-positives on GNU
#: ``date`` epoch conversions and ``grep`` searches for the literal ``-d @``.
#: The denial is unchanged; the carve-out reuses ``denied_rules._exception_eligible``
#: and allows the hit only for a single plain command whose first word runs no
#: subcommand. It is a security-deciding predicate, so it cannot leave the package,
#: and no dead code remains to offset it. Its first word is split on space and tab
#: only, the way bash splits, so a Unicode space cannot pose as a word break.
#:
#: Raised again, from 28,665, for a PlantUML route in ``redaction._DOCUMENT_LINK_RE``:
#: an encoded diagram is deflate output in a base64 alphabet, so pass 3 masked
#: nearly every diagram link. The route admits only a ``plantuml`` host and a
#: ``png|svg|txt|uml`` path, and the span counts only when the diagram inflates,
#: whole, to printable text that every credential pass leaves unchanged. The
#: text inflated per link and per call is capped so the extra scan stays that of
#: a 16 KiB plain text. One route, the decode helpers, their comment. No pass
#: widened and no threshold moved.
#:
#: Raised again, from 28,740, for the one import ``redaction_allow`` needs to publish
#: its hosts file through ``atomic_write.replace_with_retry``, which retries the
#: Windows sharing violation a bare ``os.replace`` lost the write on. No pattern moved.
#:
#: Raised again, from 28,741, for ``redaction._credential_search``: the credential
#: scan finds the JWT-branch start with its own linear finder, which skips a failed
#: segment run whole, so a long ``eyJ`` run costs linear time, not quadratic. It
#: returns the same match as ``_CREDENTIAL_PATTERNS.search`` at every step. The
#: other branches' start search guards the Telegram branch so a long digit run is
#: walked once, not once per digit, and the JWT start keeps its run's end so an
#: overlapping match does not re-walk that run. No pattern moved and no pass changed.
#:
#: Raised again, from 28,827, for ``denied_rules._exception_applies``: the
#: deny-exception check both passes of ``is_denied`` share, which reads a deny
#: exception through kiro-cli's one ``Running: `` title prefix so a shell title
#: earns exactly its command's carve-out. No glob, verb or eligibility test moved.
#:
#: Raised again, from 28,844, for ``readonly_bash.writes_through_redirect``: the
#: classifier's redirect rule on its own, so the trusted-pattern tier judges a
#: write redirect exactly as the read-only tier does, and for ``_DEVNULL_REDIR_RE``
#: ending each discard idiom where bash ends the word, with its classes spelled out
#: so a Unicode space or digit cannot pose as one. No verb or allowlist moved.
#:
#: Raised again, from 28,857, for ``shell_normalizer._SCRIPT_EXECUTES_RE``: its sed
#: ``e`` branch reads the ``e`` command as a word of the script, not the bare
#: ``-e``/``-E`` option, so a ``grep -E`` over a product-named directory keeps the
#: data-consumer exemption. The branch grows a ``(?<!^-)`` guard and its comment states
#: which terminal ``e`` is the command. Every execute construct it read before is still
#: read; no other pattern moved.
#:
#: Raised again, from 28,864, for the no-event-loop branch of the hosts-file
#: verdict in ``argv_floor.py``: a thread with no running loop (a cron-launched
#: MCP server) parses a large hosts file in the call instead of refusing as
#: pending with no retry coming. One branch, reusing ``atomic_write.on_event_loop``,
#: and the docstring and comment naming it; the on-loop path is unchanged.
#:
#: Raised again, from 28,872, for ``_deny_pattern_passes``: ``is_denied``'s
#: whole-string and per-segment passes moved, unchanged, into one helper so that
#: ``is_denied_synthesized_target(..., segments=True)`` can run them over the
#: operator's own rules for a shell title kiro-cli cut short -- a rule anchored to
#: one chained command of that title would otherwise stop firing. No new rule, no new
#: matching logic and no floor change: the growth is the helper's signature, the flag
#: and its docstring.
#:
#: Raised again, from 28,906, for the AWS env-filter rules. grep reads the
#: selector as text, while awk evaluates it as code and sed can delimit an
#: address with any character, so they
#: are two catalog rows the keystone both enforces (one row would need a top-level
#: alternation, which leaves the linear fragment matcher); only grep's text
#: selector lets ``aws-account-id=`` pass.
#:
#: Raised again, from 28,939, for the companion token-parameter URL-prefix seam in
#: ``redaction``: pass 4 leaves a ``?token=`` value alone only when it is one
#: unreserved/base64/percent-encoded run (plus trailing closers) directly after a
#: ``CredentialPolicy.token_param_exempt_url_prefixes()`` member and no earlier pass
#: claimed any of it. Costs the prefix reader with its degrade-to-empty contract, the
#: prefix grammar, the enclosing-URL boundary scan, the preceding-destination guard
#: (with proven earlier exempt links and their own ``<`` autolink opener blanked; any
#: raw ``&`` or ``<`` before the prefix is a destination signal, never decoded), the
#: value-run classes, and the match predicate. Public default is the empty set, so
#: nothing is exempt without a companion. The context-local stream suspension and its
#: exception-safe wrapper add the fail-closed rule for windows that cannot inspect
#: the whole destination. Then by 128 more for the per-text scan state that keeps
#: the two guards linear: candidates arrive left to right, so the destination guard
#: reads each character once (a found signal is sticky, a scan resumes after the
#: last exempt URL) and the boundary guard carries the last whitespace and the
#: whitespace-free run an earlier matched prefix already settled. Same answers, and
#: repeated exempt links cost one read of the text, not a rescan of the whole prefix each.
#: Then by 20 more for replacing the variable-width scheme branch with
#: fixed-width destination candidates and a backward scheme walk over disjoint
#: colon-delimited runs. This removes regex retry amplification without widening
#: the exemption or adding another scan of the message.
#: Then by 21 more for ``redact_credentials``'s ``defer_token_param`` keyword: a
#: streamed delta that a whole-segment run redacts again leaves pass 4 to that run
#: while a prefix set is installed, read lazily on the first ``token=`` match as
#: before. Every other pass still runs on the delta; the empty default and every
#: other caller are byte-identical, and the stream suspension still wins. Then by
#: 4 more because only a value no earlier pass touched is deferred: a partly
#: claimed value is still redacted per delta, gaps and all.
#:
#: Raised again, from 29,165, for one write-protected TARGET in ``paths.py``: the microvm
#: lane's crew records, whose rows carry the VM id ``teardown`` hands to
#: ``launcher.terminate``. Ten lines, of which four are the comprehension and six the
#: reason every entry in that list carries. No matching rule, no threshold and no pass
#: moved -- a target was added to a list the gate already walks.
#:
#: Raised again, from 29,175, for the three ``sql`` catalog rows' descriptions,
#: which now say the match is on the statement's text anywhere on the command line
#: (heredoc bodies included) and name the file-editing tool for prose that mentions
#: one. Six lines of catalog text: no pattern, matcher or floor change.
#:
#: Raised again, from 29,181, for the credential pre-filter's literal-led
#: anchors: the Telegram, Discord and Authorization anchors are respelled to
#: start with a literal ``re`` searches at memchr speed, which takes a second
#: Authorization half and the comments that say why each spelling accepts
#: exactly what the old one did. No credential branch, gate verdict or pass moved.
#:
#: Raised again, from 29,205, for ``readonly_bash.command_invokes_git``: the one
#: pure check every read-only shell auto-approve asks so it never approves a
#: command that runs git, which reads ``.git/config`` and so runs whatever
#: ``core.fsmonitor`` / ``core.hooksPath`` name. About 89 lines, most of them the
#: docstring, the runner and opener tables and the redirection and hidden-command
#: patterns. It only narrows what auto-approves; no deny rule, threshold or pass
#: moved.
#:
#: Raised again, from 29,293, for the ``engines`` leaf in ``paths.py``: the KAS
#: engine bundle the gateway installs and later executes joins the file-tool floor
#: beside ``playwright-cli`` -- one entry and its reason. No matcher moved.
#:
#: Raised again, from 29,296, for the hosts-file parse in ``argv_floor.py``
#: telling apart the two halves of its cache key: a file that moves mid-parse is
#: still ``"changed"``, while an own-address set the enrichment worker grows
#: mid-parse hands the table back uncached, so a name absent from an unchanged
#: file falls through to the DNS layer on the first ssh vet. Nineteen lines, five
#: of them code and the rest the docstrings that say which half may move and why
#: each answer stays fail-closed. No rule, threshold or pass moved.
#:
#: Raised again, from 29,315, for ``redaction_switch.code_text``: a scope inside which pass 3
#: spares an ordered alphabet and a data URI payload, the flag it sets, the one place pass 3
#: reads it, and the two checks that tell those shapes from a key. No credential branch, gate
#: verdict or other pass moved.
#:
#: Raised again, from 29,411, for the discovery-verified OAuth consent endpoints in
#: ``exfil.py``: the in-memory ``(host, path) -> expiry`` set with its TTL and cap,
#: ``record_discovered_oauth_endpoint`` (which reuses the operator file's host and
#: path validators), its ``oauth_endpoint_discovered`` SEL emitter, one lookup in
#: ``_approved_oauth_authorization_endpoint`` and the facade export. The rationale
#: lives in the security spec, not the package. An approved endpoint gets exactly
#: the builtin set's exemption; no pattern, threshold or pass moved.
#:
#: Raised again, from 29,492, for the PEM private-key scan: a header that no END marker
#: follows is matched without the full-block tail, which reads to the end of the text and
#: made a text that repeats the header quadratic. 56 lines are the new ``pem_scan`` module
#: (the spellings and the last-END-marker lookup) and 25 the choice between the two
#: patterns in ``_credential_search``. No credential branch, match or pass moved.
#:
#: Raised again, from 29,573, for the one ``pinned-order`` entry in
#: ``paths._CREW_SECRET_LEAVES``: the person's pinned-session order is fenced from
#: agent file tools like ``tag-grants`` beside it.
#:
#: Raised again, from 29,574, for the write tier's reason-or-None spelling and the
#: stall wording that names the stalled prefix and the cooldown left: a
#: stall still refuses everywhere it did; only the words a refused agent reads moved.
#: The prefix is named only while its cooldown is open, since most stall raises never
#: charge it.
#:
#: Raised again, from 29,607, for the chat segment save: the link pass can hand
#: back the addresses it replaced and the credential records take the per-delta
#: ``defer_token_param``, so the save can check each value a streamed delta's
#: redaction removed. No matcher moved. Seven more lines rewrite the blocked links
#: longest first, so a blocked link that prefixes a longer one cannot leave the
#: longer one's tail in the text.
#:
#: Raised again, from 29,623, for ``rm_floor``: the small seatbelt behind the two
#: recursive-force rm rows (segments from the shared split, words from the shared
#: ``_shell_tokens``, flags and an exact root/home target) and its gated branch at the
#: end of ``is_denied``. No pattern moved.
#:
#: Raised again, from 29,751, for the split of ``__init__.py`` and ``shell_normalizer.py``
#: under the 2,000-line cap. No rule, pattern or pass changed, and every moved definition
#: keeps its syntax tree but one docstring's wording. The 458 lines are glue only:
#: 205 are the eight new modules' docstrings and imports; 86 are
#: ``shell_normalizer.py``'s re-imports of the names its callers import from it and the
#: docstring naming its owners; 146 are the facade's ``_EXPORTS`` and
#: ``TYPE_CHECKING`` rows for the moved names and the owner import at its foot; and
#: 21 are the moved privates added to the frozen manifest.
#:
#: Raised again, from 30,209, for the argv floor's owner modules: ``argv_floor.py``
#: is a mirror facade over ``argv_floor_self``, ``argv_floor_hosts``, ``argv_floor_ssh``
#: and ``argv_floor_git``. The 462 lines are glue: the owners' docstrings, imports and
#: logger, the facade's family map, owner list, install and two ``TYPE_CHECKING`` manifests.
#: Every moved statement is unchanged; no rule, threshold or pass moved.
#:
#: Raised again, from 30,671, for the path floor's owner modules: ``paths.py`` is
#: a mirror facade over ``paths_lists``, ``paths_resolver`` and ``paths_targets`` that
#: keeps the gates. The 367 lines are glue: the owners' docstrings, imports and
#: logger, the ``PathResolutionStalled.__module__`` restore, and the facade's owner
#: imports, owner list, install and two ``TYPE_CHECKING`` manifests. Every moved
#: statement is unchanged bar one docstring phrase in ``_home_targets_tier``; no rule,
#: threshold or pass moved.
#:
#: Raised again, from 31,038, for the rule catalog, output redaction and egress owner
#: modules: ``denied_rules.py``, ``redaction.py`` and ``exfil.py`` are mirror facades over
#: ``denied_rules_catalog`` and ``denied_rules_tables``, ``redaction_patterns`` and
#: ``redaction_entropy``, and ``exfil_url_rules``, ``exfil_oauth`` and ``exfil_bash``. The
#: 471 lines are glue: the owners' docstrings, imports and logger; the facades' docstring
#: paragraphs, glue comments, owner imports, owner list, install, class swap and ``__all__``; and
#: their ``TYPE_CHECKING`` manifests (two each in ``redaction`` and ``exfil``, one in
#: ``denied_rules``). Every moved statement is unchanged; no rule, threshold or pass changed.
#:
#: Raised again, from 31,509, for the stream redactor's AWS key-value anchor: a
#: recognised AWS label holds its value past the 512 floor up to the JWT ceiling and
#: fails closed beyond it, the way the Bearer anchor already does. 217 lines are the
#: new ``stream_aws_anchor`` module (the label grammar read off the batch label rules,
#: its linear finder, the multi-carrier discard and the glued-label back-off) and 139
#: the ``feed`` call sites, including the cut that lands only at whitespace or the
#: buffer start, and the check that keeps a key name ending another credential
#: inside that credential. No credential branch moved.
#:
#: Raised again, from 31,865, for ``pass_compose.py``: the one composed pass that finds
#: credential and outbound-URL spans on the same text and rewrites it once. It is new
#: behaviour, not moved code, so no other file shrinks to pay for it, and it belongs in
#: this package because egress code must reach the raw passes only through it.
#:
#: Raised by four, from 32,056, for the ``app-agent-launch`` entry on the file-tool write
#: fence and its three-line comment: the empty directory an app agent's launch copy runs
#: kiro-cli from, which the sandbox seals read-only too.
#:
#: Raised again, from 32,060, by 37 lines for ``exfil_url_rules._heavy_percent_encoding``
#: and its manifest entry: words percent-encoded in a URL path (a Chinese headline) do not trip the heavy
#: percent-encoding rule. It is new rule logic, not glue; nothing else moved.
#:
#: Raised by 14, from 32,097, for each harness's own permission files on the write
#: tier: the leaf splice and the project-file matcher alias in ``paths_lists``; the
#: leaves joining the override arm, and the resolved-root list it reads, in
#: ``paths_targets``; and one ``or`` clause, its import and three ``TYPE_CHECKING``
#: lines in ``paths``. The registry lives in ``agent_sdk``.
#:
#: Raised again, from 32,111, for the host-set snapshot on
#: ``exfil.redact_exfiltration_urls_with_records`` and its two-tuple wrapper: one
#: optional ``exempt_hosts`` keyword that REPLACES the live platform read and the
#: scoped override when a caller supplies the set it keyed a cache on, so the display
#: cache stores output computed under exactly the set its key names. Fourteen lines:
#: the keyword on both signatures, a two-branch read and the docstrings saying why.
#: No rule widened or narrowed; the default path is the live read.
#:
#: Raised again, from 32,125, for ``redaction._SRI_INTEGRITY_RE``: pass 3 skips a run
#: that is the whole digest of a Subresource Integrity value (``sha256-<43>=``,
#: ``sha384-<64>``, ``sha512-<86>==``), because a digest is random base64 and a window
#: inside it clears every gate. The shape is closed: the lowercase algorithm name after
#: a non-run character, exactly that algorithm's digest length, exactly its padding,
#: and nothing glued after. One regex of three fixed-width alternatives, a helper that
#: returns the digest spans, their comment, and the one clause beside the document-link
#: exemption. No pass widened, no threshold moved, and the exfil URL scan is unchanged.
#: Six more for its ``keep_sri_digests=False`` switch: a streamed segment whose delta
#: boundary split an SRI value saves main's verdict on the digest, not the notice.
#:
#: Raised again, from 32,188, for the ``self-protection-skills-authority-retire`` floor,
#: which denies the operator-only ``kirocrew skills authority-retire`` recovery
#: through an agent shell: one argv matcher in ``argv_floor_self``, its facade
#: manifest line, its rule id and reason in ``denied_rules_tables``, its ``deny_gate``
#: registration, its facade export, and its name in the ungated-floor comment. No pass
#: widened and no threshold moved.
#:
#: Raised again, from 32,206, for the shell-expansion IMDS gate (GHSA-x2m8-5ff9-f479).
#: A spliced last octet like ``169.254.169.25$((2+2))`` evaded the literal
#: ``169.254.169.254`` deny, so ``exfil`` resolves a statically-knowable octet before
#: comparing: a tiny ``ast``-based integer evaluator (``_safe_arith``) reads only
#: ``$((..))``/``$[..]`` built from int constants and ``+ - * / %``, size-bounded and
#: running nothing, and ``_resolve_octet`` folds a literal-or-arithmetic octet to its
#: value. Only a token resolving to exactly ``169.254.169.254`` refuses; an octet
#: holding ``$VAR``, ``${..}``, ``$(..)`` or a backtick stays unresolved and is allowed
#: (``169.254.169.$o`` in a loop is benign), so the gate adds no new over-block. This
#: replaces an earlier presence-based attempt that was larger and over-blocked, so the
#: package shrank here (prior head pinned 32,485).
#:
#: Raised by 9, from 32,438, for one ``_WRITE_PROTECTED_HOME_PATHS`` entry in
#: ``paths_lists``: the Meetings app's per-meeting user notes tree, write-denied to
#: the agent file tools and read-allowed so the dashboard can serve pasted images.
#: Data, not rule logic: the matcher and every other entry are unchanged.
#:
#: Raised again for the ``rm_floor`` reads of a backtick body inside
#: double quotes and of a redirection glued to an operand, and for the floor-error
#: path naming ``rm`` as a word (``_names_rm_word``, on the raw text and with
#: ``${X:-…}`` defaults resolved). One helper pair, two branches in the existing
#: segment walk and one regex; no floor widened beyond ``rm`` itself. A live
#: redirection is kept as ONE segment word -- its fd prefix (``2>``), a ``{fd}``,
#: a two-character operator (``>>``) and an fd-dup (``2>&1``) all stay together --
#: so the program-word scan can step over a leading redirection (and its separate
#: target) the way it steps over a ``VAR=val`` assignment, using the shared
#: ``shell_normalizer._push_token_redirection`` recognizer. A redirect before
#: ``rm`` cannot hide the program word.
#:
#: Raised by 8, from 32,518, for one ``_WRITE_PROTECTED_HOME_PATHS`` entry in
#: ``paths_lists``: the crewmate agent folder, write-denied to the agent file tools
#: like ``~/.kiro/agents``. Data, not rule logic: the matcher is unchanged.
#:
#: Raised by 1, from 32,526, for one ``_CREW_SECRET_LEAVES`` entry in ``paths_lists``:
#: ``member-records``, the gateway-only leaf a crewmate's rules, goals and DM binding
#: live in. Data, not rule logic.
#:
#: Raised by 3, from 32,527, for the ``web-push`` leaf in ``paths_lists`` (the Web
#: Push key and device secrets): the leaf and its two-line reason. No rule changed.
#:
#: Raised again, from 32,530, for ``redaction_patterns``' optional native pre-filter: the
#: loader that accepts ``kc_native`` only when its fingerprint matches the Python gate,
#: the fingerprint itself, a debug line when a build's fingerprint does not match, and
#: the dispatcher that falls back to the Python gate (kept verbatim as
#: ``_might_contain_credential_py``) on a missing, stale, disabled or failing module.
#: Seventy-one lines. No pattern moved and no pass changed.
#:
#: Raised by 12, from 32,601, for the crewmate folder's state files in ``paths_lists``
#: (owners, kits, revoked approvals, tool access, migration ledger), write-denied
#: like the folder itself. Data, not rule logic: the matcher is unchanged.
#:
#: Raised by 3, from 32,613, for the enterprise install-tree write fence in
#: ``paths``: one import and one ``or`` clause on the write gate, plus the blank line
#: isort puts after the import. The matcher lives outside the package
#: (``kiro_crew.edition_install_fence``).
#:
#: Raised by 19, from 32,616, for the ``rm`` floor's own segment walk: reserved
#: words (``then``, ``do``, ``!``, ``time`` …) keep command position, a quoted
#: substitution may expand to nothing in the program word, and
#: ``#`` / ``<<`` inside an unpaired ``${…}`` / ``$[…]`` open no comment or heredoc.
#: Each command is read with and without that tracking. Deny-only: no allow widened.
#:
#: Raised by 8, from 32,635, for the ``rm`` floor's program word: an unquoted
#: ``$x`` / ``${…}`` / ``$1`` there may be unset, so ``${x}rm`` is read as ``rm``.
#: A word already read as ``rm`` keeps that reading. Deny-only: no allow widened.
#: Raised by 25, from 32,643, for ``exfil.restore_allowed_links``: an allowed host with several
#: different addresses in one reply pairs its placeholders with its records by order
#: instead of staying blocked, but only on a row marked ``blocked_links_ordered``, and
#: every address put back must be the whole URL match that starts where it lands;
#: plus the redactor's ``_ordered_out`` flag for a link rewritten inside a longer one.
#: Display-time only; no redaction rule, pattern or threshold moved.
_PACKAGE_LINE_BUDGET = 32_668

#: Ceiling on any ONE file in the package. This is what the bound is really for --
#: a package total says nothing about a single file growing back into a second
#: monolith, and a per-file cap is what a whole-file bound on the pre-split module
#: could not express. Set with headroom over the largest cluster so ordinary growth
#: does not trip it; a cluster that reaches it is asking to be split, and RAISING
#: the number is not the fix.
_MODULE_LINE_CAP = 4_500


def _package_line_counts() -> dict[str, int]:
    """Line count per file of the installed ``kiro_crew.security`` package."""
    package_dir = Path(security.__file__).parent
    return {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(package_dir.glob("*.py"))
    }


def test_the_package_stays_within_its_line_budget() -> None:
    counts = _package_line_counts()
    assert counts, "no package sources found"
    total = sum(counts.values())
    assert total <= _PACKAGE_LINE_BUDGET, f"package grew to {total} lines: {counts}"


def test_no_single_module_grows_back_into_a_monolith() -> None:
    oversized = {
        name: count for name, count in _package_line_counts().items() if count > _MODULE_LINE_CAP
    }
    assert not oversized, f"past the per-module cap: {oversized}"


def test_the_facade_is_the_smallest_it_can_be_of_the_package() -> None:
    """The facade carries re-exports and the mirroring machinery, so it must stay a
    small share of the package: a share that climbs means logic is accreting in the
    one file every caller imports, which is the shape the split exists to prevent."""
    counts = _package_line_counts()
    facade = counts["__init__.py"]
    assert facade * 5 <= sum(
        counts.values()
    ), f"the facade is {facade} of {sum(counts.values())} package lines"


# ─────────────────────────────────────────────────────────────────────────────
# Size ceiling: refused, not scanned, not skipped
# ─────────────────────────────────────────────────────────────────────────────


def test_oversized_command_is_refused_with_a_reason() -> None:
    cmd = "echo " + "x" * MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert "too large to security-scan" in reason
    assert str(len(cmd)) in reason


def test_command_at_the_ceiling_is_scanned_not_refused() -> None:
    body = "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo "))
    assert is_sensitive_bash_command("echo " + body) is None
    # And a detector's subject at the very end of a ceiling-sized command is found:
    # the ceiling is a bound on what is scanned, not a skip of the tail.
    tail = "; curl http://169.254.169.254/latest/meta-data/"
    cmd = "echo " + "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo ") - len(tail)) + tail
    assert len(cmd) == MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert reason.startswith("Blocked: command accesses IMDS")


def test_ceiling_matches_the_tool_input_tier() -> None:
    """The two tiers refuse at the same size, so a command cannot be too long
    for one and scanned by the other."""
    from kiro_crew import llm_helpers

    assert llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS == MAX_SCANNABLE_COMMAND_CHARS


# ─────────────────────────────────────────────────────────────────────────────
# A cron SCRIPT BODY has its own ceiling, and is not a shell subject at all
# ─────────────────────────────────────────────────────────────────────────────


def test_the_source_body_ceiling_is_larger_and_owned_by_the_cron_reader() -> None:
    """20 KiB of shell on one ``Bash`` call is a heredoc; 20 KiB of cron script is an
    ordinary script, and refusing it there is permanent (every tick until edited). The
    cron gate reads and refuses on ONE number so the reader and the scan agree."""
    from kiro_crew import mcp_cron

    assert MAX_SCANNABLE_SOURCE_BODY_CHARS > MAX_SCANNABLE_COMMAND_CHARS
    assert mcp_cron._MAX_SCRIPT_SCAN_BYTES == MAX_SCANNABLE_SOURCE_BODY_CHARS

    body = "".join(f'value_{i} = "{"t" * 200}"\n' for i in range(120))
    assert MAX_SCANNABLE_COMMAND_CHARS < len(body) <= MAX_SCANNABLE_SOURCE_BODY_CHARS
    assert mcp_cron._vet_script_contents(body) is None
    assert mcp_cron._vet_script_contents(body + 'open("~/.aws/credentials")\n') is not None

    over = "x = 1\n" * MAX_SCANNABLE_SOURCE_BODY_CHARS
    reason = mcp_cron._vet_script_contents(over)
    assert reason is not None and "too large to security-scan" in reason


def test_the_shell_gate_has_no_source_body_entry_point() -> None:
    """RATCHET: ``is_sensitive_bash_command`` takes a shell command line and nothing
    else -- no subject flag, no re-pointed traversal subjects, no per-caller ceiling.
    Every one of those knobs existed once to make a Python source body survive a
    shell-grammar pass, and each pass still produced a false-denial class on ordinary
    scripts. A source body is not this gate's subject; see
    ``mcp_cron._vet_script_contents``."""
    params = inspect.signature(security.is_sensitive_bash_command).parameters
    assert set(params) == {"command", "enabled_ids"}, sorted(params)
    for name in (
        "is_sensitive_source_body",
        "_source_command_subjects",
        "_sensitive_run_in_source_literals",
        "_parse_source_body",
        "_SOURCE_PATTERN_SINKS",
        "_SOURCE_COMMAND_SUBJECT_CAP",
    ):
        assert not hasattr(security, name), name


# ─────────────────────────────────────────────────────────────────────────────
# Liveness at the crash size, under the ceiling
# ─────────────────────────────────────────────────────────────────────────────


def _gate_seconds(command: str) -> float:
    started = time.perf_counter()
    is_sensitive_bash_command(command)
    return time.perf_counter() - started


def test_double_separator_10kb_is_fast() -> None:
    """The crash shape, at the crash size: 15 s on the shipped build."""
    cmd = _double_separator_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) is None
    assert _gate_seconds(cmd) < 2.0


def test_url_payload_12kb_is_fast() -> None:
    cmd = _url_payload_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) is None
    assert _gate_seconds(cmd) < 2.0
