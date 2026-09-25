import { Fragment, memo, useState, useMemo, useEffect, useRef } from 'react'
import { Copy, Check, Columns2, Rows2 } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { fileReadUrl } from '../utils/fileReadUrl'
import { isSafePath } from '../utils/safePath'
import { splitPatchSections, type PatchSection } from '../utils/diffLineCounts'
import { PierrePatch } from '../pierre'
import { PlainCodeFallback, PlainFilePairHeader, PlainPatchBodyContext, type PlainPatchBodyOwner } from '../pierre/PlainCodeFallback'
import { PIERRE_WRAP_NO_HSCROLL_CSS, PIERRE_SEPARATOR_BG_CSS } from '../pierre/config'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'
import { useDiffSplit } from '../hooks/useDiffSplit'
import { usePlainDiff } from '../hooks/usePlainDiff'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/** Extract the target file path from unified-diff header lines.
 *
 * Tries several formats in order of specificity:
 *   1. `+++ b/<path>` — git's unified diff (preferred — explicitly the new side)
 *   2. `+++ <path>`   — plain unified diff without git's a/ b/ prefix
 *   3. `--- a/<path>` — git's old-side header (used when only the - side is named)
 *   4. `--- <path>`   — plain unified-diff old-side header
 *   5. `diff --git a/... b/<path>` — git's diff command header (greedy)
 *
 * Skips conventional placeholder paths like `/dev/null` (used for adds /
 * deletes) and bare `-`/`+` markers.
 *
 * `prefixStripped` reports whether the winning candidate had a git `a/` / `b/`
 * prefix removed. That matters because git JOINS the prefix onto the path,
 * collapsing an absolute path's leading slash: `git diff --no-index /tmp/x /tmp/y`
 * emits `+++ b/tmp/y`, so the stripped remainder `tmp/y` is a rootless spelling
 * of `/tmp/y` — syntactically indistinguishable from a genuine repo-relative
 * path. The caller resolves the ambiguity with an existence probe (see
 * `ROOTLESS_ABS_RE` below); this function only preserves the signal.
 *
 * Only lines OUTSIDE hunks are considered: `@@` starts a hunk, and within one
 * a `--- ` / `+++ ` row is content (a deleted `-- ` / added `++ ` line), not a
 * header. Scanning stops at the first hunk since git headers precede hunks.
 */
export function extractFilePath(code: string): { path: string; prefixStripped: boolean } | null {
  let plusFallback: string | null = null
  let minusGit: string | null = null
  let minusPlain: string | null = null
  let gitFallback: string | null = null
  const skip = (p: string) => !p || p === '/dev/null' || p === '-' || p === '+'
  for (const line of code.split('\n')) {
    if (line.startsWith('@@')) break
    // Header paths terminate at a TAB (the unified-diff timestamp separator)
    // or end of line — never at a space, which is a legal path character.
    // Both difflib (the backend's diff generator) and git emit the path as
    // the whole remainder of the line, so a lazy match cut at the first
    // space would resolve "/work/report final.md" to the SIBLING file
    // "/work/report" — and the open-in-panel affordance would read and save
    // the wrong file. A trailing space-separated timestamp from some other
    // tool stays attached instead; the existence probe then fails and the
    // affordance is simply not offered — fail-safe in the harmless direction.
    const plusGitMatch = /^\+\+\+ b\/(.+?)(?:\t|$)/.exec(line)
    if (plusGitMatch && !skip(plusGitMatch[1])) return { path: plusGitMatch[1], prefixStripped: true }
    const plusPlainMatch = /^\+\+\+ ([^\s].*?)(?:\t|$)/.exec(line)
    if (plusPlainMatch && !skip(plusPlainMatch[1]) && !plusFallback) {
      plusFallback = plusPlainMatch[1]
    }
    const minusGitMatch = /^--- a\/(.+?)(?:\t|$)/.exec(line)
    if (minusGitMatch && !skip(minusGitMatch[1]) && !minusGit) {
      minusGit = minusGitMatch[1]
    }
    const minusPlainMatch = /^--- ([^\s].*?)(?:\t|$)/.exec(line)
    if (minusPlainMatch && !skip(minusPlainMatch[1]) && !minusPlain) {
      minusPlain = minusPlainMatch[1]
    }
    if (!gitFallback) {
      const gitMatch = /^diff --git a\/.+ b\/(.+)/.exec(line)
      if (gitMatch) gitFallback = gitMatch[1]
    }
  }
  if (plusFallback) return { path: plusFallback, prefixStripped: false }
  if (minusGit) return { path: minusGit, prefixStripped: true }
  if (minusPlain) return { path: minusPlain, prefixStripped: false }
  if (gitFallback) return { path: gitFallback, prefixStripped: true }
  return null
}

