"""Opt-in recorder for raw inbound ACP frames — a no-op unless switched on.

Set ``KIROCREW_ACP_RECORD_FRAMES`` to a directory and every agent->client
JSON-RPC frame the two transports read is appended, one JSON object per line, to
``<dir>/<backend>.jsonl``. Unset — which is every ordinary run, and every CI run
— :func:`record_frame` returns after one environment lookup and touches nothing.

Why it exists: the replay corpus under ``test/fixtures/acp_frames/`` is the
behavioural gate that stops a refactor of the dispatch layer silently changing
what a backend's frames become, and a corpus is only worth trusting if a human
can regenerate it from a real backend rather than hand-writing what they assume
the wire looks like. The corpus README (``test/fixtures/acp_frames/README.md``,
introduced with the corpus) documents the recording procedure and the review a
recording must pass before it is committed.

Four properties this module owes the reader loops that call it, because it sits
on the hot path of every frame of every session:

* **It never waits on the reader loop.** :func:`record_frame` puts the frame on
  ONE process-wide, thread-safe, bounded queue and returns; a single daemon
  thread drains it in arrival order. One queue and one writer for the whole
  process, not one per event loop: the two transports read on different
  threads and loops (``AcpRuntime`` on the gateway loop, ``AcpClient`` on a
  worker's), and a writer bound to "the loop that first recorded" would be
  replaced each time the other loop recorded, leaving several writers
  appending to the same file concurrently and each holding its own copy of
  the byte budget. So neither a slow disk nor a wedged executor can hold a
  frame back from routing — a reader loop that waited on either would stall
  every multiplexed session until the liveness watchdog ended the process.
  The writer is a dedicated thread rather than a job on the shared
  ``subprocess_executor``: the recorder must not compete with, or be starved
  by, the pool that runs tool subprocesses. It is started by
  :func:`start_recorder` at import time, on the importing thread, and never
  from a reader loop: ``Thread.start()`` waits for the OS to schedule the new
  thread, which is unbounded on a starved host, and every lazy scheme
  (``to_thread``, a private bootstrap pool) still paid one such start on the
  loop. If the switch is set after import without ``start_recorder`` being
  called, recording stands down with a line saying so. The queue is bounded in BYTES as well
  as frames: a frame may be up to the transports' 10 MiB line limit, so a count
  alone would let a burst of large frames hold gigabytes of parsed JSON behind
  one writer. A full queue drops the frame and stands recording down rather
  than blocking or growing.
  The price of never waiting is that the recording is not durable across a
  hard exit. A frame still queued when the process ends is lost, and a process
  ending in the middle of an append (one open/append/close per frame) can
  leave a torn last line. Both cost the last few milliseconds of a session, and
  a human regenerating a fixture stops the gateway after the scenario is done;
  the README's review step is what catches a torn tail before it is committed.
  Nothing in the gateway's shutdown path awaits this module, by design:
  recording is a development aid and must never add a step to shutdown.
* **It never raises.** A recorder fault must not reach a reader loop: in
  ``AcpRuntime._reader_loop`` an exception tears down EVERY multiplexed session
  on that process, which a user sees as an unexplained chat failure. Every
  failure mode degrades to one log line and a permanent stand-down, after which
  anything still queued is discarded unwritten — one failure costs one line,
  not one per pending frame.
* **It redacts before it writes.** Frames carry tool output, prompts and
  transcripts. Recording runs every string leaf (keys and values, at any
  depth) through the same
  :func:`kiro_crew.acp._dispatch.redact_text` the dashboard path runs, plus a
  home-directory scrub, BEFORE serialization so redaction can never eat JSON
  syntax and every recorded line parses. An accidental commit of a raw
  recording is then not an
  accidental commit of a credential. That is a floor and not a guarantee — see
  the README's review step, which is what actually keeps account ids and
  private paths out of the corpus.
* **The recording is owner-only on disk, and lands where the operator said.**
  Redaction is best-effort, and a recording sits on a developer machine for as
  long as they take to review it. On POSIX the destination is PINNED by
  :mod:`kiro_crew.pinned_fs`, the one home for that mechanism: the parent
  chain is resolved once, every component is opened relative to the previous
  descriptor with ``O_DIRECTORY | O_NOFOLLOW``, the leaf is created (0o700)
  through that descriptor, and the file is opened relative to it with
  ``O_NOFOLLOW`` and tightened (``fchmod`` 0o600) on its own descriptor.
  A path that already crosses a link is refused outright (the recording must
  land where the env var reads, not where a link points), and nothing on the
  way to the frame is resolved by name through a link after that, so another
  local user who can write to an ancestor cannot redirect the recording by
  swapping a link in between check and use, and a planted symlink or hard
  link at the target is refused. Whatever the pinned descriptor lands on must
  then be owned by this user and owner-only in mode, or the recording stands
  down. The mode is
  not the whole story: a 0700 directory can still carry a POSIX ACL that grants
  another account, and a file created inside inherits the directory's default
  ACL, so both descriptors are also checked for an extended ACL
  (``system.posix_acl_access`` / ``_default`` xattrs) and refused if one is
  present. That check exists only on Linux; macOS keeps ACLs behind
  ``acl_get_file`` with no xattr view, so a macOS process cannot prove the
  recording is owner-only and stands down like Windows does. The recorder
  is Linux-only for exactly this reason: Windows has neither ``dir_fd`` nor
  ``O_NOFOLLOW``, its DACL lockdown is applied by path after the open, and an
  ancestor another account can rename in cannot be pinned, so every
  path-based check there leaves a swap window. Rather than ship a recorder
  whose owner-only guarantee holds on one platform and is best-effort on the
  other, a Windows process with the env var set stands down once with a log
  line naming the reason. The corpus is regenerated on a POSIX host.
"""

from __future__ import annotations

import asyncio
import collections
import errno
import functools
import json
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND

logger = logging.getLogger(__name__)

