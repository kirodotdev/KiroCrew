/**
 * Pure line-counting and patch-sectioning helpers shared by the diff surfaces.
 *
 * Their own module rather than members of `components/FileChangeChips.tsx` and
 * `pages/chat/ActivityViewer.tsx` so a pure test can reach them without
 * importing either: both pull the Pierre diff runtime, framer-motion,
 * react-markdown, katex and highlight.js into the importing fork, which line
 * arithmetic over two strings has no reason to pay for.
 */

/**
 * Line-level diff count via LCS — correctly attributes moves as +N/-N
 * (a moved line shows up as a removal at the old position and an addition
 * at the new). Falls back to a cheap multiset count for huge files to bound
 * cost; that fallback can under-report pure moves but only on files we
 * already cap at 200KB, so the cap is rarely hit in practice.
 */
export function countLines(before: string, after: string): { added: number; removed: number } {
  if (before === after) return { added: 0, removed: 0 }
  // Guard empty strings: ''.split('\n') yields [''] (1 phantom line), which would
  // mis-count a new file as +1/-1 instead of +1, and a fully cleared file as
  // +1/-2 instead of -2. Treat empty content as zero lines.
  const a = before ? before.split('\n') : []
  const b = after ? after.split('\n') : []
  const m = a.length, n = b.length
  // LCS with rolling rows: O(mn) time, O(min(m,n)) space.
  // 1M cell cap = ~1000x1000 lines which covers anything inside our 200KB snapshot cap comfortably.
  if (m * n <= 1_000_000) {
    let prev = new Int32Array(n + 1)
    let curr = new Int32Array(n + 1)
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (a[i - 1] === b[j - 1]) curr[j] = prev[j - 1] + 1
        else curr[j] = prev[j] >= curr[j - 1] ? prev[j] : curr[j - 1]
      }
      const tmp = prev; prev = curr; curr = tmp
      curr.fill(0)
    }
    const lcs = prev[n]
    return { added: n - lcs, removed: m - lcs }
  }
  // Huge-file fallback: multiset count. Cheap but doesn't detect pure moves.
  const aMap = new Map<string, number>()
  const bMap = new Map<string, number>()
  for (const line of a) aMap.set(line, (aMap.get(line) || 0) + 1)
  for (const line of b) bMap.set(line, (bMap.get(line) || 0) + 1)
  let added = 0, removed = 0
  for (const [line, count] of bMap) {
    const aCount = aMap.get(line) || 0
    if (count > aCount) added += count - aCount
  }
  for (const [line, count] of aMap) {
    const bCount = bMap.get(line) || 0
    if (count > bCount) removed += count - bCount
  }
  return { added, removed }
}

/**
 * Added/removed counts read straight off a UNIFIED DIFF's own markers, for a
 * surface that already holds a patch rather than the before/after pair
 * `countLines` needs. The whole patch's totals: `splitPatchSections` is the
 * walk, and this sums its per-file counts.
 */
export function countDiffStats(diff: string): { added: number; removed: number } {
  let added = 0, removed = 0
  for (const section of splitPatchSections(diff)) {
    added += section.added
    removed += section.removed
  }
  return { added, removed }
}

/** One file of a unified diff: the run of lines from where the patch says a
 *  file begins — its `diff --git` / `Index:` preamble, or its `---`/`+++`
 *  header pair when it has no preamble — up to where the next file begins. */