/** Prefix-stripped candidates whose first segment names a conventional
 * filesystem root — the shape a mangled absolute path takes after git's
 * `a/` / `b/` join swallowed its leading slash (issue #2493: the dashboard was
 * observed requesting `path=home/<user>/…`, which the backend correctly 400s).
 * A header matching this is treated as AMBIGUOUS: it is probed only as the
 * rooted spelling, and only when the surrounding chat text independently
 * names that spelling (pathHint corroboration) — otherwise it gets no probe
 * and no affordance. Existence probing cannot arbitrate the ambiguity itself,
 * because with no project dir configured the backend rejects every relative
 * path, making "relative spelling absent" meaningless as evidence. */
const ROOTLESS_ABS_RE = /^(home|Users|tmp|var|opt|workplace)\//

const basename = (path: string) => path.slice(path.lastIndexOf('/') + 1)

/** What a file's header row is titled: the basename, and for a rename the old
 *  basename, an arrow, the new one. The full path is the wrapper's tooltip and
 *  the Open button's target; the row only has to tell the files of one patch
 *  apart. A rename that keeps its basename — a move between directories —
 *  reads as the full paths, `src/a.ts → test/a.ts`: the basenames would say
 *  `a.ts → a.ts`, which says nothing. */
function sectionTitle(section: PatchSection, fallbackPath: string | null): string {
  const name = section.name ?? fallbackPath
  if (name == null) return ''
  const prev = section.prevName
  if (prev == null) return basename(name)
  return basename(prev) !== basename(name) ? `${basename(prev)} → ${basename(name)}` : `${prev} → ${name}`
}

/** What a file's row says about it beyond its name and counts, in words — the
 *  change type Pierre's header showed as an icon, and the mode change no body
 *  shows: `added` / `deleted` (a rename is told by the title's arrow, so no
 *  word repeats it), `binary`, `now executable` / `no longer executable`.
 *  Nothing for a plain edit. Beside the counts or alone, in every state. */
function changeWords(section: PatchSection): string[] {
  const words: string[] = []
  if (section.kind === 'added') words.push(i18nT('components.diffBlock.added'))
  else if (section.kind === 'deleted') words.push(i18nT('components.diffBlock.deleted'))
  if (section.binary) words.push(i18nT('components.diffBlock.binary'))
  if (section.modeChange) words.push(modeWords(section.modeChange))
  return words
}

/** A mode change in words, never as git's octal digits (`100644 → 100755`
 *  meant nothing to a reader). The one change a reader meets is the executable
 *  bit — odd permission digits — so that is the one named; a change of file
 *  type (the leading digits: a regular file becoming a symlink) or anything
 *  else reads as a mode change. */
function modeWords({ from, to }: { from: string; to: string }): string {
  const kind = (mode: string) => mode.slice(0, -3)
  const executable = (mode: string) => /[1357]/.test(mode.slice(-3))
  if (kind(from) === kind(to) && executable(from) !== executable(to)) {
    return i18nT(executable(to) ? 'components.diffBlock.now_executable' : 'components.diffBlock.no_longer_executable')
  }
  return i18nT('components.diffBlock.mode_changed')
}

/** The row's note, after the filename: shrinks and truncates like the title,
 *  because at 320px the row holds it beside the label, the controls and the
 *  counts, and a fixed-width item there pushes the row past the card. The
 *  whole wording stays reachable as the tooltip. */
function changeNote(words: string[]) {
  const text = words.join(' · ')
  return (
    <span data-change-note className="min-w-0 truncate text-[11px] font-normal text-muted" title={text}>
      {text}
    </span>
  )
}

