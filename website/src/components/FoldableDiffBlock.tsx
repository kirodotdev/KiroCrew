import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { FileDiff } from 'lucide-react'

import DiffBlock, { extractFilePath } from './DiffBlock'
import { countDiffStats } from '../utils/diffLineCounts'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/**
 * A prose ```diff fence, COLLAPSED to a one-line chip by default.
 *
 * A message that changes several files carries several fences, and a fully
 * expanded patch is tens of rows each — so the prose the user actually came
 * for scrolls off. The chip states the same three facts a reader needs to
 * decide whether to look (which file, how much added, how much removed) in one
 * row, and the patch is one click away.
 *
 * A separate control from `ToolCallLine`'s card fold, which governs a diff the
 * dashboard itself rendered from a tool call. Both default CLOSED, but each
 * remembers its expansions in its own module-scope set, because the two are
 * keyed differently (a fence's `foldKey` vs a `tool_call_id`).
 *
 * Applied ONLY where the fence is a retelling: `MarkdownRenderer` opts in with
 * `collapseDiffs`, which the chat transcript sets and no other surface does. On
 * an artifact, spec, changelog or review page the patch IS the content, and
 * hiding it there would take it out of the DOM for find-in-page, whole-page
 * selection and printing.
 */

/**
 * Fences the user has opened, keyed by `foldKey`. Module-level for the same
 * reason `ToolCallLine.openedDiffCards` is: the transcript re-mounts messages
 * (load-earlier, variant switch, tab return), and an expansion that evaporates
 * on re-mount reads as the UI closing the patch while it is being read.
 * Session-scoped by intent — a reload starts collapsed again.
 */
const expandedDiffFences = new Set<string>()

/** Exported for tests: forget every remembered expansion. */
export function resetExpandedDiffFences(): void {
  expandedDiffFences.clear()
}

export default memo(function FoldableDiffBlock({ code, complete, onFileOpen, pathHint, streaming, foldKey }: {
  code: string
  complete: boolean
  onFileOpen?: (path: string) => void
  pathHint?: string
  streaming?: boolean
  /** Stable identity for remembering the open state; omit to keep it local. */
  foldKey?: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = useState(() => !!foldKey && expandedDiffFences.has(foldKey))
  const containerRef = useRef<HTMLDivElement>(null)
  // The region the chip controls. It is rendered whether or not it holds the
  // patch, so `aria-controls` always resolves to a real element — a disclosure
  // pointing at an id that exists only while open is not a disclosure.
  const regionId = useId()
  // The two halves of the toggle unmount each other, so the activated control
  // disappears and focus would fall to <body>. Hand focus to the counterpart
  // once it mounts — both carry data-diff-toggle.
  const pendingFocus = useRef(false)
  const toggle = useCallback(() => {
    setExpanded(prev => {
      const next = !prev
      if (foldKey) {
        if (next) expandedDiffFences.add(foldKey)
        else expandedDiffFences.delete(foldKey)
      }
      return next
    })
    pendingFocus.current = true
  }, [foldKey])
  useEffect(() => {
    if (!pendingFocus.current) return
    pendingFocus.current = false
    containerRef.current?.querySelector<HTMLElement>('[data-diff-toggle]')?.focus()
  }, [expanded])

  const stats = useMemo(() => countDiffStats(code), [code])
  const headerPath = useMemo(() => extractFilePath(code)?.path, [code]) ?? pathHint ?? null
  // The chip shows the basename; two changed files sharing a name would render
  // as identical chips, so the full path lives in the native tooltip.
  const basename = headerPath ? headerPath.split('/').pop() : null
  // An aria-label REPLACES the button's text, so the counts have to be in it:
  // without them a screen-reader user cannot hear how large the patch is
  // without opening it, which is the one decision the chip exists to support.
  const label = [
    headerPath
      ? i18nT('components.fileChangeChips.toggle_diff', { path: headerPath })
      : i18nT('components.markdownPanel.toggle_diff_view'),
    i18nT('components.fileChangeChips.removals', { count: stats.removed }),
    i18nT('components.fileChangeChips.additions', { count: stats.added }),
  ].join(' · ')

  return (
    <div ref={containerRef}>
      {!expanded && (
        <button
          type="button"
          className="my-1 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-[12px] leading-5 text-muted hover:text-text hover:border-border-strong cursor-pointer transition-colors focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none"
          data-diff-toggle
          data-testid="prose-diff-chip"
          title={headerPath ?? undefined}
          aria-expanded={false}
          aria-controls={regionId}
          aria-label={label}
          onClick={toggle}
        >
          <FileDiff size={12} className="shrink-0" aria-hidden />
          {basename && <span className="font-mono max-w-[240px] truncate">{basename}</span>}
          <span className="tabular-nums">
            {stats.removed > 0 && <span className="text-danger">-{stats.removed}</span>}
            {stats.removed > 0 && stats.added > 0 && ' '}
            {stats.added > 0 && <span className="text-ok">+{stats.added}</span>}
          </span>
        </button>
      )}
      <div id={regionId}>
        {expanded && (
          <DiffBlock code={code} complete={complete} onFileOpen={onFileOpen} pathHint={pathHint} streaming={streaming} onFold={toggle} />
        )}
      </div>
    </div>
  )
})
