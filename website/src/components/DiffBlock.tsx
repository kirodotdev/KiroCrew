import { memo, useState, useMemo, useEffect, useRef } from 'react'
import { Copy, Check, Columns2, Rows2 } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { fileReadUrl } from '../utils/fileReadUrl'
import { isSafePath } from '../utils/safePath'
import { parseDiffLines, DIFF_BG, DIFF_FG, DIFF_NUM, DIFF_EDGE, type DiffLine } from '../utils/diffUtils'
import UnchangedSeparator from './UnchangedSeparator'

import { i18nT } from '../i18n/t'

type DiffSegment = { kind: 'context'; lines: DiffLine[] } | { kind: 'line'; line: DiffLine }

/** Group consecutive context lines for collapsing. Returns segments: either a context group or individual change lines. */
function groupContextRuns(lines: DiffLine[]): DiffSegment[] {
  const segments: DiffSegment[] = []
  let ctxBuf: DiffLine[] = []
  const flushCtx = () => {
    if (ctxBuf.length > 0) { segments.push({ kind: 'context', lines: [...ctxBuf] }); ctxBuf = [] }
  }
  for (const l of lines) {
    if (l.type === 'context') { ctxBuf.push(l) }
    else { flushCtx(); segments.push({ kind: 'line', line: l }) }
  }
  flushCtx()
  return segments
}

/** Build side-by-side pairs from parsed diff lines. */
function buildSideBySide(lines: DiffLine[]): { left: DiffLine | null; right: DiffLine | null }[] {
  const pairs: { left: DiffLine | null; right: DiffLine | null }[] = []
  let i = 0
  while (i < lines.length) {
    const l = lines[i]
    if (l.type === 'meta' || l.type === 'hunk') {
      pairs.push({ left: l, right: l })
      i++
    } else if (l.type === 'context') {
      pairs.push({ left: l, right: l })
      i++
    } else if (l.type === 'del') {
      // Collect consecutive del, then consecutive add, pair them
      const dels: DiffLine[] = []
      while (i < lines.length && lines[i].type === 'del') { dels.push(lines[i]); i++ }
      const adds: DiffLine[] = []
      while (i < lines.length && lines[i].type === 'add') { adds.push(lines[i]); i++ }
      const max = Math.max(dels.length, adds.length)
      for (let j = 0; j < max; j++) {
        pairs.push({ left: dels[j] || null, right: adds[j] || null })
      }
    } else if (l.type === 'add') {
      pairs.push({ left: null, right: l })
      i++
    } else { i++ }
  }
  return pairs
}

const CTX_COLLAPSE_THRESHOLD = 20

/** Extract the target file path from diff meta lines.
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
 */