#: Directory to append recordings to. Absent or blank disables recording.
ENV_RECORD_FRAMES = "KIROCREW_ACP_RECORD_FRAMES"

#: Frames that may wait for the writer before the recorder gives up. A frame is
#: normally a few KB and the writer keeps up with any disk that is not failing,
#: so this is a stand-down trigger for a wedged destination, not a normal
#: buffer depth.
QUEUE_LIMIT = 1024

#: Wire bytes that may wait for the writer. The transports cap a single frame at
#: 10 MiB, so a count limit alone would admit a burst of large frames that holds
#: gigabytes of parsed JSON on the heap behind one writer; this bounds the
#: backlog to a few such frames regardless of how many small ones fit.
QUEUE_BYTES_LIMIT = 32 * 1024 * 1024

#: POSIX mode bits the recording is held at.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: Set once when recording has failed, so a broken destination costs one log
#: line rather than one per frame for the life of the process.
_stood_down = False
_stand_down_lock = threading.Lock()
#: A stand-down latched ON A READER LOOP stores its reason here instead of
#: logging: ``logger.warning`` takes the handler lock and runs the handlers
#: synchronously, which is a wait the reader loop must not pay. The notifier
#: thread emits it (:func:`_emit_pending_stand_down`) -- not the file writer,
#: which may be wedged on the very fault being reported.
_pending_stand_down: BaseException | None = None