export interface PatchSection {
  /** The section's lines, verbatim: preamble, header pair and hunks. A section
   *  is itself a unified diff of one file, so it renders wherever the whole
   *  patch would. */
  text: string
  /** The file the section is about: the `+++` side with git's `b/` marker
   *  removed, or the `---` side for a deletion (`+++ /dev/null`). An entry
   *  with no header pair — a 100%-similarity rename, a binary change, a mode
   *  change — is named by its `rename to` line, else by its `diff --git`
   *  line when that names one path on both sides. `null` when the text names
   *  none — a bare hunk, a fence of `+`/`-` lines. */
  name: string | null
  /** The `---` side (git's `a/` marker removed) — or, with no header pair,
   *  the `rename from` line — when it names a different file than `name`: a
   *  rename. `null` otherwise. */
  prevName: string | null
  /** What kind of change the entry is, from git's extended header lines and
   *  the header pair's `/dev/null` side: `added` (`new file mode`,
   *  `--- /dev/null`), `deleted` (`deleted file mode`, `+++ /dev/null`), else
   *  `modified` — a rename or a mode change is a change to a file that is
   *  still there. Data for the row: with Pierre's own header (its change icon)
   *  off, the row is the one place left to say it. */
  kind: 'added' | 'deleted' | 'modified'
  /** A binary change (`Binary files … differ`, `GIT binary patch`): no text
   *  hunk shows it, so the row has to. */
  binary: boolean
  /** The file's mode change, from the `old mode` / `new mode` pair git writes
   *  in the entry's metadata — beside a hunk (a chmod and an edit in one
   *  commit) or alone. `null` when the mode did not change; a `new file mode`
   *  is not a change of mode. */
  modeChange: { from: string; to: string } | null
  /** What a plain rendering under a caller-drawn header row prints: the
   *  hunks' content, one string per hunk, with nothing filtered after the
   *  first `@@` — a line there is content whatever it starts with. An entry
   *  with no hunk has no content, and the row states its change — the file
   *  names, the rename, the kind, the binary change, the mode change — so its
   *  body is empty (the row alone), save a metadata line the row has no word
   *  for (`copy from`), which prints rather than hide anything. */
  body: string[]
  added: number
  removed: number
}

/**
 * Cut a unified diff into its per-file sections, each with the file it names,
 * its mode change, its plain body and its own added/removed counts.
 *
 * Everything is decided by POSITION, never by the shape of a line. A section
 * has a metadata block — from its `diff --git` / `Index:` preamble (or its
 * `---`/`+++` header pair, when it has no preamble) up to its first `@@` hunk
 * header — and, after that header, content. Inside the content a line is read
 * by its first character alone, because content can begin with the header
 * prefixes too: `createTwoFilesPatch` marks a removed `-- comment` (SQL, Lua,
 * Haskell) or a removed YAML/markdown `---` rule as `--- comment` / `----`, and
 * an added `++i` as `+++i`. Skipping those by prefix under-counted exactly the
 * change they carry, and dropping them from a plain body hid it.
 *
 * A file begins at EVERY `diff ` / `Index: ` preamble line — an entry with no
 * hunk and no header pair (a 100%-similarity rename, `Binary files … differ`,
 * a mode-only change) is a file too, and a cut that waited for a hunk or a
 * `+++` swallowed it into the next file's section — or, for the preamble-less
 * shape difflib emits, at a `---`/`+++` header pair met once the current
 * section already holds its own header pair or a hunk. Inside a hunk such a
 * pair is a header only where a `@@` right below it announces the next file:
 * never because the hunk's declared counts say it is over, since a hand- or
 * model-written patch gets those counts wrong in both directions and reading
 * a pair as a header on their word turned real changed lines into a fake
 * file. That is the cut `normalizePatchHunks` makes for Pierre before it
 * repairs the counts from the body, so the rows and the bodies under them
 * agree on where each file begins. Lines before the first file's preamble
 * stay with that file.
 *
 * Text with no hunk header at all — a hand-pasted ```diff fence of bare `+`/`-`
 * lines — has no positions to go by, so its lines are read by the prefix rule:
 * every `+`/`-` line counts except `+++`/`---`, which can only be headers there.
 */