function extractFilePath(lines: DiffLine[]): { path: string; prefixStripped: boolean } | null {
  let plusFallback: string | null = null
  let minusGit: string | null = null
  let minusPlain: string | null = null
  let gitFallback: string | null = null
  const skip = (p: string) => !p || p === '/dev/null' || p === '-' || p === '+'
  for (const l of lines) {
    if (l.type !== 'meta') continue
    // +++ b/<path> — the strongest signal (git format, explicitly new file).
    const plusGitMatch = /^\+\+\+ b\/(.+?)(?:\s+|$)/.exec(l.content)
    if (plusGitMatch && !skip(plusGitMatch[1])) return { path: plusGitMatch[1], prefixStripped: true }
    // +++ <path> — plain unified diff.
    const plusPlainMatch = /^\+\+\+ ([^\s].+?)(?:\s+|$)/.exec(l.content)
    if (plusPlainMatch && !skip(plusPlainMatch[1]) && !plusFallback) {
      plusFallback = plusPlainMatch[1]
    }
    // --- a/<path> — old side, used only as fallback if no +++ found.
    const minusGitMatch = /^--- a\/(.+?)(?:\s+|$)/.exec(l.content)
    if (minusGitMatch && !skip(minusGitMatch[1]) && !minusGit) {
      minusGit = minusGitMatch[1]
    }
    // --- <path> — plain old side.
    const minusPlainMatch = /^--- ([^\s].+?)(?:\s+|$)/.exec(l.content)
    if (minusPlainMatch && !skip(minusPlainMatch[1]) && !minusPlain) {
      minusPlain = minusPlainMatch[1]
    }
    // diff --git a/X b/Y — fallback when no +++/--- present.
    if (!gitFallback) {
      const gitMatch = /^diff --git a\/.+ b\/(.+)/.exec(l.content)
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

export default memo(function DiffBlock({ code, complete, onFileOpen, pathHint, streaming }: { code: string; complete: boolean; onFileOpen?: (path: string) => void; pathHint?: string; streaming?: boolean }) {
  const [copied, setCopied] = useState(false)
  const [sideBySide, setSideBySide] = useState(false)
  const [expandedCtx, setExpandedCtx] = useState<Set<number>>(new Set())
  const lines = useMemo(() => parseDiffLines(code), [code])
  // Resolve the file path: prefer headers inside the diff, fall back to the
  // pathHint extracted from the surrounding chat text by MarkdownRenderer
  // (helps when a tool emits "Created /path/to/file:" before a
  // bare diff with no +++/--- headers).
  const extracted = useMemo(() => extractFilePath(lines), [lines])
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
  const hasLineNums = lines.some(l => l.oldNum !== undefined || l.newNum !== undefined)
  // Gutter width must fit the widest line number. A fixed 3.5ch fits only
  // 3 digits — at 4+ digits (line 1000+) the numbers overflow the column,
  // the old/new gutters visually collide ("10081008") and the column
  // separator is drawn through the digits. +1.5ch covers the pr-1 padding
  // and keeps small diffs at the previous 3.5ch minimum.
  const gutterCh = useMemo(() => {
    let digits = 2
    for (const l of lines) {
      if (l.oldNum !== undefined) digits = Math.max(digits, String(l.oldNum).length)
      if (l.newNum !== undefined) digits = Math.max(digits, String(l.newNum).length)
    }
    return digits + 1.5
  }, [lines])
  const gutterStyle = { width: `${gutterCh}ch` }

  // Stash onFileOpen in a ref so the effect below only depends on the probe
  // candidates. If onFileOpen were a direct dep, every parent re-render that
  // produced a new function reference would refire the effect →
  // setFilePath(null) → HEAD probe → setFilePath(path), causing the Open
  // button to flicker and reflowing the diff body by 1-2px each time.
  // usePanelState / useTouchedFiles now memoize their returns so the upstream
  // churn is mostly gone, but a ref here is cheap defense in depth and
  // protects future callers from re-introducing the same flicker.
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
  const segments = useMemo(() => groupContextRuns(lines), [lines])
  const sbsPairs = useMemo(() => sideBySide ? buildSideBySide(lines) : [], [lines, sideBySide])

  const copy = () => { copyToClipboard(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  const toggleCtx = (idx: number) => setExpandedCtx(prev => { const n = new Set(prev); if (n.has(idx)) n.delete(idx); else n.add(idx); return n })

  // Slim separator replacing the raw `@@` hunk header: the gutter line
  // numbers carry the position, so the header only needs to say how many
  // unchanged lines were skipped. First hunk (hidden === undefined) and
  // zero-gap hunks render nothing.
  const renderHunkSeparator = (line: DiffLine, key: number) => {
    if (line.hidden === undefined || line.hidden <= 0) return null
    return <UnchangedSeparator key={key} count={line.hidden} />
  }

  const renderUnifiedLine = (line: DiffLine, key: number) => {
    if (line.type === 'meta') return null
    if (line.type === 'hunk') return renderHunkSeparator(line, key)
    return (
    <div key={key} className={`ft-drow flex text-[13px] font-mono leading-relaxed ${DIFF_BG[line.type]}${line.type === 'add' || line.type === 'del' ? ` ${DIFF_EDGE[line.type]}` : ''}`}>
      {hasLineNums && <span style={gutterStyle} className={`select-none text-right shrink-0 pr-1 border-r border-border ${DIFF_NUM[line.type]}`}>{(line.type === 'del' ? line.oldNum : line.newNum) ?? ''}</span>}
      <span className={`px-2 flex-1 min-w-0 whitespace-pre-wrap break-words ${DIFF_FG[line.type]}`}>{line.content || ' '}</span>
    </div>
  )
  }

  return (
    <div className="diff-block group/diff rounded-xl border border-border bg-bg-elevated overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1">
        <span className="text-muted text-[13px] font-mono">{i18nT('components.diffBlock.diff')}{headerPath && <span className="ml-1.5 text-muted/70">— {headerPath.split('/').pop()}</span>}</span>
        <div className="flex items-center gap-1 opacity-0 group-hover/diff:opacity-100 group-focus-within/diff:opacity-100 transition-opacity">
          {/* Open: hover-gated alongside the other diff actions for visual
              consistency. Plain "Open" label, no icon, since the diff header
              already prefixes the file name. */}
          {filePath && onFileOpen && (
            <button
              className="px-1.5 py-0.5 rounded text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
              onClick={() => onFileOpen(filePath)}
              title={i18nT('components.diffBlock.open_in_side_panel', { path: filePath })}
              aria-label={i18nT('components.diffBlock.open_in_side_panel', { path: filePath })}
            >
              {i18nT('components.diffBlock.open')}
            </button>
          )}
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={() => setSideBySide(!sideBySide)} title={sideBySide ? i18nT('components.diffBlock.unified_view') : i18nT('components.diffBlock.split_view')} aria-label={sideBySide ? i18nT('components.diffBlock.switch_to_unified_view') : i18nT('components.diffBlock.switch_to_split_view')}>{sideBySide ? <Rows2 size={13} /> : <Columns2 size={13} />}</button>
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.diffBlock.copied') : i18nT('components.diffBlock.copy_patch')} aria-label={copied ? i18nT('components.diffBlock.copied') : i18nT('components.diffBlock.copy_patch')}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
        </div>
      </div>
      {/* Forced line wrap: these diffs render in width-constrained surfaces
          (chat column, side panels), so long lines wrap instead of introducing
          a horizontal scrollbar — the editor diff is the full-width surface. */}
      <pre className="p-0">
        <div className={streaming ? 'ft-stream-block' : undefined}>
        {sideBySide ? (
          /* Side-by-side view */
          sbsPairs.map((pair, i) => {
            const left = pair.left
            const right = pair.right
            const lType = left?.type || 'context'
            const rType = right?.type || 'context'
            // Meta lines are dropped; hunk headers become the slim separator.
            if (lType === 'meta') return null
            if (lType === 'hunk') return left ? renderHunkSeparator(left, i) : null
            return (
              <div key={i} className="flex text-[13px] font-mono leading-relaxed">
                <div className={`w-1/2 flex overflow-hidden border-r border-border ${left ? `${DIFF_BG[lType]}${lType === 'del' ? ` ${DIFF_EDGE.del}` : ''}` : ''}`}>
                  {hasLineNums && <span style={gutterStyle} className={`select-none text-right shrink-0 pr-1 border-r border-border ${left ? DIFF_NUM[lType] : ''}`}>{left?.oldNum ?? ''}</span>}
                  <span className={`px-2 flex-1 min-w-0 whitespace-pre-wrap break-words ${left ? DIFF_FG[lType] : 'text-muted'}`}>{left?.content || ' '}</span>
                </div>
                <div className={`w-1/2 flex overflow-hidden ${right ? `${DIFF_BG[rType]}${rType === 'add' ? ` ${DIFF_EDGE.add}` : ''}` : ''}`}>
                  {hasLineNums && <span style={gutterStyle} className={`select-none text-right shrink-0 pr-1 border-r border-border ${right ? DIFF_NUM[rType] : ''}`}>{right?.newNum ?? ''}</span>}
                  <span className={`px-2 flex-1 min-w-0 whitespace-pre-wrap break-words ${right ? DIFF_FG[rType] : 'text-muted'}`}>{right?.content || ' '}</span>
                </div>
              </div>
            )
          })
        ) : (
          /* Unified view with collapsible context */
          segments.map((seg, si) => {
            if (seg.kind === 'line') return renderUnifiedLine(seg.line, si)
            // Context group — collapse if large
            const ctxLines = seg.lines
            if (ctxLines.length <= CTX_COLLAPSE_THRESHOLD) return ctxLines.map((l, li) => renderUnifiedLine(l, si * 10000 + li))
            if (expandedCtx.has(si)) {
              return <div key={si}>
                {ctxLines.map((l, li) => renderUnifiedLine(l, si * 10000 + li))}
                <div className="px-3 py-0.5 text-[12px] text-muted cursor-pointer hover:text-text bg-bg-hover/50" role="button" tabIndex={0} aria-expanded="true" onClick={() => toggleCtx(si)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCtx(si) } }}>{i18nT('components.diffBlock.collapse')} {ctxLines.length} {i18nT('components.diffBlock.context_lines')}</div>
              </div>
            }
            // Show first 2 + last 2, collapse middle
            const hidden = ctxLines.length - 4
            return <div key={si}>
              {ctxLines.slice(0, 2).map((l, li) => renderUnifiedLine(l, si * 10000 + li))}
              <div className="px-3 py-0.5 text-[12px] text-muted cursor-pointer hover:text-text bg-bg-hover/50 select-none" role="button" tabIndex={0} aria-expanded="false" onClick={() => toggleCtx(si)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCtx(si) } }}>▼ {hidden} {i18nT('components.diffBlock.lines_hidden')}</div>
              {ctxLines.slice(-2).map((l, li) => renderUnifiedLine(l, si * 10000 + 9000 + li))}
            </div>
          })
        )}
        {!complete && <div className="px-3 py-1 text-muted text-[12px] italic animate-pulse">{i18nT('components.diffBlock.generating_diff')}</div>}
        </div>
      </pre>
    </div>
  )
})