class _Writer:
    """The one process-wide queue, its byte budget, and the thread draining it.

    The reader side of this object is LOCK-FREE. A reader loop must never wait
    on a mutex another thread may be holding while preempted, and every
    stdlib queue takes one (``queue.Queue.mutex`` inside ``put_nowait``). So the
    backlog is a ``collections.deque``, whose ``popleft`` is a single bytecode
    under the GIL and needs no lock on the drain side; the two counters that
    bound it (``queued`` frames, ``queued_bytes``) and the reader's ``append``
    are guarded by ``lock`` -- which the reader only ever tries with
    ``acquire(blocking=False)``.
    ``queued_bytes`` is the backlog, not a lifetime total: the drain thread
    releases a frame's bytes once it has been written or discarded.
    ``in_flight`` is the frame the drain thread has popped but not yet finished;
    it exists so the test-only flush can tell "empty" from "done".
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: collections.deque = collections.deque()
        self.queued = 0
        self.queued_bytes = 0
        self.in_flight = 0
        #: Set by the test-only stop path. The drain thread waits on this with
        #: a timeout so it notices the stop even when the backlog is empty, and
        #: discards rather than writes once it is set, so teardown never waits
        #: on a backlog.
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=_drain, args=(self,), name="acp-frame-recorder", daemon=True
        )
        #: A second thread whose only job is to emit a stand-down that was
        #: latched on a reader loop. It is NOT the file writer: a writer wedged
        #: in a hung NFS/FUSE ``write`` never returns to its loop, and the one
        #: warning that says "frames are being dropped" would wait behind it
        #: forever. This thread touches no file and cannot wedge.
        self.notifier = threading.Thread(
            target=_notify, args=(self,), name="acp-frame-recorder-notify", daemon=True
        )
        self.thread.start()
        try:
            self.notifier.start()
        except BaseException:
            # The writer is already running; without this the only handle to
            # stop it would be lost with the failed constructor, leaving an
            # idle daemon thread behind for the life of the process.
            self.stop.set()
            self.thread.join(10)
            raise


#: ``None`` until :func:`start_recorder` runs. The writer is started at
#: process start (import time, when the env var is set), never from a reader
#: loop: ``Thread.start()`` waits for the OS to schedule the new thread, which
#: is unbounded on a starved host, and no lazy scheme avoids paying that on
#: the loop at least once. A stopped-then-restarted writer is a test-only path.
_writer: _Writer | None = None
_writer_lock = threading.Lock()


def fixture_dir_name(backend: str) -> str:
    """Return the corpus file stem for a backend id.

    The kiro-cli backend's id is the empty string, which is not a filename, so
    the mapping cannot be identity. It reuses ``POLICY_ID_BY_BACKEND`` — the
    module that already had to give every backend id a writable wire name —
    rather than introducing a second table that could disagree with it. The
    reader loops only ever pass an id from ``ACP_BACKENDS_KNOWN`` (construction
    rejects anything else), so a ``KeyError`` here is a programming error and is
    caught by :func:`write_frame`'s stand-down like any other recorder fault.
    """
    return POLICY_ID_BY_BACKEND[backend]


def recording_destination() -> str:
    """Return the recording directory, or ``""`` when recording is off.

    The whole cost of recording being off: one environment lookup. Read on every
    call rather than cached so there is no stale-state path to reason about.
    """
    if _stood_down:
        return ""
    return os.environ.get(ENV_RECORD_FRAMES, "").strip()


# A dict key whose VALUE is a credential by construction, whatever the value
# looks like. The pattern redactor knows credential SHAPES (an AWS key id, a
# ``Bearer`` token, a PEM block); it has no branch for ``Authorization: Basic
# <base64>`` (reversible: it IS the password), ``Proxy-Authorization``, a
# ``Cookie`` header, an ``api_key`` field, or a ``password`` field holding an
# arbitrary string. Under one of these keys the value is redacted unconditionally
# -- the key NAME is the evidence, not the value's shape. Matched against every
# ancestor key of a leaf, so ``{"Authorization": {"value": ...}}`` and
# ``{"headers": {"Cookie": [...]}}`` are both covered. The key is CANONICALISED
# first -- lower-cased, with every ``-``, ``_`` and space removed -- so one
# spelling here covers ``Authorization`` (RFC 9110 5.1: header names are
# case-insensitive), ``api_key``/``api-key``/``apiKey``/``X-Api-Key``, and
# ``accessToken``/``access_token``/``_authToken`` alike. The pattern below is
# written against that canonical form: no separators, no capitals.
#
# It is a SUFFIX rule, not a list of known prefixes: ``slackbottoken``,
# ``githubenterprisetoken``, ``xinternalsecret`` and ``dbpassword`` all end in
# a credential noun, and a prefix allowlist would have to be extended for every
# new integration -- each omission a leak. The one family that shares a suffix
# and is NOT a credential is the usage COUNTER: ``inputTokens``, ``max_tokens``,
# ``context_window_tokens``. Those are plural, so the singular suffix ``token``
# excludes them; the singular counters that do exist are named explicitly in
# :data:`_NOT_CREDENTIAL_KEY_RE` (``maxtoken``, ``nexttoken`` -- a pagination
# cursor, which is not secret).
_CREDENTIAL_KEY_RE = re.compile(
    r"(?:"
    r"authorization"
    r"|cookie"
    r"|apikey"
    r"|token"
    r"|secret|secretkey|privatekey"
    r"|password|passwd|passphrase|pwd"
    r"|credentials?"
    # One-time and verification codes: short, often all digits, and live for
    # minutes -- a recording is the one place they must not land. ``code`` on
    # its own is NOT here (an HTTP status code, an exit code, a language code
    # are all ``code``); the qualified forms are.
    r"|otp|totp|hotp|pin|pincode|mfacode|2facode|verificationcode|authcode"
    r"|authorizationcode|securitycode|accesscode|recoverycode|backupcode"
    # ``auth`` as a suffix: a bare ``auth`` field is a credential blob (npm's
    # ``_auth`` is ``base64(user:password)``, a requests-style ``auth`` pair),
    # and so are ``basic_auth``, ``proxyAuth``, ``registryAuth``. The names
    # that END in ``auth`` and are NOT blobs are exempted by exact name in
    # :data:`_NOT_CREDENTIAL_KEY_RE`: ``oauth`` is a configuration block whose
    # ``clientId``/``issuer``/``scopes`` a replay needs.
    r"|auth"
    r")$"
)

# Canonical key names that END in a credential noun but hold nothing secret.
# ``nexttoken`` / ``continuationtoken`` / ``pagetoken`` are pagination cursors,
# ``maxtoken`` / ``endoftexttoken`` are model settings, ``tokenizer`` never
# reaches the suffix rule (it does not end in ``token``).
_NOT_CREDENTIAL_KEY_RE = re.compile(
    r"^(?:"
    r"(?:next|continuation|page|pagination|max|min|stop|eos|eot|bos|endoftext|pad|unk|start|end)token"
    # OAuth is a configuration block, not a blob; ``preauth``/``reauth`` are
    # flags or phases. Anything else ending in ``auth`` is a credential.
    r"|oauth|oauth2|preauth|reauth|noauth|requiresauth|needsauth|hasauth|useauth|skipauth"
    r")$"
)

_KEY_SEPARATORS = str.maketrans("", "", "-_ ")

#: Fields whose STRING VALUE names a sibling field. A header list is often
#: serialised as ``[{"name": "Authorization", "value": "Basic ..."}]``, so the
#: credential key is a value, not a key, and the walk would otherwise judge
#: ``value: Basic ...`` with no header context. When one of these fields holds a
#: credential-bearing name, that name joins the key context for the dict's
#: OTHER leaves (never for the name field itself, which must survive).
_NAME_FIELDS = frozenset({"name", "key", "header", "field", "param", "parameter", "label"})

#: The sibling fields a borrowed credential name applies to: the ones that
#: carry the header's VALUE. ``enabled``, ``source``, ``description`` next to
#: a ``name: Authorization`` are metadata, not the secret, and must survive.
_VALUE_FIELDS = frozenset({"value", "values", "val", "content", "data", "secret", "default"})


def _is_name_field(key: str) -> bool:
    return key.translate(_KEY_SEPARATORS).lower() in _NAME_FIELDS


def _is_value_field(key: str) -> bool:
    return key.translate(_KEY_SEPARATORS).lower() in _VALUE_FIELDS


def _is_credential_key(key: str) -> bool:
    """True when *key* names a field whose value is a credential by definition.

    Matched on the canonical form (see :data:`_CREDENTIAL_KEY_RE`), so
    ``accessToken``, ``access_token``, ``ACCESS-TOKEN``, ``_accessToken`` and
    ``SLACK_BOT_TOKEN`` are one rule; ``inputTokens`` (plural, a count) and
    ``NextToken`` (a cursor) are not credentials.
    """
    canonical = key.translate(_KEY_SEPARATORS).lower()
    if _NOT_CREDENTIAL_KEY_RE.match(canonical):
        return False
    return _CREDENTIAL_KEY_RE.search(canonical) is not None


@functools.lru_cache(maxsize=4)
def _home_pattern(home: str) -> re.Pattern[str]:
    """``$HOME`` followed by a path separator or the end of the string, never mid-name."""
    return re.compile(re.escape(home.rstrip("/")) + r"(?=/|$)")


def _scrub_string(text: str, home: str) -> str:
    """Redact one string leaf: credential SHAPES, then the recording user's $HOME.

    Free text (a tool result, a curl transcript) is scrubbed by the shared
    ``redact_text`` shape redactor only. The key-NAME rule in ``_scrub_value``
    applies to structured keys, where the label is unambiguous; guessing labels
    inside prose is that redactor's job, not a second engine here.
    """
    text = redact_text(text)
    if home:
        # A recording made on a developer's machine otherwise carries their
        # login name in every file path a tool call touched. Only a whole
        # path component is replaced: with ``$HOME=/home/al`` a sibling user's
        # ``/home/alice/x`` must stay intact, not become ``~ice/x``.
        text = _home_pattern(home).sub("~", text)
    return text


def _scrub_keyed_string(keys: tuple[str, ...], value: str, home: str) -> str:
    """Redact a string that sits under dict keys, with each key as context.

    Two tests, in order. First, the key NAME: a value under ``Authorization``,
    ``Cookie``, ``password``, ``api_key`` (see :data:`_CREDENTIAL_KEY_RE`) is a
    credential whatever it looks like -- ``Authorization: Basic <base64>`` is
    the password, reversibly, and no shape detector recognises it -- so it is
    replaced outright. Second, the key as CONTEXT for a shape: a bare opaque
    ``Bearer …`` token is not a credential pattern, but ``Authorization: Bearer
    …`` is, so the pair is re-joined for the CHECK only. *keys* is every
    enclosing dict key from the outermost down, so ``{"Authorization":
    {"value": "Bearer …"}}`` is judged under ``Authorization`` as well as under
    ``value``; an inner key never hides an outer one. The keys themselves are
    never consumed, which is what keeps the output valid JSON.
    """
    for key in keys:
        if _is_credential_key(key):
            return "[REDACTED: credential]"
    for key in keys:
        joined = f"{key}: {value}"
        if redact_text(joined) != joined:
            return "[REDACTED: credential]"
    return _scrub_string(value, home)


#: What a dict KEY looks like when it is a field name rather than a secret
#: that was used as one: an identifier, optionally dashed, of modest length.
#: ``value``, ``Content-Type``, ``max_tokens`` pass; ``Basic dXNlcjpwYXNz``
#: (a space), ``eyJhbGciOi…`` (too long), ``dXNlcjpwYXNz==`` (``=``) do not.
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{0,63}\Z")


def _scrub_dict_key(keys: tuple[str, ...], key: str, home: str) -> str:
    """Redact a dict KEY that sits under the enclosing *keys*.

    A key is a string leaf too, and under a credential-named ancestor the
    secret may BE the key (``{"Authorization": {"Basic …": true}}``). The
    value rule cannot be applied verbatim, though: under ``Authorization`` a
    sub-key such as ``value`` is a field name, not the secret. So under a
    credential context a key is kept only when it looks like a field name
    (:data:`_FIELD_NAME_RE`); anything else there -- a blob with spaces, a
    base64 tail, an over-long token -- is replaced. Outside a credential
    context the key gets the same shape check as any value.
    """
    for ancestor in keys:
        if _is_credential_key(ancestor):
            if _FIELD_NAME_RE.match(key):
                break
            return "[REDACTED: credential]"
    for ancestor in keys:
        joined = f"{ancestor}: {key}"
        if redact_text(joined) != joined:
            return "[REDACTED: credential]"
    return _scrub_string(key, home)


def _scrub_value(value, home: str, keys: tuple[str, ...] = ()):
    """Walk a decoded JSON value and redact every string leaf, keys included.

    Structural, not textual: redacting the SERIALIZED frame let one credential
    pattern span a key, the quote, the colon and the value (``"Authorization":
    "Bearer …"`` collapsed to one ``[REDACTED]`` token), which leaves a line the
    corpus loader cannot parse. Per-leaf redaction cannot cross a JSON
    boundary, so the output is always valid JSON.

    *keys* is the chain of enclosing dict keys, outermost first, carried down
    through lists and nested dicts so that ``{"Authorization": ["Bearer …"]}``
    and ``{"Authorization": {"value": "Bearer …"}}`` are both judged with the
    header context; a bare token under a credential-shaped ancestor key is
    otherwise unrecognisable, and a nested dict's own key must add to that
    context, not replace it.
    """
    if isinstance(value, str):
        return _scrub_keyed_string(keys, value, home) if keys else _scrub_string(value, home)
    if isinstance(value, (int, float, bool)):
        # A numeric PIN, OTP or all-digit token under a credential key is as
        # much a secret as a string one; the type says nothing about the value.
        if any(_is_credential_key(k) for k in keys):
            return "[REDACTED: credential]"
        return value
    if isinstance(value, dict):
        # Name/value pairs: a ``name`` (or ``key``, ``header``, ...) whose
        # value names a credential lends that name to the sibling VALUE
        # fields, so ``{"name": "Authorization", "value": "Basic ..."}`` is
        # judged as ``Authorization: Basic ...``. Only the value-bearing
        # siblings borrow it: the name field itself is kept (the recording must
        # still say which header it was) and metadata such as ``enabled`` or
        # ``description`` is not the secret.
        named: tuple[str, ...] = ()
        for k, v in value.items():
            if (
                isinstance(k, str)
                and isinstance(v, str)
                and _is_name_field(k)
                and _is_credential_key(v)
            ):
                named = named + (v,)
        out: dict = {}
        # Next free ``#n`` suffix per scrubbed spelling, so a frame whose
        # every key collapses to one spelling (many secrets used as keys
        # under ``Authorization``) costs one probe per key, not a rescan of
        # every suffix already taken.
        next_suffix: dict = {}
        for k, v in value.items():
            # A key is a string leaf too: under an inherited credential
            # context (``{"Authorization": {"Basic ...": true}}``) the
            # credential may BE the key, so a key there is kept only when it
            # looks like a field name; ``value`` under ``Authorization`` is
            # a field name, ``Basic dXNl…`` under it is not.
            key = _scrub_dict_key(keys, k, home) if isinstance(k, str) else k
            if key in out:
                # Two distinct keys scrubbed to one spelling (``/home/al/x``
                # and a literal ``~/x``). Silently keeping the last would
                # drop a field from the recording; suffix instead so both
                # survive and the collision is visible in the corpus.
                base = key
                n = next_suffix.get(base, 2)
                while f"{base}#{n}" in out:
                    n += 1
                next_suffix[base] = n + 1
                key = f"{base}#{n}"
            own = (k,) if isinstance(k, str) else ()
            extra = named if (named and isinstance(k, str) and _is_value_field(k)) else ()
            out[key] = _scrub_value(v, home, keys + extra + own)
        return out
    if isinstance(value, list):
        # A two-item ``[name, value]`` pair is the other common header encoding
        # (``headers: [["Authorization", "Basic ..."]]``). When the first item
        # names a credential, the second is judged under it whatever its type
        # -- a container there (``["Authorization", ["Basic ..."]]``) is walked
        # with the name in its key chain, exactly as a dict value under a
        # credential key is; the first item is kept.
        if len(value) == 2 and isinstance(value[0], str) and _is_credential_key(value[0]):
            return [
                _scrub_value(value[0], home, keys),
                _scrub_value(value[1], home, keys + (value[0],)),
            ]
        return [_scrub_value(v, home, keys) for v in value]
    return value


def scrub_frame(frame: dict) -> str:
    """Serialize *frame* to one JSON line with credentials and $HOME removed.

    Every string in the frame — keys and values, at any depth — goes through
    :func:`kiro_crew.acp._dispatch.redact_text` before serialization, so a
    secret is caught wherever it sits (a nested tool result, a header map, a
    list of content blocks) without this module knowing the shape of every
    frame kind, and the line that lands on disk is always well-formed JSON.
    """
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        home = ""
    if home in ("/", ""):
        home = ""
    return json.dumps(_scrub_value(frame, home), ensure_ascii=False, sort_keys=True)


def _refuse_linked_component(directory: Path) -> None:
    """Refuse a destination whose lexical path and physical path disagree.

    :func:`kiro_crew.pinned_fs.pin_parent` resolves the parent once and pins
    from there, and documents the residual: a component that was ALREADY a
    link when the path was resolved is followed by that resolution (refusing
    it there would break ``/tmp`` on macOS). For a snapshot destination that
    is acceptable; for a recording the env var is the operator's statement of
    where transcripts will sit on disk, and a link already planted at
    ``<dest>/..`` by another local user would move them somewhere else while
    every descriptor check still passes -- the target can be a 0700 directory
    the same user owns. So before the pinned walk, the lexically-normalised
    path is compared with its physical resolution and any difference is
    refused, naming the first linked component so the operator knows what to
    change (on macOS ``/tmp`` and ``/var`` are OS-provided links, and the fix
    is the physical path). One comparison, not a walk: the open itself is
    still done by ``pinned_fs``, so a link swapped in AFTER this check is
    refused by ``O_NOFOLLOW`` there.
    """
    lexical = str(directory)
    if os.path.realpath(lexical) == lexical:
        return
    walked = ""
    for name in lexical.split(os.sep):
        if not name:
            continue
        walked = f"{walked}{os.sep}{name}"
        if os.path.islink(walked):
            break
    raise OSError(
        f"recording destination crosses a link at {walked}; "
        f"set {ENV_RECORD_FRAMES} to the physical path instead"
    )


def _pin_destination(directory: Path) -> int:
    """Open the destination directory as a descriptor, creating it if absent.

    A thin consumer of :mod:`kiro_crew.pinned_fs`, which owns the walk:
    :func:`kiro_crew.pinned_fs.pin_parent` opens the parent chain component by
    component with ``O_DIRECTORY | O_NOFOLLOW`` relative to the previous
    descriptor and translates ``ELOOP``/``ENOTDIR`` into one refusal; the leaf
    is then created (0o700) and opened through that pinned parent with the
    same flags, so a link or non-directory at the leaf's own name is refused
    too. ``pin_parent`` is handed the LEXICAL parent, not a re-resolved one:
    :func:`_refuse_linked_component` has just established that the lexical
    path and the physical path agree, and resolving again between that check
    and the walk would follow a link swapped in during the gap (review found
    exactly that window in a version that went through
    ``create_and_open_dir_pinned``, whose ``realpath`` is its own). ``..``
    and ``.`` are collapsed lexically first (``Path.absolute`` plus
    ``normpath``, never ``resolve``) so a ``..`` after a link cannot climb out
    through the link's target. Whatever the descriptor lands on must then be
    owned by this user, owner-only in mode, and free of an extended ACL
    (:func:`_require_owner_only_dir`); a leaf this run created is 0o700 by
    construction and passes the same check. A pre-existing directory is
    checked, never changed. The descriptor is returned; the caller owns it.
    """
    normalised = Path(os.path.normpath(directory.absolute()))
    _refuse_linked_component(normalised)
    parent_fd = pinned_fs.pin_parent(
        str(normalised.parent), what="recording destination", refusal=OSError
    )
    try:
        try:
            os.mkdir(normalised.name, DIR_MODE, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            fd = os.open(normalised.name, pinned_fs.dir_flags(), dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise OSError(
                    f"recording destination {directory} is a symbolic link or not a "
                    f"directory; set {ENV_RECORD_FRAMES} to a directory path"
                ) from exc
            raise
    finally:
        os.close(parent_fd)
    try:
        _require_owner_only_dir(fd, directory)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _require_owner_only_dir(fd: int, directory: Path) -> None:
    """Refuse a pre-existing destination that is not already owner-only.

    Tightening it here is what an earlier revision did, and that is the wrong
    side of the trade: the env var can name a directory the operator shares on
    purpose, and a recorder that ``chmod``s it to 0700 on the first frame locks
    every other intended user out with no undo. A directory this run did not
    create is therefore checked, never changed: it must be owned by this user
    and grant nothing to group or other. Owner-only is one ``chmod 700`` away
    for an operator who wants recordings there.
    """
    info = os.fstat(fd)
    if info.st_uid != platform_compat.local_user_id():
        raise OSError(f"recording destination is not owned by this user: {directory}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise OSError(
            f"recording destination {directory} is {oct(mode)}, not owner-only; "
            f"chmod 700 it, or set {ENV_RECORD_FRAMES} to a fresh path"
        )
    _require_no_extended_acl(fd, directory)


#: The xattrs under which Linux stores a POSIX ACL with named entries. A
#: minimal ACL (owner/group/other only, i.e. the mode) is NOT stored as an
#: xattr, so the presence of either name means an entry beyond the mode exists.
_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")


def _require_no_extended_acl(fd: int, path: Path) -> None:
    """Refuse a descriptor whose inode carries a POSIX ACL beyond its mode.

    ``mode & 0o077 == 0`` says nothing about a named ACL entry: ``setfacl -m
    u:other:rx`` on a 0700 directory leaves the mode bits reading owner-only
    while another account can list and read it, and a default ACL on the
    directory is inherited by every file created inside, ``fchmod`` 0600
    notwithstanding. Linux exposes both as xattrs, so the check is one
    ``flistxattr``. A platform without that view (macOS) is refused outright
    by :func:`_require_acl_inspectable`; this function is only reached where
    the answer can be known.
    """
    try:
        names = os.listxattr(fd)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return  # the filesystem has no xattrs at all, so it has no ACLs
        raise
    found = [n for n in _ACL_XATTRS if n in names]
    if found:
        raise OSError(
            f"recording destination {path} carries a POSIX ACL ({', '.join(found)}); "
            f"the mode is owner-only but the ACL may grant another account -- "
            f"setfacl -b it, or set {ENV_RECORD_FRAMES} to a fresh path"
        )


def _require_acl_inspectable() -> None:
    """Stand down where an owner-only guarantee cannot be verified.

    Only Linux exposes ACLs through the xattr API CPython binds; macOS stores
    them behind ``acl_get_file`` with no ``os`` binding, so a recorder there
    could pass every mode check while an inheritable ACL lets another account
    read every frame. Rather than a guarantee that holds on one platform and
    is a guess on another, the recorder refuses to run where it cannot check.
    """
    if not platform_compat.IS_LINUX or not hasattr(os, "listxattr"):
        raise OSError(
            f"{ENV_RECORD_FRAMES} is Linux-only: this platform has no way to "
            "verify the destination carries no ACL granting another account, "
            "so an owner-only recording cannot be guaranteed here"
        )


class _HardlinkRefused(Exception):
    """Raised by ``pinned_fs.refuse_hardlink_alias`` AFTER it has closed the fd.

    A private type so the caller can tell "the helper refused and already
    closed the descriptor" from "the helper failed and left it open" -- the two
    need opposite cleanup, and both must surface as an ``OSError`` stand-down.
    """


def _open_private_append(directory: Path, name: str):
    """Open ``<directory>/<name>`` for appending as a regular, owner-only file.

    The directory is pinned by :func:`_pin_destination`, and the file open is
    relative to that descriptor with ``O_NOFOLLOW``, so nothing on the way to
    the file is resolved by name through a link.

    The opened inode is then checked to be a regular file (a FIFO would wedge
    the writer) that still has a name (an unlinked inode would take the frame
    and lose it on close) and is owned by this user (the rule the directory is
    already held to: a foreign-owned file would be locked to its owner by the
    fchmod, not to us), refused if it is a hard-link alias
    (:func:`kiro_crew.pinned_fs.refuse_hardlink_alias` -- a planted link at
    ``<name>`` shares its inode with another file, which the append would
    otherwise write into), checked for an extended ACL, and locked to the
    owner on the DESCRIPTOR (``os.fchmod``) rather than by path, so the
    lockdown lands on the same inode the frame does.
    """
    if not platform_compat.IS_POSIX:
        raise OSError(
            f"{ENV_RECORD_FRAMES} is POSIX-only: Windows cannot pin the destination "
            "by descriptor, so an owner-only recording cannot be guaranteed there"
        )
    _require_acl_inspectable()
    path = directory / name
    # O_NONBLOCK: a FIFO planted at <name> would otherwise make the open block
    # until a reader appears, wedging the writer thread for the life of the
    # process. With it the open returns at once and the S_ISREG check below
    # refuses the FIFO. It has no effect on a regular file.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    dir_fd = _pin_destination(directory)
    try:
        fd = os.open(name, flags, FILE_MODE, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"recording target is not a regular file: {path}")
        if info.st_nlink == 0:
            # Unlinked between the open and here (a log rotator, a cleanup):
            # the append would succeed onto an inode with no name and vanish on
            # close. ``refuse_hardlink_alias`` only asks about MORE than one
            # name, so the zero case is this module's to refuse.
            raise OSError(f"recording target was removed before it could be written: {path}")
        if info.st_uid != platform_compat.local_user_id():
            # The same rule the directory is held to. An owner-only directory
            # can still hold a foreign-owned inode (a bind mount, a file left by
            # a privileged run), and fchmod 0600 on it would lock it to THAT
            # owner, who then reads every frame. Refused, and left as found.
            raise OSError(f"recording target is not owned by this user: {path}")
    except Exception:
        os.close(fd)
        raise
    # The helper closes the descriptor itself before raising its refusal, so
    # that one exception must not reach a second close. Anything ELSE it could
    # raise (its own fstat failing) leaves the descriptor open and is ours.
    try:
        pinned_fs.refuse_hardlink_alias(
            fd, what="recording target", name=str(path), refusal=_HardlinkRefused
        )
    except _HardlinkRefused as exc:
        raise OSError(str(exc)) from None
    except Exception:
        os.close(fd)
        raise
    try:
        # Checked BEFORE fchmod: on a file that carries an ACL, fchmod rewrites
        # the ACL mask entry, so tightening first would alter a file this run
        # is about to refuse. A refused file is left exactly as it was found.
        _require_no_extended_acl(fd, path)
        os.fchmod(fd, FILE_MODE)
    except Exception:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8")


def write_frame(backend: str, frame: dict, dest: str) -> None:
    """Append one scrubbed frame to ``<dest>/<backend>.jsonl``.

    Runs on a worker thread, never on the event loop. Swallows every failure:
    the caller is a transport reader whose job is the session, not the
    recording.
    """
    if not dest.strip():
        # Path("") resolves to the CWD, so a blank destination would append the
        # frame to ./<backend>.jsonl wherever the gateway happens to be running.
        # A blank destination means recording is off.
        return
    try:
        directory = Path(dest).expanduser()
        line = scrub_frame(frame)
        with _open_private_append(directory, f"{fixture_dir_name(backend)}.jsonl") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - a recorder must never take down a reader
        _stand_down(exc)


def _drain(writer: _Writer) -> None:
    """The writer thread: one frame at a time, in arrival order, off every loop.

    Once recording has stood down, whatever is still queued is discarded
    without being written: the stand-down already logged the failure, and
    retrying each pending frame against a destination known to be broken
    would emit up to ``QUEUE_LIMIT`` more warnings for one fault.
    """
    while True:
        try:
            item = writer.items.popleft()
        except IndexError:
            if writer.stop.wait(0.05):
                return
            continue
        # The drain thread MAY block on the lock: it is not a reader loop, and
        # the only other holders (readers) hold it for a few instructions and
        # never wait on anything while holding it. The frame count is released
        # at pop time -- the backlog is what waits, not what is being written --
        # and the bytes when the write is done, since the parsed JSON is held
        # on the heap until then.
        with writer.lock:
            writer.queued -= 1
            writer.in_flight += 1
        backend, frame, dest, size = item
        try:
            if not _stood_down and not writer.stop.is_set():
                write_frame(backend, frame, dest)
        except Exception as exc:  # noqa: BLE001 - the writer must outlive any one fault
            _stand_down(exc)
        finally:
            with writer.lock:
                writer.queued_bytes -= size
                writer.in_flight -= 1


def _notify(writer: _Writer) -> None:
    """The notifier thread: emit a reader-latched stand-down, off every loop.

    Separate from :func:`_drain` on purpose (see :class:`_Writer`): the
    warning must not depend on the file writer being able to return from a
    write. Wakes every 50 ms; the log line lands within that of the latch.
    """
    while not writer.stop.wait(0.05):
        _emit_pending_stand_down()
    _emit_pending_stand_down()


def start_recorder() -> bool:
    """Start the writer thread if recording is switched on. Idempotent.

    Called at import time — the gateway imports this module on the main thread
    before any event loop runs — so ``Thread.start()`` never executes on a
    reader loop. It is also the explicit entry point for a host that sets the
    env var after import (tests, an embedding process): call it from a plain
    thread before recording. Returns ``True`` when a writer is running.

    A start that fails (out of threads) stands recording down: the process
    keeps running, the recording does not happen, one log line says why.
    """
    global _writer
    if not recording_destination():
        return False
    if not platform_compat.IS_POSIX:
        # No thread is started on Windows: the recorder is POSIX-only (module
        # docstring), so stand down here, once, rather than start a writer
        # whose first write would refuse anyway.
        _stand_down(
            OSError(
                f"{ENV_RECORD_FRAMES} is POSIX-only: Windows cannot pin the destination "
                "by descriptor, so an owner-only recording cannot be guaranteed there"
            )
        )
        return False
    try:
        _require_acl_inspectable()
    except OSError as exc:
        # Same shape for macOS: no writer or notifier thread is started for a
        # recorder that cannot verify the destination carries no ACL.
        _stand_down(exc)
        return False
    with _writer_lock:
        if _writer is not None and _writer.thread.is_alive():
            return True
        try:
            _writer = _Writer()
        except Exception as exc:  # noqa: BLE001 - a recorder must never take down its host
            _stand_down(exc)
            return False
        return True


class _Overflow(RuntimeError):
    """The backlog is full (by count, by bytes, or its lock was busy)."""


def _enqueue(writer: _Writer, backend: str, frame: dict, dest: str, wire_bytes: int) -> None:
    """Hand a frame to *writer*, never blocking the caller.

    Thread-safe and loop-independent: both transports, on whichever threads
    and loops they read from, share the one backlog and the one byte budget.
    Overflow — by count or by bytes — means the writer has fallen far enough
    behind that the destination is effectively broken, so the frame is dropped
    and recording stands down.
    """
    # The two bounds are guarded by a lock the writer thread also takes, for a
    # few instructions per frame. A reader loop must not WAIT on it: if the
    # holder is preempted (a starved host, a GC pause) every session on this
    # loop would stall behind the recorder. Acquire without blocking; a busy
    # lock is treated like an overflow -- the frame is dropped and recording
    # stands down -- which is the same answer the module gives whenever the
    # writer cannot keep up. In practice the lock is free: two reader loops
    # and the drain thread contend only when a frame arrives on both
    # transports within the same microseconds. This ``acquire`` is the ONLY
    # synchronisation on the reader path, and it never blocks. The append
    # happens INSIDE the same critical section: admission to the budget and
    # admission to the backlog are one step, so two readers cannot pass the
    # check in one order and land in the deque in the other, which would put
    # the corpus out of arrival order.
    if not writer.lock.acquire(blocking=False):
        raise _Overflow("backlog lock busy; not waiting on a reader loop")
    try:
        if writer.queued + 1 > QUEUE_LIMIT:
            raise _Overflow(f"{writer.queued} frames queued reaches {QUEUE_LIMIT}")
        if writer.queued_bytes + wire_bytes > QUEUE_BYTES_LIMIT:
            raise _Overflow(
                f"{writer.queued_bytes + wire_bytes} bytes queued exceeds {QUEUE_BYTES_LIMIT}"
            )
        writer.queued += 1
        writer.queued_bytes += wire_bytes
        writer.items.append((backend, frame, dest, wire_bytes))
    finally:
        writer.lock.release()


async def record_frame(backend: str, frame: dict, wire_bytes: int = 0) -> None:
    """Record one inbound frame, if a recording directory is set.

    Returns after one environment lookup when ``KIROCREW_ACP_RECORD_FRAMES`` is
    unset, which is the state of every ordinary run. When it is set the frame is
    queued for the writer thread and this returns without awaiting anything
    else, so the reader loop never waits on the filesystem, on a thread
    start, or on a mutex: the writer was started by :func:`start_recorder`
    before any loop existed, and this function only ever does one
    non-blocking ``acquire`` plus a lock-free ``deque.append``.
    If the env var was set after import without :func:`start_recorder` being
    called, recording stands down with a line saying so rather than starting a
    thread from the loop.

    *wire_bytes* is the length of the line *frame* was parsed from. The reader
    loops already hold it, and it is what the byte bound counts — serializing
    the frame here to measure it would put a ``json.dumps`` of up to 10 MiB on
    the reader loop, which is the cost this module exists to avoid.
    """
    dest = recording_destination()
    if not dest:
        return
    writer = _writer
    if writer is None or not writer.thread.is_alive():
        # Misconfigured: the switch is set but no drain thread exists to carry
        # the log line off this loop. One line, once per process, logged here
        # directly -- the alternative is a recorder that silently never
        # records, and there is no other thread to hand the line to.
        _stand_down(
            RuntimeError(
                f"{ENV_RECORD_FRAMES} is set but the recorder was not started before the "
                "event loop; call kiro_crew.acp._frame_record.start_recorder() at startup"
            )
        )
        return
    try:
        _enqueue(writer, backend, frame, dest, wire_bytes)
    except Exception as exc:  # noqa: BLE001 - a full queue must not reach a reader
        _stand_down(exc, from_reader_loop=True)


def _stand_down(exc: BaseException, *, from_reader_loop: bool = False) -> None:
    """Disable recording for the rest of the process after one failure.

    One fault, one log line. Callers race: the writer thread (a failed write)
    and any reader thread (a queue overflow, which is what a failed write tends
    to cause next). The lock makes the first to arrive the one that logs --
    directly when the caller is off the loop, via the notifier thread when
    *from_reader_loop* is set;
    ``recording_destination`` returns ``""`` once the latch is set and
    ``_drain`` discards the backlog, so nothing else reaches here afterwards.
    """
    global _stood_down, _pending_stand_down
    # Non-blocking: a reader loop reaches here too (a dropped frame), and must
    # not wait behind another thread that is mid-latch. If the lock is busy,
    # someone else IS latching, and their log line is the one that matters.
    if not _stand_down_lock.acquire(blocking=False):
        return
    try:
        if _stood_down:
            return
        _stood_down = True
        if from_reader_loop:
            # Latch only. The log line is the notifier thread's job: emitting it
            # here would take logging's handler lock and run every handler on
            # the reader loop.
            _pending_stand_down = exc
            return
    finally:
        _stand_down_lock.release()
    _log_stand_down(exc)


def _log_stand_down(exc: BaseException) -> None:
    logger.warning(
        "%s is set but recording failed; frame recording is now off for this process: %s",
        ENV_RECORD_FRAMES,
        exc,
    )


def _emit_pending_stand_down() -> None:
    """Log a stand-down that was latched on a reader loop. Notifier thread only."""
    global _pending_stand_down
    # Unlocked peek first. The notifier polls every 50 ms; if it took the
    # latch lock on every poll, a reader-side ``_stand_down`` that happened to
    # coincide would find the lock busy, return without latching, and the
    # frame it was reporting would be dropped silently with recording still on.
    # The lock is only taken when there IS a reason to consume.
    if _pending_stand_down is None:
        return
    with _stand_down_lock:
        exc, _pending_stand_down = _pending_stand_down, None
    if exc is not None:
        _log_stand_down(exc)


async def flush_for_tests() -> None:
    """Wait for every queued frame to reach disk (or be discarded). For tests only."""
    writer = _writer
    if writer is None:
        return

    def _wait() -> None:
        while True:
            with writer.lock:
                if writer.queued == 0 and writer.in_flight == 0 and not writer.items:
                    return
            time.sleep(0.005)

    await asyncio.to_thread(_wait)


def _stop_writer(writer: _Writer) -> None:
    """Tell *writer*'s thread to exit and wait for it. Tests only.

    The drain thread waits on the stop event with a timeout, so it notices this
    whether the backlog is empty or full. The join is checked: a thread still alive
    afterwards means a wedged ``write_frame`` (a test's gate never released),
    and raising here is what turns "pytest later removes a directory the
    thread still writes into" into a failure at the test that caused it.
    """
    writer.stop.set()
    writer.thread.join(10)
    writer.notifier.join(10)
    if writer.thread.is_alive():
        raise RuntimeError("acp-frame-recorder writer thread did not stop within 10s")
    if writer.notifier.is_alive():
        raise RuntimeError("acp-frame-recorder notifier thread did not stop within 10s")


async def stop_for_tests() -> None:
    """Stop the writer thread and wait for it to exit. For tests only.

    A daemon thread would not stop the interpreter, but a test's temporary
    directory can be removed while the thread is still writing into it, so
    teardown waits for the thread to exit (and fails if it does not).
    """
    global _writer
    with _writer_lock:
        writer = _writer
    if writer is not None:
        # Stop BEFORE clearing: if the join fails and _stop_writer raises, the
        # writer stays referenced (and its thread visible) rather than being
        # dropped while still running.
        await asyncio.to_thread(_stop_writer, writer)
    with _writer_lock:
        if _writer is writer:
            _writer = None


def _reset_for_tests() -> None:
    """Clear the stand-down latch and drop the writer. For tests only.

    Synchronous, so it is usable from a sync fixture; see :func:`_stop_writer`
    for why the stop cannot block and why a thread that outlives the join is
    an error rather than a leak.
    """
    global _stood_down, _pending_stand_down, _writer
    _stood_down = False
    _pending_stand_down = None
    with _writer_lock:
        writer = _writer
    if writer is not None:
        _stop_writer(writer)  # raises, keeping _writer set, if the thread survives
    with _writer_lock:
        if _writer is writer:
            _writer = None


# Started here, on the importing thread, so that a gateway launched with the
# switch set never starts the writer from a reader loop (see start_recorder).
start_recorder()