export default memo(function DiffBlock({ code, complete, onFileOpen, pathHint }: { code: string; complete: boolean; onFileOpen?: (path: string) => void; pathHint?: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  // Shares the app-wide `mc-diff-split` preference with the side panel and
  // markdown panel (#6024): the choice made on any diff surface sticks and
  // seeds the next block, instead of every fence resetting to unified.
  const [sideBySide, setSideBySide] = useDiffSplit()
  // Plain-diff preference (Settings → Display). PierrePatch honours it on its
  // own; this block reads it too because the split/unified toggle below is a
  // Pierre layout option, meaningless over the raw text plain mode prints.
  const [plain] = usePlainDiff()
  // Resolve the file path: prefer headers inside the diff, fall back to the
  // pathHint extracted from the surrounding chat text by MarkdownRenderer
  // (helps when a tool emits "Created /path/to/file:" before a
  // bare diff with no +++/--- headers).
  const extracted = useMemo(() => extractFilePath(code), [code])
  // The header row shows the basename only; `extracted` above keeps the full
  // path for the Open button. The patch itself is handed on untouched: Pierre
  // consumes its `--- `/`+++ ` lines to name the file for its grammar and
  // cache key and draws no header here, the plain-diff-mode render prints
  // them verbatim — the one thing "show me the raw diff" promises, and what
  // the Copy button hands out — and every other plain body under this row
  // (the streaming stand-in, the hold, the plain text Pierre shows INSTEAD of
  // the diff) drops them, because this row already states what they say (see
  // `PlainPatchBodyContext`).
  const headerPath = extracted?.path ?? pathHint ?? null
  // When a git prefix was stripped and the remainder starts with a
  // conventional root (`home/…`, `tmp/…`, …), the header is ambiguous between
  // a repo-relative path and an absolute path whose leading slash git's
  // `a/` / `b/` join collapsed (`git diff --no-index /tmp/x` → `+++ b/tmp/x`).
  // An existence probe cannot settle this safely: with no project dir
  // configured the backend 400s EVERY relative path, so "relative spelling
  // absent" is not evidence, and an existence race could point the Open
  // button (which leads to an editor a save can write through) at an
  // unrelated host file. So the ambiguity is resolved by OUTSIDE
  // corroboration only: when the surrounding chat text independently names
  // the rooted spelling (pathHint), that spelling is probed instead; without
  // corroboration the header is suppressed outright — no probe (this was the
  // captured `path=home/<user>/…&resolve=1` 400 from issue #2493) and no
  // affordance, because no button beats a guessed target.
  const ambiguousRootless = extracted != null && extracted.prefixStripped && ROOTLESS_ABS_RE.test(extracted.path)
  const corroboratedRooted = ambiguousRootless && extracted != null && pathHint === '/' + extracted.path ? pathHint : null
  const probePath = ambiguousRootless ? corroboratedRooted : headerPath
  // The path the Open button acts on — committed by the probe effect, KEYED to
  // the headerPath that initiated the probe. The keyed derivation means a
  // verdict measured for a PREVIOUS header is never rendered against the
  // current one (same pattern as usePathKind): during the one render between a
  // header change and the effect re-running, the stale entry mismatches and
  // the button disappears instead of targeting the old path.
  const [resolved, setResolved] = useState<{ forHeader: string; path: string } | null>(null)
  const filePath = resolved && resolved.forHeader === headerPath ? resolved.path : null

  // Stash onFileOpen in a ref so the effect below only depends on the probe
  // candidates. If onFileOpen were a direct dep, every parent re-render that
  // produced a new function reference would refire the effect →
  // setResolved(null) → HEAD probe → setResolved(...), causing the Open
  // button to flicker and reflowing the diff body by 1-2px each time.
  const onFileOpenRef = useRef(onFileOpen)
  onFileOpenRef.current = onFileOpen

  useEffect(() => {
    setResolved(null)
    if (!probePath || !headerPath || !isSafePath(probePath) || !onFileOpenRef.current) return
    const ac = new AbortController()
    ;(async () => {
      let ok = false
      try {
        ok = (await fetch(fileReadUrl(probePath), { method: 'HEAD', signal: ac.signal })).ok
      } catch { /* network failure / abort → no affordance */ }
      // An aborted run must not commit: its fetch may have settled before
      // abort() fired, and the next run's setResolved(null) has already
      // cleared the slate this result was measured against.
      if (ok && !ac.signal.aborted) setResolved({ forHeader: headerPath, path: probePath })
    })()
    return () => ac.abort()
  }, [headerPath, probePath])

  // Diff layout follows the shared, persisted split preference (toggled from
  // this block's own header); wrap because chat/side-panel columns are
  // width-constrained. Pierre draws the BODY only: the block's title row (name,
  // ± counts, controls) is the header row rendered below, so Pierre's own file
  // header stays off.
  const options = useMemo(
    () => ({
      diffStyle: (sideBySide ? 'split' : 'unified') as 'split' | 'unified',
      overflow: 'wrap' as const,
      disableFileHeader: true,
      // A chat diff is a snippet, not a review surface: `simple` is a bare
      // hairline with no label and no expand control, which keeps a short block
      // reading as continuous code. Every other surface keeps `line-info`, whose
      // count and arrows earn their room on a full file. It also keeps the
      // library's untranslated "N unmodified lines" out of chat entirely.
      hunkSeparators: 'simple' as const,
      unsafeCSS: PIERRE_WRAP_NO_HSCROLL_CSS + PIERRE_SEPARATOR_BG_CSS,
    }),
    [sideBySide],
  )

  const copy = async () => { if (await copyToClipboard(code)) { setCopied(true); setTimeout(() => setCopied(false), 1500) } }

  // Patch-level controls, in the block's own header row (light DOM, so
  // outer-tree styling and the group-hover reveal both apply).
  // Space for the Open affordance is reserved on exactly the condition that runs
  // the probe, so the probe's OUTCOME never changes this row's geometry.
  //
  // Without the reserve, a successful HEAD adds a third button to the actions row
  // after an async round-trip. On a pointer surface that reflows the diff body by a
  // couple of pixels; under `HOVER_NONE_ACTIONS_ROW_CLS` (touch) the row is
  // `flex-wrap` with `p-3` targets, so the third button WRAPS it to a second line
  // and the header grows by a whole row — the transcript below then slides by that
  // much, mid-read, which is what "the file diff's loading pushed it up" is.
  //
  // Reserved space costs a phone row even for a file that turns out to be missing.
  // That is the right trade: a header that is one row taller from first paint is
  // stationary, and a header that changes height while someone is reading is not.
  const reserveOpen = Boolean(onFileOpen && probePath && isSafePath(probePath))

  // Frames are still arriving: render the patch as plain text and mount Pierre
  // only once the block is final. Pierre re-parses and re-tokenizes the WHOLE
  // patch on every frame -- `contentCacheKey` is content-derived, so each frame
  // is a cache MISS -- which is a main-thread parse plus a fresh token set per
  // chunk. On a long streamed diff that burst is what shows up as a renderer
  // memory spike and a laggy click, and every intermediate token set is thrown
  // away the moment the next chunk lands. `CodeBlock` already gates its Pierre
  // mount on `complete` for exactly this reason; this is the diff half of that
  // rule. The stand-in is `PlainCodeFallback`, whose body metrics match
  // Pierre's; the header row above it is the block's own and does not change.
  const standInForStream = !complete
  // The patch, one section per file it carries, each with the file it names and
  // its own ± counts — the same rows Pierre's own headers would draw, one per
  // file, available in every state: while frames arrive, while the highlight
  // pool is starting or recovering, in plain mode — because the block owns
  // them. A single-file patch is one section.
  const sections = useMemo(() => splitPatchSections(code), [code])
  // Whether Pierre is showing plain text INSTEAD of the diff under any of the
  // rows — its highlight pool down, or a section it cannot parse. Pierre's
  // patch surface reports each such body through `PlainPatchBodyContext` as it
  // mounts and leaves (a count, because a multi-file patch has one body per
  // row), so the block knows the state from the body itself rather than by
  // reading Pierre's internals. While it holds: the card's row (the file's row
  // on a single-file card) says why the body is plain, the body prints the
  // hunks' content only (the row already names the file and counts its
  // lines), and the split/unified toggle — a Pierre layout option,
  // meaningless over plain text — is withheld, as it is while frames stream.
  // A hold before the diff paints does not report: the toggle it would gate
  // still shapes the diff that is about to land.
  const [plainBodies, setPlainBodies] = useState(0)
  const plainBodyOwner = useMemo<PlainPatchBodyOwner>(() => ({
    onPlainBody: mounted => setPlainBodies(n => n + (mounted ? 1 : -1)),
  }), [])
  const plainBody = plainBodies > 0
  // A card of several files opens with a row of its own: how many files it
  // holds, the whole card's ± counts, the "Plain view" label and the controls
  // that act on the whole card — Open, layout, Copy. On the first file's row
  // those controls sat beside counts that were that file's alone, and a reader
  // could not tell whether the top numbers counted the card or one file. The
  // file rows beneath then carry their own file, note and counts only. A
  // single-file card is its file's row, which is the card's.
  const cardRow = sections.length > 1
  const totals = useMemo(() => sections.reduce(
    (sum, section) => ({ added: sum.added + section.added, removed: sum.removed + section.removed }),
    { added: 0, removed: 0 },
  ), [sections])
  const plainViewLabel = plainBody ? i18nT('components.diffBlock.plain_view') : undefined

  const headerControls = () => (
    <span className={`relative z-10 flex items-center gap-1 opacity-0 group-hover/diff:opacity-100 group-focus-within/diff:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
      {reserveOpen && (
        <button
          className={`px-1.5 py-0.5 rounded text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer ${filePath ? '' : 'invisible pointer-events-none'}`}
          onClick={filePath && onFileOpen ? () => onFileOpen(filePath) : undefined}
          disabled={!filePath}
          aria-hidden={!filePath}
          tabIndex={filePath ? undefined : -1}
          title={filePath ? i18nT('components.diffBlock.open_in_side_panel', { path: filePath }) : undefined}
          aria-label={filePath ? i18nT('components.diffBlock.open_in_side_panel', { path: filePath }) : undefined}
        >
          {i18nT('components.diffBlock.open')}
        </button>
      )}
      {/* Split/unified is a PIERRE layout option, so omit it while plain mode
          or the streaming stand-in renders the raw patch, and while Pierre
          itself shows plain text (pool down, unparseable section): over plain
          text the toggle would change nothing the reader can see, and with
          Open offered the row stays at two actions
          (`max-two-buttons-per-row`). The row's label says why the body is
          plain; the toggle returns with the highlighted diff. */}
      {!plain && !standInForStream && !plainBody && (
        <button
          className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          onClick={() => setSideBySide(!sideBySide)}
          title={sideBySide ? i18nT('components.diffBlock.unified_view') : i18nT('components.diffBlock.split_view')}
          aria-label={sideBySide ? i18nT('components.diffBlock.switch_to_unified_view') : i18nT('components.diffBlock.switch_to_split_view')}
        >
          {sideBySide ? <Rows2 size={13} /> : <Columns2 size={13} />}
        </button>
      )}
      <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.diffBlock.copied') : i18nT('components.diffBlock.copy_patch')} aria-label={copied ? i18nT('components.diffBlock.copied') : i18nT('components.diffBlock.copy_patch')}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
    </span>
  )

  return (
    /* The header shows the basename, so two changed files sharing a name render
       as identical blocks; the full path lives here as a tooltip. */
    <div className="diff-block group/diff rounded-xl border border-border overflow-hidden" title={headerPath ?? undefined}>
      <div className="relative pierre-surface">
        {/* One title row per file, drawn by the block for the block's whole
            life: filename, a note in words where the file's change is more
            than an edit, ± counts — and, on the one row of a single-file card
            or the card row above a multi-file card's file rows,
            `headerControls`, which act on the whole patch. Pierre draws each
            file's body only. Pierre's own header exists only while its
            renderer holds a highlight result — not while its chunk loads, not
            while the worker pool is starting or recovering after a failure,
            not for a patch that will not parse, never in plain mode — so a
            control slotted into it is gone in every one of those states, and
            a mount of the slot proves nothing about the shadow header it
            projects into. One header per file in every state, by
            construction rather than by detection. The row is the one the
            oversized file-pair card keeps for the same reason, so the two
            read alike. Each section is a one-file patch, so Pierre and the
            streaming stand-in take it as they would the whole. */}
        {cardRow && (
          <PlainFilePairHeader
            filename={i18nT('components.diffBlock.file_count', { count: sections.length })}
            label={plainViewLabel}
            stats={totals}
            renderHeaderActions={headerControls}
          />
        )}
        {sections.map((section, i) => {
          const words = changeWords(section)
          return (
            <Fragment key={`${section.name ?? ''}:${i}`}>
              <PlainFilePairHeader
                filename={sectionTitle(section, i === 0 ? headerPath : null)}
                label={cardRow ? undefined : plainViewLabel}
                stats={section}
                renderHeaderFilenameSuffix={words.length > 0 ? () => changeNote(words) : undefined}
                renderHeaderActions={cardRow ? undefined : headerControls}
              />
              {/* No owner in plain-diff mode: the reader asked for the raw patch
                 and gets it verbatim. With colour on, every plain body under
                 the row — the streaming stand-in, the hold and Pierre's
                 stand-in for the diff — takes the hunks-only shape, and only
                 the last, rendered `degraded`, reports. */}
              <PlainPatchBodyContext.Provider value={plain ? null : plainBodyOwner}>
                {standInForStream
                  ? <PlainCodeFallback text={section.text} />
                  : <PierrePatch patch={section.text} options={options} />}
              </PlainPatchBodyContext.Provider>
            </Fragment>
          )
        })}
        {!complete && <div className="px-3 py-1 text-muted text-[12px] italic animate-pulse">{i18nT('components.diffBlock.generating_diff')}</div>}
      </div>
    </div>
  )
})