export function splitPatchSections(diff: string): PatchSection[] {
  const lines = diff.split('\n')
  const sections: PatchSection[] = []
  let start = 0
  let open = new OpenSection()
  const close = (end: number) => {
    sections.push(open.finish(lines.slice(start, end).join('\n')))
    start = end
    open = new OpenSection()
  }
  walkPatch(lines, (kind, line, i) => {
    switch (kind) {
      case 'file':
        // A `diff ` / `Index: ` line always begins a file, so it closes a
        // section that holds anything at all.
        if (open.preambled || open.bodied) close(i)
        open.preambled = true
        open.names.readPreamble(line)
        break
      case 'old-header':
        // A `---` header closes only a section that already holds its own
        // header pair or a hunk: after a bare preamble it is that preamble's.
        if (open.bodied) close(i)
        open.names.prevName = headerPath(line)
        break
      case 'new-header':
        open.names.name = headerPath(line)
        open.bodied = true
        break
      case 'hunk':
        open.bodied = true
        open.hunks.push([])
        break
      case 'add':
        open.added++
        open.content(line)
        break
      case 'del':
        open.removed++
        open.content(line)
        break
      case 'ctx':
      case 'marker':
        open.content(line)
        break
      case 'meta':
        if (open.hunks.length > 0) open.content(line)
        else open.metadata(line)
        break
    }
  })
  close(lines.length)
  return sections
}

/**
 * The plain body of `patch` under a header row the caller draws — every
 * section's `body`, in order. One file's patch (a section handed to a plain
 * renderer) gives that file's hunks.
 */
export function plainPatchHunks(patch: string): string[] {
  return splitPatchSections(patch).flatMap(section => section.body)
}

/** A section while the walk is inside it. */
class OpenSection {
  names = new SectionNames()
  /** Holds a `diff ` / `Index: ` preamble line. */
  preambled = false
  /** Holds its own `+++` header or a hunk, so a `---` header met next begins
   *  the following file. */
  bodied = false
  /** The hunks' content lines, one array per `@@`. */
  hunks: string[][] = []
  /** Metadata lines that would be lost if no one printed them: the row has no
   *  word for them and the entry has no hunk. */
  meta: string[] = []
  modeChange: { from: string; to: string } | null = null
  private oldMode: string | null = null
  private newMode: string | null = null
  private newFile = false
  private deletedFile = false
  binary = false
  /** Inside a `GIT binary patch` payload: `literal N` / `delta N` and the
   *  base85 lines under them, until the entry ends. */
  private binaryPayload = false
  added = 0
  removed = 0

  /** A content line: after the first hunk header, whatever it starts with. */
  content(line: string): void {
    if (this.hunks.length === 0) this.hunks.push([])
    this.hunks[this.hunks.length - 1].push(line)
  }

  /** A line of the metadata block: names, the kind of change, a mode change,
   *  a binary change — data the row states — or a line nothing else will. */
  metadata(line: string): void {
    if (this.binaryPayload) return
    this.names.readPreamble(line)
    if (line.startsWith('old mode ')) this.oldMode = line.slice('old mode '.length)
    else if (line.startsWith('new mode ')) this.newMode = line.slice('new mode '.length)
    else if (line.startsWith('new file mode ')) this.newFile = true
    else if (line.startsWith('deleted file mode ')) this.deletedFile = true
    else if (line.startsWith('Binary files ')) this.binary = true
    else if (line.startsWith('GIT binary patch')) { this.binary = true; this.binaryPayload = true }
    else if (!ROW_STATED_META_RE.test(line)) this.meta.push(line)
  }

  finish(text: string): PatchSection {
    const modeChange = this.oldMode != null && this.newMode != null ? { from: this.oldMode, to: this.newMode } : null
    // The header pair says it too: an added file's `---` side and a deleted
    // file's `+++` side are `/dev/null`, which `headerPath` reads as no name.
    const paired = this.names.name != null || this.names.prevName != null
    const kind = this.newFile || (paired && this.names.prevName == null) ? 'added'
      : this.deletedFile || (paired && this.names.name == null) ? 'deleted'
      : 'modified'
    // Blank lines at a hunk's edges are seams, not content: the trailing
    // newline of the patch, the gap some emitters leave between files.
    const blocks = (this.hunks.length > 0 ? this.hunks : [this.meta]).map(trimBlankEdges).filter(b => b.length > 0)
    return { ...patchSection(text, this.names, this.added, this.removed), kind, binary: this.binary, modeChange, body: blocks.map(b => b.join('\n')) }
  }
}

function trimBlankEdges(lines: string[]): string[] {
  let from = 0, to = lines.length
  while (from < to && lines[from] === '') from++
  while (to > from && lines[to - 1] === '') to--
  return lines.slice(from, to)
}

