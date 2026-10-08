import { usePathKind, type PathKind } from '../../hooks/usePathKind'
import { WINDOWS_ABS_PATH_RE } from '../../utils/urlTransform'
import { revealOrOpen } from '../FilePathMenu'
import type { PathActions } from './contexts'

/**
 * Local filesystem references in rendered text: which strings are worth a stat
 * probe (`isPathCandidate`), how a `file:line` suffix splits (`splitLineRef`),
 * how both readings of a suffix resolve against the backend
 * (`usePathResolution`), and where a confirmed chip or link sends its
 * activation (`activatePath`). The inline-code chip and the markdown link share
 * all four, so a path cannot classify one way as a chip and another as a link.
 */

/**
 * Character-level shape of a local filesystem path: letters and digits in any
 * script (`\p{L}\p{N}` — filenames are not ASCII-only), combining marks
 * (`\p{M}` — macOS stores NFD-decomposed forms, and Indic/Thai/Arabic scripts
 * need marks even under NFC), underscore, dot, dash, @, ~, colon, space and
 * PARENTHESES, separated by slashes — EITHER kind, because a Windows gateway
 * names its files with `\`. Anchored at both ends, so anything carrying a URL
 * scheme (`https://…`) or shell punctuation fails outright.
 *
 * The punctuation set is a DECIDED boundary, not an accumulation. Two review
 * rounds each found one more character that is legal in a real filename —
 * parentheses (`C:\Program Files (x86)`, the most-trodden directory on Windows)
 * and then an apostrophe (`C:\Users\O'Neil`) — which is the signature of an
 * allowlist being discovered one bug report at a time. So the rule is stated once
 * instead: admit every character that is legal in a filename on BOTH platforms
 * and is not a shell control operator, on both shapes, since the two describe one
 * filesystem convention and an asymmetry is only a later bug report.
 *
 * IN: letters, marks, digits, `_ . @ ~ - space` and `' ! # % = + , ( ) [ ] { }`.
 * A closing bracket may also END a path, so `App (old)` and `data [2026]`
 * classify as directories.
 *
 * OUT, deliberately — these are what keep the anchored shape from matching a
 * command or a URL: `$` and a backtick (expansion), `&` `;` `|` (chaining),
 * `<` `>` (redirection), `"` (quoting), `?` `*` (globbing), and `:` anywhere but
 * the last segment, where it serves `file:447`. Windows forbids `< > : " / \ | ?
 * *` in a filename outright, so excluding them costs nothing there and buys the
 * prose rejection everywhere.
 *
 * Widening the repertoire never widens the positive-signal rule, so punctuated
 * prose (`foo/bar (baz)`, `a&&b/c.sh`) still carries neither a root nor an
 * extension and is still refused below.
 *
 * Admitting `\` as a separator here is what lets a relative Windows path
 * (`src\main.py`, `.\src\main.py`) reach the probe. It cannot express a
 * DRIVE-rooted path, whose colon sits before the first separator while this
 * shape allows a colon only in the last segment (where it serves `file:447`),
 * so that form has its own shape below.
 *
 * Shape alone is NOT sufficient to linkify — see `isPathCandidate`.
 */
