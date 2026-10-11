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
#: Raised again, from 28,551, for the Windows alias fold in ``paths.py``: one lexical
#: helper strips a local-drive namespace prefix and a default-stream suffix, and
#: ``_candidate_forms`` resolves the folded spelling while keeping the raw one as a
#: candidate. No target, no matching rule and no threshold moved.
#:
#: Re-pinned from 28,572 for the value-only span of the key-anchored branches in
#: ``redaction.py``: each of the four branches that begins at the key naming a
#: secret (the three AWS key-value forms and ``Authorization: Bearer``) exposes its
#: value as one named group, ``_credential_value_span`` redacts that group alone,
#: and the key survives. What the kept key costs in lines is the fixed point it
#: needs: the registered tags embedded in every value group as one atom, the
#: whole-value tag skip pass 1 shares with pass 4, the quoted-value boundary
#: (``_quoted_value_end``), the straddle clamp, the coalescing sweep in pass 4 and
#: the strict-prefix atom the streaming anchor carries, each with the comment
#: naming the egress case it closes. No branch widened, no pass added and no
#: threshold moved.
#:
#: Re-pinned from 28,925 for the tag skip's closed-quote condition in pass 1 of
#: ``redaction.py``: a value that is a registered tag is skipped only when the tag
#: is proven to fill its value -- unquoted, or quoted with the closing quote on
#: its line. A quote that never closes certifies nothing, and a quoted string
#: folds across a raw line break in YAML and the shell, so a tag ending such a
#: line is left as it is (the bytes are the tag) but WARNED, on every run, so an
#: author-written tag cannot silence ``decisions.gate.scrub_reason`` while a
#: continuation line stands. The scan stays line-bounded. Twenty-five lines, all
#: of them the condition and the comment naming the case; no branch widened.
#:
#: Re-pinned from 28,950 for three rules in ``redaction.py`` the server lanes asked
#: for on that head: a pass-1 claim that runs to the end of an unterminated quoted
#: line WRITES the closing quote, so the redactor's own output is a closed pair a
#: re-screen leaves alone in silence while an author-written tag inside an open
#: quote still warns; ``_value_is_credential_tag`` and ``_CREDENTIAL_TAG_ATOM`` read
#: a RUN of whole tags as one value (two credentials adjacent inside one value were
#: two tags that the next run mangled at the second tag's interior space); and
#: pass 4 coalesces a ``token=`` value fully covered by two or more claims into one
#: tag on the first run (``_covering_claims``), silently. Sixty-three lines, the
#: rules and the comments naming the shapes; no branch widened, no pass added.
#:
#: Re-pinned from 29,013 for the streaming discard in ``__init__.py``: the
#: token-parameter discard reads the batch grammar's value shape -- value-class
#: bytes and WHOLE registered tags -- instead of the plain class, and keeps a
#: strict tag prefix buffered at a chunk tail (``_trailing_tag_prefix``) both
#: when the fail-closed drop arms it and while it runs. A ``token=`` value made
#: of a run of whole tags past the 4096 ceiling armed the drop, and the next
#: chunk's completed tag ended the discard at its interior space, so the bytes
#: glued to the tag's ``]`` streamed anchor-less. Fifty-six lines, the helper,
#: the two call sites and the comments naming the shape; the value class, the
#: ceiling and the drop bound are unchanged.
#:
#: Re-pinned from 29,069 for the presence-only readers of the patterns. Once the
#: key survives redaction, the redactor's own output ``key=[REDACTED: credential]``
#: matches its key-anchored branch again, so every reader that only asked "does
#: the pattern match?" -- the ledger push gate over ``ledger.jsonl``, the deploy
#: and preview file scans, the exfil request gates, the hard URL regex -- called
#: cleaned text live (the ledger refused every push from its first redacted entry
#: on). ``redaction.py`` gains ``_match_is_live_credential`` (pass 1's own skip,
#: judged on the same extended span), the public ``credential_matches`` and
#: ``contains_credential`` the readers now go through, and routes
#: ``_contains_credential_pattern`` through them; ``exfil.py``'s
#: ``_HARD_CREDENTIAL_RE`` declines a value that is only a registered tag, built
#: from the registry; ``__init__.py`` and ``_exports.py`` list the two names.
#: Ninety-two lines, the rule, the two accessors, the lookahead and the comments
#: naming the readers; no pattern widened, no pass added. Then fifteen more for
#: `_quoted_value_end`: a DOUBLED quote inside a quoted value is an escaped
#: interior quote (YAML and SQL ``''``, CSV ``""``), never the close -- reading
#: the first of the pair as the close let the redactor's own first pass emit
#: ``key='[REDACTED: credential]''<s2>'`` and the second run skip the tag in
#: silence with ``<s2>`` standing unwarned. The pair is consumed as value
#: bytes; the condition and the paragraph naming the bypass.
#:
#: Re-pinned from 29,176 for the pair embedded in an enclosing string literal
#: and the stream hold that carries it. A ledger line ``json.dumps`` wrote,
#: persisted history or a serialized log carries a redacted pair's quotes
#: ESCAPED (``key=\"[REDACTED: credential]\"``), and a value class that admitted
#: the backslash read the one-byte ``\`` of that escaped quote as the value: the
#: presence check called the redactor's own stored output live (every later
#: ledger push refused) and a second run un-escaped the quote and broke the
#: enclosing document. ``redaction.py`` gains ``_LABEL_QUOTE`` (a label quote
#: bare or escaped, one atom for the four key-anchored branches and the label
#: rules), excludes the backslash from ``_AWS_VALUE_CLASS``, and reads
#: ``_quoted_value_end`` in the opener's encoding, returning where the claim
#: ends, whether it closed and the opener to write; ``exfil.py``'s hard floor
#: follows. Then the stream: a key-anchored pair has terminator bytes INSIDE it
#: (the whitespace after the separator, a space or a backslash inside a quoted
#: value), so the natural cut committed the label and the value streamed raw --
#: on the base commit too -- or committed a quoted head whose batch pass wrote
#: the close mid-value; ``_key_anchored_hold_start`` and ``_LABEL_TAIL`` pull
#: such a cut back to the pair's start (``__init__.py`` adds the WEAK hold beside
#: the Bearer anchor, whose quote slots take the escaped forms). The hard URL
#: floor's tag exemption is then judged against what CLOSES the value: behind an
#: opening quote the closing quote, not the class boundary -- a tag merely heading
#: a quoted value (``secretaccesskey="[REDACTED: credential] <secret>"``, percent-
#: encoded into a URL path under a lower-case key the canonical branches do not
#: read) was exempt at the tag's following space and the URL reached display
#: (``_NOT_A_REDACTION_TAG_QUOTED_VALUE``). Then seventeen more: the quoted
#: exemption captures its opener and requires the SAME quote after the tag run
#: (``_not_a_redaction_tag_quoted_value``, one named group per branch), and the
#: stream DROPS a key-anchored hold whose extent would exceed the hold-back cap
#: instead of flooring it -- the floor cut inside a token's run and streamed its
#: remainder anchor-less, where the natural cut never bisects a credential-class
#: run. Two hundred and fifty-seven lines in all: the atoms, the encoding-aware
#: scan, the hold predicate, the stream's guarded call, the quoted exemption, and
#: the paragraphs naming the defects; no pass added, no cap or drop rule changed.
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
#: Re-pinned from 29,433 for the hard URL floor's doubled-quote rule in ``exfil.py``:
#: the quoted tag exemption's close must not be followed by the same quote again,
#: because a doubled quote is an escaped interior quote to the redactor and so to
#: this floor -- one lookahead and the paragraph naming the bypass it closes. No
#: branch widened, no pass added and no threshold moved.
#:
#: Re-pinned from 29,440 for the escape pair as a value atom: ``_AWS_VALUE_CLASS``
#: in ``redaction.py`` reads ``\/``, ``\\`` and ``\u`` inside a key-anchored value
#: as the value's own bytes (PHP-style JSON writes every ``/`` of a base64 secret
#: as ``\/``; a class that stopped at the backslash left the rest raw), the hard
#: URL floor in ``exfil.py`` spells its value run and its unquoted tag exemption
#: from that atom, the stream's credential class admits the backslash so its
#: natural cut cannot bisect such a value, and the label tail holds a lone
#: backslash after an opening quote. The paragraphs naming the shapes; no branch
#: widened, no pass added and no threshold moved.
#:
#: Re-pinned from 29,468 for escaped whitespace heading a value (``_AWS_VALUE_HEAD``
#: in ``redaction.py``): a bounded run of ``\n`` ``\r`` ``\t`` ``\f`` ``\v`` pairs
#: before the first value atom is leading whitespace in the value's encoding and
#: is consumed with it -- a head nothing admitted left a JSON document's value
#: standing behind one ``\n``, and percent-encoded into a URL path it passed the
#: exfil floor silently. The hard floor reads the same head; the label tail holds
#: through it. The atom and the paragraph naming the bypass; no branch widened,
#: no pass added and no threshold moved.
#:
#: Re-pinned from 29,498 for the value SCANNER (``scan_keyed_value``,
#: ``redaction.py``): the regex value grammar of the three AWS key-anchored
#: branches -- the value atom, the escaped-whitespace head, the quoted-value
#: scan, the label tail -- is replaced by one explicit tokenizer (the opener,
#: the head, the tag run, the escape pair, the doubled quote and the line end
#: as its stopping rules), and the hard URL floor in ``exfil.py`` reads its
#: labelled keys through the same scanner instead of a hand-mirrored regex. The
#: regex that stood there drew a real finding on four consecutive review
#: rounds; the scanner's docstring carries the rules, and the net package
#: change is +39 lines: 414 added, 369 removed. No branch widened, no pass
#: added and no threshold moved.
#: Re-pinned from 29,537 for the per-traversal reader of key-anchored values in
#: ``redaction`` (``_KeyedValueScans``): a key repeated as its own value made every
#: anchor after the first rescan the first value's run before the coverage check,
#: quadratic in the text (5,000 keys, 80 KB: 73 s in pass 1, past the 25 s watchdog
#: budget of the loop that runs it). The reader answers an anchor inside the last
#: unquoted run from that run's end -- the fresh scan's answer, byte for byte --
#: and pass 1, the stream hold and ``credential_matches`` read through it; the
#: hold also tests the remaining length before slicing the text for a strict tag
#: prefix. The class, its docstring and the three call sites are the growth.
#: Re-pinned from 29,590 for the enclosing-close rule of the quoted scan in
#: ``redaction``: a value opened by a BARE quote inside a literal of the other
#: quote kind (``{"text":"key='<v>","keep":1}``) ran past the enclosing close to
#: the line's end and deleted the sibling field; a quote of the other kind
#: followed by a structural byte or whitespace now ends the inner line, with the
#: follower set and the docstring rule as the growth.
#: Re-pinned from 29,613 for the stream hold's strict-tag-prefix test in
#: ``redaction`` (``_value_ends_in_a_tag_prefix``): it judged the buffer's tail
#: from the value's start only, so a tag cut off after whole tags
#: (``key=<tag>[REDACTED: ``) released the key's hold and the next chunk's
#: glued token streamed raw; the bounded scan over the last tag-length bytes is
#: the growth.
#: Re-pinned from 29,628 for the enclosing-close rule read by the opener's kind
#: in ``redaction`` (``scan_keyed_value``, ``_own_close_ahead``): a symmetric rule
#: reads an apostrophe before whitespace or punctuation inside a ``"``-opened
#: value (``{"SessionToken": " note' suffix", "keep": 1}``) as an enclosing close
#: and leaves the JSON view unparseable. Inside a ``'``-opened
#: value a bare ``"`` there stays the enclosing ``"`` string's close; inside a
#: ``"``-opened value a ``'`` is a byte of the value while the value's own close
#: is still to come on its line, read once per scan; the escaped encoding reads
#: another kind of quote as interior. The stream hold also judges a pulled-back
#: cut against every pair it would split. The look-ahead, its docstring and the
#: hold's second pass are the growth.
#: Re-pinned from 29,720 for the LOOK-BACK in ``redaction`` (``_advance_line_state``,
#: ``_enclosing_at``, ``_innermost``, the enclosing-aware ``_inner_token`` and the
#: carried state in ``StreamRedactor``): the scanner knows which string literal
#: encloses the key -- read back along the line, the outer literal's quote and
#: the inner literal an escaped quote delimits -- so the literal's own close is
#: never read as the value's opener (``{"template":"key=","keep":1}`` broke the
#: JSON view), the value ends at the literal's close and no further, and outside
#: a literal a quote of the other kind is a byte of the value. It REPLACES the
#: enclosing-close follower rule and the own-close look-ahead of the two rounds
#: before it (``_ENCLOSING_CLOSE_FOLLOWERS``, ``_own_close_ahead``, deleted). The
#: stream carries the line's quote state across its commits so a piece beginning
#: inside a literal reads as the whole text does. The state machine, its
#: docstrings and the stream's three bookkeeping lines are the growth.
#: Re-pinned from 29,824 to 29,858 (+34) for two rules of the same scanner.
#: ``redaction.py`` +15: escapes pair up at the value's own depth, so after the
#: escaped encoding's inner backslash a bare quote is the enclosing literal's
#: close and never the escaped token (the claim took the close with it and
#: ``{"text": "\"key\": \"x\\", "keep": 1}`` stopped parsing); the rule's branch
#: and its comment add 17 lines, and the unquoted path's lone-backslash-at-end
#: check, unreachable behind the ``partial`` token, gives 2 back. ``exfil.py``
#: +19: the hard credential floor reads the labels of a line with the line's
#: quote state carried from one to the next (``_KeyedValueScans``) instead of
#: walking the line back from its start for every label, which cost N walks for
#: N labels; the carrier import, the floor's loop and its docstring are the 19.
#:
#: Re-pinned from 29,858 to 29,898 (+40), all in ``redaction.py``. The tokenizer
#: reads an escape pair as ONE token before any delimiter test whenever the pair
#: is in an escaped encoding, the value's own or a backslash-escaping enclosing
#: literal's (+9 with its docstring: a bare ``'`` value inside ``"..."`` read
#: ``\\`` as two backslashes and the second with the quote after it as the inner
#: literal's close). The scanner's three rules from the document sweep (+31): a
#: doubled quote of the enclosing kind where the value would open is the value's
#: own quote and the same pair its close (a YAML single-quoted scalar's ``''``),
#: ``]`` where a value would start is no value, and a bare quote of the enclosing
#: kind after a backslash is that literal's close in every encoding. The
#: key-anchored patterns keep the base's separator whitespace, ``\s*`` on both
#: sides, so detection across a line break is the base's. No target, no matching
#: rule and no threshold moved.
#:
#: Re-pinned from 29,898 to 29,930 (+32), all in ``redaction.py``: a structural
#: byte (``,``, ``}``, ``]``) where a value would start opens no value only when a
#: terminator, whitespace, a quote or the text's end follows it at once, read as
#: the inner token (``_no_value_opens_at``, +20 with its reason), and is pending at
#: the text's end so the stream holds (+4); the value's head consumes the enclosing
#: encoding's escaped whitespace (+4); the standalone presence scanner starts from
#: a fresh line start, never the carried stream state (+6 with the reason). No
#: target, no matching rule and no threshold moved. The pin is 29,989: that chain
#: plus the two paragraphs above that this branch merges, the 10 lines of
#: ``StreamRedactor``'s ``held`` and ``discarding`` properties (from 28,572) and the
#: 49 lines of the routed-commit ceiling (from 28,582).
#:
#: Re-pinned from 29,989 to 29,991 (+2), in ``redaction.py``: the value's head
#: consumes the enclosing encoding's escaped LINE BREAK (``\\n``, ``\\r`` inside a
#: JSON string, one ``break`` token) as it consumes its escaped tab; read as the
#: value's end instead, the whole value stood behind it in plaintext while the
#: base redacted it. One token kind added to the head's test and the two
#: docstring lines naming it. No target, no matching rule and no threshold moved.
#:
#: Re-pinned from 29,991 to 29,993 (+2), in ``redaction.py``: the bare-backslash
#: branch of the unquoted scan ends the value at a quote or raw whitespace after
#: the backslash and at nothing else, so the two-byte spelling ``\\n`` a
#: percent-decoded URL path carries is the value's bytes, as the base's grammar
#: reads it; read as a line break, the value stopped before it, a tag ahead stood
#: exempt and the secret behind passed every floor. One test narrowed, the comment
#: naming the rule grown by two lines net. No target, no matching rule and no
#: threshold moved. The pin is 30,102: that chain plus the 34 lines of the
#: ``-d @`` carve-out in ``exfil.py`` (from 28,631 to 28,665) and the 75 lines
#: of the PlantUML route in ``redaction.py`` (from 28,665 to 28,740), the two
#: paragraphs above, which this branch merges.
#:
#: Re-pinned from 30,102 to 30,148 (+46), in ``redaction.py``: a structural byte
#: where an unquoted value would start heads a PREFIX run (``_prefix_run_end``:
#: structural bytes, backslash pairs, the enclosing encoding's escaped whitespace)
#: read whole, and the token after the run decides whether a value opens; judged
#: one byte at a time, ``]`` after ``]`` read as nothing value-like and
#: ``key=]]<secret>`` stood in plaintext while the base's class, which admits
#: ``]`` and a backslash, redacted it. The helper, its docstring, the judge's
#: widened docstring and the loop's comment are the growth. No target, no
#: matching rule and no threshold moved. The pin is 30,149: that chain plus the
#: one import line of ``atomic_write.replace_with_retry`` in ``redaction_allow.py``
#: (from 28,740 to 28,741, the paragraph above), which this branch merges.
#:
#: Re-pinned from 30,149 to 30,155 (+6), in ``redaction.py``: the nested pass over
#: a decoded PlantUML diagram (``_plantuml_verdict``) reads the source from a fresh
#: line start, never from the line state the stream carries for the text around
#: the link; under a carried ``'`` the quote opening the source's value read as
#: that literal's close and a diagram the batch pass masked streamed out. The
#: set and reset of the carrier and the comment naming the rule are the growth.
#: No target, no matching rule and no threshold moved.
_PACKAGE_LINE_BUDGET = 30_155

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