/** The metadata lines the header row already states — the blob `index`, the
 *  `similarity index`, the `rename from` / `rename to` pair (the mode, kind
 *  and binary lines are read into data above) — so a hunk-less entry's plain
 *  body omits them. */
const ROW_STATED_META_RE = /^(?:index [0-9a-f]+\.\.[0-9a-f]+|similarity index \d+%|(?:rename|copy) (?:from|to) )/

/** How the positional walk reads one line of a unified diff. */
type PatchLineKind =
  /** A `diff ` / `Index: ` preamble line: where a file begins. */
  | 'file'
  /** A `--- x` file header: outside a hunk body and paired with a `+++ ` line. */
  | 'old-header'
  /** A `+++ y` file header: the partner of an `old-header`. */
  | 'new-header'
  /** An `@@` hunk header, with counts (`@@ -a,b +c,d @@`) or without. */
  | 'hunk'
  /** A hunk body line, by its first character. */
  | 'add' | 'del' | 'ctx'
  /** `\ No newline at end of file`. */
  | 'marker'
  /** Any other line outside a hunk body: git's extended header lines (`index`,
   *  `similarity index`, `rename from`, `old mode`, `Binary files … differ`),
   *  a lone `--- x` past a miscounted hunk, a blank line. */
  | 'meta'

/**
 * The positional walk `splitPatchSections` reads, so what counts as a header,
 * a hunk line or a preamble is decided once.
 *
 * Every line beginning `@@` is a hunk header — git's `@@ -a,b +c,d @@` and the
 * bare `@@ text @@` a hand-written patch carries (`normalizePatchHunks` feeds
 * Pierre the same reading). Inside a hunk body a `---`/`+++` pair is the next
 * file's header only when a `@@` right below announces it; a `diff ` /
 * `Index: ` line always begins a file. A header's declared counts decide
 * nothing — a hand- or model-written patch gets them wrong in both directions,
 * and `normalizePatchHunks` rewrites them from the body for Pierre — so a
 * `---`/`+++` line that nothing announces is content whatever the counts say.
 * Text with no `@@` at all is read by the prefix rule instead: `+`/`-` lines
 * are content, `---`/`+++` lines are headers.
 */
function walkPatch(lines: readonly string[], visit: (kind: PatchLineKind, line: string, index: number) => void): void {
  if (!lines.some(isHunkHeader)) {
    for (let i = 0; i < lines.length; i++) visit(prefixKind(lines[i]), lines[i], i)
    return
  }
  let inHunk = false
  // A `--- `/`+++ ` pair that a `@@` right below announces as the next file's
  // header — the one reading of such a pair a hunk body allows.
  const announcedPair = (i: number) => lines[i].startsWith('--- ') && (lines[i + 1] ?? '').startsWith('+++ ') && isHunkHeader(lines[i + 2] ?? '')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (isHunkHeader(line)) {
      inHunk = true
      visit('hunk', line, i)
      continue
    }
    if (inHunk) {
      if (line.startsWith('\\')) { visit('marker', line, i); continue }
      // Inside a hunk a `---`/`+++` pair is the next file's header only when a
      // `@@` right below announces it (a `diff ` line above announces it too,
      // and is read as the preamble it is). Never the counts: a header's
      // declared counts are the one thing a hand- or model-written patch gets
      // wrong in both directions, and reading a pair as a header because the
      // counts say the hunk is over turned a deleted `-- foo` and an added
      // `++ bar` into a fake file and lost them from every body — the same
      // rule `normalizePatchHunks` applies before it repairs the counts from
      // the body for Pierre, so the rows and the bodies agree.
      if (!announcedPair(i)) {
        if (line.startsWith('+')) {
          visit('add', line, i)
          continue
        }
        if (line.startsWith('-')) {
          visit('del', line, i)
          continue
        }
        if (!isPreamble(line)) {
          visit('ctx', line, i)
          continue
        }
      }
      // An announced pair or the next file's preamble: the hunk is over and
      // this line is read as a header below.
      inHunk = false
    }
    if (isPreamble(line)) visit('file', line, i)
    // Paired with its `+++`, the way a header always is: a lone `--- x`
    // outside a hunk is a stray line, not a file, and cutting a file there
    // would hand the renderer a headless half of it.
    else if (line.startsWith('--- ') && (lines[i + 1] ?? '').startsWith('+++ ')) visit('old-header', line, i)
    else if (line.startsWith('+++ ') && (lines[i - 1] ?? '').startsWith('--- ')) visit('new-header', line, i)
    else visit('meta', line, i)
  }
}