const PATH_SHAPE_RE =
  /^~?(?:\.{0,2}[/\\])?[\p{L}\p{M}\p{N}_.@~'!#%=+,()[\]{}/\\ -]*[/\\][\p{L}\p{M}\p{N}_.@~'!#%=+,()[\]{}: -]*[\p{L}\p{M}\p{N}_.)\]}]$/u

/**
 * Character-level shape of a DRIVE-rooted Windows path (`C:\x`, `c:/x`), whose
 * root `PATH_SHAPE_RE` cannot carry: the colon precedes the first separator.
 *
 * The trailing segment may be empty so a bare drive root (`C:\`) — a real
 * directory the file manager can reveal — still classifies, and segments carry
 * the same repertoire `PATH_SHAPE_RE` allows, so both
 * `C:\Program Files (x86)\app.txt` and `C:\Users\O'Neil\notes.md` resolve.
 */
const WIN_DRIVE_PATH_SHAPE_RE =
  /^[A-Za-z]:[/\\](?:[\p{L}\p{M}\p{N}_.@~'!#%=+,()[\]{} -]+[/\\])*[\p{L}\p{M}\p{N}_.@~'!#%=+,()[\]{} -]*$/u

/**
 * A UNC prefix in EITHER spelling — `\\host\share\…` or `//host/share/…` —
 * refused outright below.
 *
 * NOT an oversight that the Windows support here stops at drive letters. A UNC
 * path names a HOST, and this pre-filter classifies markdown that may be
 * attacker-authored (a rendered web page, a quoted file, any untrusted text a
 * message carries), so admitting one would let that text make the dashboard ask
 * the gateway to stat `\\attacker.example\share\x`. On Windows that stat is an
 * outbound SMB connection, which offers the host's NTLM credentials — a
 * credential-leak vector, from nothing but rendering a message.
 *
 * Windows reads ANY two leading separators as a UNC root, of either kind and in
 * either order, so the character class is the whole point: matching two of the
 * SAME kind (`\\\\` or `//`) leaves `\\/attacker.example\\share\\x` and its `/\\`
 * mirror admitted, and those resolve to the same share. A mixed pair is the same
 * vector under a different coat of paint, and unlike the `//` spelling it is a
 * shape no pre-diff predicate here could even form.
 *
 * Three places in this codebase already hold exactly this line, and this is the
 * fourth: `WINDOWS_ABS_PATH_RE` (utils/urlTransform.ts) excludes UNC for image
 * `src` values, `MdAnchor` refuses a decoded `//`-prefixed link destination, and
 * `WIN_PRODUCER_PATH_RE` (utils/fileTokens.ts) documents the producer/consumer
 * asymmetry that makes all of them deliberate — our own upload endpoint may emit
 * a UNC path because we trust it, while every consumer-side predicate over
 * authorable text must refuse the host-naming shape.
 *
 * Cost on POSIX is nil: `//tmp/x` names the same file as `/tmp/x`, which is
 * still a candidate. Cost on Windows is that a network-share path renders as a
 * copy chip rather than an open chip — the same trade `MdAnchor` already makes.
 */
const UNC_PREFIX_RE = /^[/\\]{2}/

/**
 * A space followed by a flag (`-s`, `--force`) or by the root of a SECOND path
 * (`/x`, `~/x`, `./x`, `..\x`, `C:\x`): the span is a command line carrying a
 * path argument, not one path.
 *
 * `PATH_SHAPE_RE` admits spaces and `-` so that `My Docs/a.md` and
 * `Report - final.md` classify, which also lets a whole command such as
 * `sudo xcode-select -s /Applications/Xcode.app` pass on the extension of its
 * last argument. The stat probe always refuses it, but the glyph reserve is
 * keyed to SHAPE, not to the probe's answer, so the copy chip kept an invisible
 * 16px glyph slot in front of the command for good — a blank gap that reads as
 * a missing icon. A path never needs a space directly before a separator or a
 * flag, so refusing that pair keeps every spaced name above classifying.
 */
const COMMAND_LINE_RE = /\s(?:--?[A-Za-z]|~?[/\\]|\.{1,2}[/\\]|[A-Za-z]:[/\\])/

/** The last path segment, split on EITHER separator so a Windows path yields its
 *  real basename. `lastIndexOf('/')` alone returns -1 for `C:\a\notes` and hands
 *  the whole string to `EXT_RE`, which then reads a dotted DIRECTORY name
 *  (`project\v1.2\notes`) as an extension on the file. */
export function basenameOf(s: string): string {
  const cut = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'))
  return s.slice(cut + 1)
}

/** A trailing `.ext` on the last segment, 1-8 chars — the only positive path
 *  signal available to a path that is neither rooted nor explicitly relative.
 *  The extension itself stays ASCII on purpose: it is a POSITIVE signal, and
 *  keeping it narrow is what stops slash-separated prose from classifying. A
 *  Unicode basename with an ASCII extension (`产品文档-v1.0.md`) still passes,
 *  because only the trailing `.ext` is matched. */
const EXT_RE = /\.[A-Za-z0-9]{1,8}$/

/** Explicitly relative, either separator: `./x`, `../x`, `.\x`, `..\x`. */
const REL_PREFIX_RE = /^\.{1,2}[/\\]/

/**
 * Could this inline-code text denote a local filesystem path?
 *
 * Deliberately a PRE-FILTER, not a decision. "Is `refs/heads/fix/foo` a path?"
 * is not a syntactic question — it is a filesystem question — so this only
 * decides whether spending a stat probe is worthwhile. The probe
 * (`usePathKind`) makes the actual call.
 *
 * Merely containing a slash is not enough: that matched git refs
 * (`refs/heads/…`, `origin/main`), repo slugs (`owner/repo`), MIME types
 * (`text/plain`), npm scopes (`@scope/pkg`) and dates (`2026/08/02`), every one
 * of which then rendered as a clickable "file" that could only ever 404. So a
 * candidate must carry a positive signal that it names a location:
 *
 *   - rooted — POSIX (`/x`, `~/x`) or a Windows drive (`C:\x`, `C:/x`), or
 *   - explicitly relative (`./x`, `../x`, `.\x`, `..\x`), or
 *   - a file extension on the last segment (`src/main.py`, `src\main.py`).
 *
 * A bare two-segment identifier with no extension is rejected. That rejection is
 * what keeps the backslash separator safe on every platform: a `\`-joined
 * non-path carries no extension, so an escape sequence (`\n`), a registry key
 * (`HKEY_LOCAL_MACHINE\Software\Foo`) and a domain-qualified login
 * (`CORP\alice`) all still fail here rather than becoming a chip that could only
 * 404. Note the third rule still admits `origin/feature/x.ts`; that is
 * intentional — syntax cannot settle it, and the stat probe will.
 *
 * UNC is refused FIRST, ahead of every shape and signal test, because the other
 * rules would otherwise readmit it: the extension rule matches
 * `\\host\share\x.txt`, and the leading-`/` rule matches `//host/share/x`.
 * See `UNC_PREFIX_RE` for why that shape must never reach the probe. A command
 * line carrying a path argument is refused next, for the same reason: the
 * extension rule would otherwise admit the whole command (`COMMAND_LINE_RE`).
 *
 * A directory written with a trailing separator (`/home/user/notes/`,
 * `C:\Users\me\`) is classified by retrying on the slash-stripped form when the
 * literal string fails: `PATH_SHAPE_RE` requires the string to END in a name
 * character, so a trailing `/` otherwise fails the shape and the directory chip
 * renders dead -- the directory-chip half of issue #9409. This widens NOTHING.
 * The retry runs the SAME rules on the string minus one trailing separator, so a
 * trailing slash rescues only a string whose slash-less form is already a
 * candidate: `owner/repo/`, `text/plain/` and `2026/08/02/` stay rejected because
 * `owner/repo` etc. are. The literal form is tried first so a bare drive root
 * (`C:\`, whose slash-stripped `C:` is not a valid shape) keeps classifying, and
 * the UNC refusal runs on the ORIGINAL string so `//host/share/` cannot slip
 * through the strip.
 */
export function isPathCandidate(s: string): boolean {
  if (UNC_PREFIX_RE.test(s)) return false
  if (COMMAND_LINE_RE.test(s)) return false
  if (classifyPathShape(s)) return true
  // Retry once on the slash-stripped form so a trailing separator does not
  // disqualify an otherwise-valid directory. Guarded to len > 1 so `/` and `\`
  // are not reduced to the empty string.
  if (s.length > 1 && (s.endsWith('/') || s.endsWith('\\'))) {
    return classifyPathShape(s.slice(0, -1))
  }
  return false
}

/** Shape + positive-signal test for a UNC-screened candidate. See
 *  `isPathCandidate`, which owns the UNC refusal and the trailing-separator
 *  retry. */
function classifyPathShape(s: string): boolean {
  if (!PATH_SHAPE_RE.test(s) && !WIN_DRIVE_PATH_SHAPE_RE.test(s)) return false
  if (s.startsWith('/') || s.startsWith('~') || REL_PREFIX_RE.test(s)) return true
  // Rootedness is the positive signal, exactly as a leading `/` is on POSIX, so
  // a drive-rooted path needs no extension: `C:\Windows` is a real directory.
  // Reuses the consumer-side predicate `urlTransform` already applies to image
  // `src` values rather than restating it, so the chip and the request it issues
  // cannot drift on what "absolute" means — and this pre-filter inherits that
  // predicate's deliberate exclusion of host-naming shapes.
  if (WINDOWS_ABS_PATH_RE.test(s)) return true
  return EXT_RE.test(basenameOf(s))
}

/**
 * A trailing source location: `:447`, or `:447:12` for line-and-column.
 *
 * Capped at 7 digits so a long digit run (a hash fragment, an id) is not read as
 * a line number, and so the captured value always parses to a safe integer.
 */
const LINE_REF_RE = /:(\d{1,7})(?:-(\d{1,7})|:\d{1,7})?$/

/**
 * Split a `file:line` / `file:line:col` reference into its path and line.
 *
 * Agents cite code the way compilers and stack traces do, so the location is
 * part of the token, and treating the whole token as a filename is what made
 * these chips inert: the stat probe asked the backend about
 * `…/_dispatch.py:447`, which does not exist, so the chip rendered as dead
 * text. Splitting first lets the probe ask about the file and the click carry
 * the line.
 *
 * Three shapes are accepted: a single line (`:447`), a line and column
 * (`:447:12`), and a RANGE (`:10-16`). The column is matched so it can be
 * consumed but is discarded — the reveal is line-granular, and pretending to a
 * column we then ignore would be a worse contract than not offering one. A
 * range, by contrast, IS honoured: the whole span is revealed and highlighted.
 *
 * Purely syntactic and therefore ambiguous: a file whose name genuinely ends in
 * `:12` splits into a path that does not exist. Callers resolve that by probing
 * the split path first and falling back to the unsplit text (see `InlineCode`),
 * rather than by guessing here.
 */
export function splitLineRef(s: string): { path: string; line?: number; endLine?: number } {
  const m = LINE_REF_RE.exec(s)
  if (!m) return { path: s }
  const line = Number(m[1])
  // `:0` is not a line — every editor numbers from 1 — so treat it as
  // part of the name rather than clamping it to 1 and jumping somewhere the
  // text never named.
  if (!line) return { path: s }
  const path = s.slice(0, m.index)
  const end = m[2] ? Number(m[2]) : undefined
  // A reversed or degenerate range (`:16-10`, `:10-0`, `:10-10`) carries no more
  // information than its start, so it collapses to a single line rather than
  // being silently swapped — guessing which end the author meant would be worse
  // than honouring the number they put first.
  if (end == null || end <= line) return { path, line }
  return { path, line, endLine: end }
}

type PathResolution = {
  candidate: boolean
  /** Path SHAPE alone, independent of whether probing is enabled.
   *
   * `candidate` also requires the probe to be on, so it flips the moment a
   * message stops streaming — and anything keyed to it would appear then,
   * re-wrapping a paragraph whose text has just become final. The glyph reserve
   * is keyed to this instead, so it is already in place before the probe's
   * answer (or the probe itself) can arrive. */
  shaped: boolean
  kind: PathKind | undefined
  path: string
  splitPath: string
  line: number | undefined
  endLine: number | undefined
  probePending: boolean
}

/** Resolve both legal readings of a location suffix before exposing an action.
 *
 * A literal filename such as `report.md:12` takes precedence over the inferred
 * `report.md` at line 12, so both Markdown forms use the same probe ordering.
 */
export function usePathResolution(raw: string, probeEnabled: boolean): PathResolution {
  const { path: splitPath, line, endLine } = splitLineRef(raw)
  const shaped = isPathCandidate(splitPath)
  const candidate = probeEnabled && shaped
  const literalCandidate = candidate && line != null
  const splitKind = usePathKind(candidate ? splitPath : null)
  const literalKind = usePathKind(literalCandidate ? raw : null)
  const literalWins = literalKind === 'file' || literalKind === 'dir'

  return {
    candidate,
    shaped,
    kind: literalWins ? literalKind : splitKind,
    path: literalWins ? raw : splitPath,
    splitPath,
    line: literalWins ? undefined : line,
    endLine: literalWins ? undefined : endLine,
    probePending: (candidate && splitKind === undefined)
      || (literalCandidate && literalKind === undefined),
  }
}

/**
 * Act on a confirmed path chip.
 *
 * `reveal` is the shift-modifier / no-handler escape hatch: hand the path to the
 * OS file manager, which understands both files and directories.
 *
 * `line` (from a `file:447` chip) is passed to the file handler so it can scroll
 * to and flash that line. It is dropped on the two fallback routes on purpose:
 * `revealPath` selects a file in Finder/Explorer, which has no notion of a line,
 * and a directory does not have one either.
 */
export function activatePath(
  path: string,
  kind: PathKind,
  reveal: boolean,
  actions: PathActions,
  onRevealError: (message: string) => void,
  line?: number,
  endLine?: number,
): void {
  // Route through the shared helper, not bare `api.revealPath`: the helper owns
  // the clipboard write and the failure message. `api.revealPath` is side-effect-
  // free, so a bare call on a remote/headless session would answer {ok, copy} and
  // nobody would write the clipboard — the chip's "Shift+click to copy path"
  // promise would silently do nothing. A failed reveal is reported to the chip
  // that was clicked (see useRevealFailure), never to a blocking dialog.
  const opts = { onError: onRevealError }
  if (reveal) { void revealOrOpen(path, 'reveal', opts); return }
  if (kind === 'dir') {
    // No folder handler wired: fall back to the OS file manager rather than
    // silently doing nothing.
    if (actions.onFolderOpen) actions.onFolderOpen(path)
    else void revealOrOpen(path, 'reveal', opts)
    return
  }
  if (!actions.onFileOpen) { void revealOrOpen(path, 'reveal', opts); return }
  // Called with ONE argument when there is no line, not with an explicit
  // `undefined`: the handler is also the app's general-purpose file opener, and
  // an omitted argument keeps a chip click indistinguishable from every other
  // caller of it.
  if (line != null) actions.onFileOpen(path, endLine != null ? { line, endLine } : { line })
  else actions.onFileOpen(path)
}