/** The prefix rule for text with no hunk header: every `+`/`-` line is content
 *  except `+++`/`---`, which can only be headers there. */
function prefixKind(line: string): PatchLineKind {
  if (isPreamble(line)) return 'file'
  if (line.startsWith('+++ ')) return 'new-header'
  if (line.startsWith('--- ')) return 'old-header'
  if (line.startsWith('+') && !line.startsWith('+++')) return 'add'
  if (line.startsWith('-') && !line.startsWith('---')) return 'del'
  if (line.startsWith('\\')) return 'marker'
  return 'meta'
}

const isPreamble = (line: string) => line.startsWith('diff ') || line.startsWith('Index: ')

/** The names a section collects while it is walked: its `---`/`+++` pair, and
 *  the fallbacks for an entry that has none. */
class SectionNames {
  /** The `+++` side. */
  name: string | null = null
  /** The `---` side. */
  prevName: string | null = null
  /** The `rename to` / `rename from` — or `copy to` / `copy from` — lines of
   *  a git rename or copy entry (`git diff -C`); a copy is titled like a
   *  rename, `from → to`, because that is what it says about the new file. */
  renameTo: string | null = null
  renameFrom: string | null = null
  /** The path a `diff --git a/x b/x` line names on both sides. */
  gitPath: string | null = null

  /** Read a preamble or extended-header line for the names it carries. */
  readPreamble(line: string): void {
    if (line.startsWith('diff --git ')) this.gitPath = gitDiffPath(line)
    else if (line.startsWith('rename from ') || line.startsWith('copy from ')) this.renameFrom = unquotePath(line.slice(line.indexOf(' from ') + ' from '.length))
    else if (line.startsWith('rename to ') || line.startsWith('copy to ')) this.renameTo = unquotePath(line.slice(line.indexOf(' to ') + ' to '.length))
  }
}

/** The one path a `diff --git a/x b/x` line names when both sides agree —
 *  the entry of a binary or mode-only change, which has no `---`/`+++` pair to
 *  name it. `null` when the sides differ (a rename, which its `rename from` /
 *  `rename to` lines name instead) or the line is not in git's shape. Git
 *  quotes a path with a space, a quote or a non-ASCII byte (`"a/my notes.md"`,
 *  C-style escapes inside), so a quoted line is split at its quotes; a bare
 *  one at the ` b/` at which the two halves are equal, so a path containing
 *  ` b/` cannot be cut in the wrong place. */
function gitDiffPath(line: string): string | null {
  const rest = line.slice('diff --git '.length)
  if (rest.startsWith('"')) {
    const halves = rest.match(/^"((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"$/)
    if (halves == null) return null
    const a = unquotePath(`"${halves[1]}"`), b = unquotePath(`"${halves[2]}"`)
    return a.startsWith('a/') && b.startsWith('b/') && a.slice(2) === b.slice(2) ? a.slice(2) : null
  }
  if (!rest.startsWith('a/')) return null
  for (let at = rest.indexOf(' b/'); at !== -1; at = rest.indexOf(' b/', at + 1)) {
    const a = rest.slice(2, at)
    if (a && a === rest.slice(at + 3)) return a
  }
  return null
}

/** Undo git's C-style path quoting (`core.quotePath`): a path git wrapped in
 *  double quotes has `\"`, `\\`, `\t`, `\n` and octal `\303\251` escapes
 *  inside, the octal bytes spelling UTF-8. An unquoted path is returned as
 *  is. */
function unquotePath(path: string): string {
  if (!(path.length >= 2 && path.startsWith('"') && path.endsWith('"'))) return path
  const bytes: number[] = []
  const inner = path.slice(1, -1)
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i]
    if (ch !== '\\' || i + 1 >= inner.length) { bytes.push(...new TextEncoder().encode(ch)); continue }
    const next = inner[++i]
    const octal = /^[0-7]{3}/.exec(inner.slice(i))
    if (octal != null) { bytes.push(parseInt(octal[0], 8)); i += 2; continue }
    const escapes: Record<string, string> = { n: '\n', t: '\t', r: '\r', '"': '"', '\\': '\\', a: '\x07', b: '\b', f: '\f', v: '\v' }
    bytes.push(...new TextEncoder().encode(escapes[next] ?? next))
  }
  return new TextDecoder().decode(new Uint8Array(bytes))
}

/** The path a `--- ` / `+++ ` header names: the remainder up to the TAB that
 *  separates an optional timestamp, with git's `a/` / `b/` side marker
 *  removed. `null` for the placeholders of an added or deleted side
 *  (`/dev/null`, a bare `-` / `+`). */
function headerPath(line: string): string | null {
  const rest = line.slice(4)
  const raw = rest.startsWith('"') ? unquotePath(rest) : rest.split('\t')[0]
  if (!raw || raw === '/dev/null' || raw === '-' || raw === '+') return null
  return /^[ab]\//.test(raw) ? raw.slice(2) : raw
}

function patchSection(text: string, names: SectionNames, added: number, removed: number): Omit<PatchSection, 'kind' | 'binary' | 'modeChange' | 'body'> {
  // The header pair names the file when there is one: a deletion names its
  // file on the `---` side only; a modification names the same file twice.
  // Without a pair the git preamble does — `rename to` / `rename from` for a
  // rename, the `diff --git` line for a binary or mode-only change.
  const paired = names.name != null || names.prevName != null
  const name = paired ? names.name : names.renameTo ?? names.gitPath
  const prevName = paired ? names.prevName : names.renameFrom
  if (name == null) return { text, name: prevName, prevName: null, added, removed }
  return { text, name, prevName: prevName === name ? null : prevName, added, removed }
}

/** Every line beginning `@@` is a hunk header, counts or not. */
const isHunkHeader = (line: string) => line.startsWith('@@')

/** The 0-based line span that differs between two texts, per side, or `null`
 *  when they are identical. `oldStart`/`oldEnd` index `before`'s lines and
 *  `newStart`/`newEnd` index `after`'s; each `*End` is exclusive.
 *
 *  This is a common-prefix / common-suffix walk, NOT a diff: it finds only the
 *  OUTER bounds of the change (first line that differs from the top, last line
 *  that differs from the bottom), so a scatter of edits reports one span that
 *  covers them all. That span is a locality region, not a row-level diff. Only
 *  its first and last non-empty rows are proven to differ; consumers must not
 *  give the interior add/remove semantics. The oversized fallback exists because
 *  a real line-level diff is too expensive to run on the renderer thread for
 *  these inputs (see `renderBudget`), and this stays cheap for the same reason:
 *  two pointer walks that stop at the first difference, no LCS, no allocation
 *  beyond the two line arrays the caller already holds. Its one job is to tell
 *  the fallback WHERE to look so it can anchor there instead of at line 1. */
export function changedLineSpan(
  beforeLines: readonly string[],
  afterLines: readonly string[],
): { oldStart: number; oldEnd: number; newStart: number; newEnd: number } | null {
  const m = beforeLines.length
  const n = afterLines.length
  let start = 0
  const max = Math.min(m, n)
  while (start < max && beforeLines[start] === afterLines[start]) start++
  if (start === m && start === n) return null // identical
  // Walk the common suffix, but never cross the common prefix on either side.
  let endBack = 0
  while (
    endBack < m - start
    && endBack < n - start
    && beforeLines[m - 1 - endBack] === afterLines[n - 1 - endBack]
  ) endBack++
  return { oldStart: start, oldEnd: m - endBack, newStart: start, newEnd: n - endBack }
}
