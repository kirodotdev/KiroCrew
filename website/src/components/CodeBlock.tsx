import { memo, useCallback, useContext, useEffect, useMemo, useState, useRef } from 'react'
import { Copy, Check, MessageSquare, MessageSquarePlus } from 'lucide-react'
import type { LineAnnotation, SelectedLineRange } from '@pierre/diffs'
import { copyCode } from '../utils/clipboard'
import { PierreCode } from '../pierre'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'
import { useStagedMount, VIEWPORT_PRELOAD_MARGIN_PX } from './pierreStaging'
import { useNearViewport } from '../hooks/useNearViewport'
import { useMeasuredHeight } from '../hooks/useMeasuredHeight'
import { CodeCommentContext, holdCodeBlock, holdCodeChip, type CodeLineAnnotationMeta } from './codeComments'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { APPROVAL_COMMAND_TAG } from '../lib/approvalNotificationBody'

/** A block taller than this repeats its action row at the bottom, so copying
 *  or editing never costs a scroll back to the top. Fixed rather than
 *  viewport-relative: it matches the editor's own `max-h-[480px]` cap
 *  (EditableCodeBlock), keeps the check deterministic across window sizes,
 *  and avoids a resize listener for a threshold the content itself already
 *  measures via ResizeObserver. */
const TALL_CODE_BLOCK_PX = 480

/** Fence tags whose content is prose, not source code. A markdown paragraph is
 *  one long source line, so rendering it under `white-space: pre` turns every
 *  paragraph into a horizontal scrub — these tags soft-wrap instead. The set
 *  stays small and explicit: an unknown or missing tag is code and KEEPS the
 *  horizontal scroll (that is the reported requirement, not an oversight).
 *  `error-report` is the dashboard's own tag (utils/errorReport.prompt.ts):
 *  a `- Message: …` line is one long sentence, and clipping it at the bubble
 *  edge hid the very text the user asked the agent to diagnose. The approval
 *  command tag (lib/approvalNotificationBody.ts) is here for the same reason:
 *  the feed row that authorizes a command must show all of it, and a shell
 *  line wider than the feed column would otherwise scroll off the edge. */
const PROSE_LANGS = new Set(['markdown', 'md', 'text', 'txt', 'plaintext', 'plain', 'error-report', APPROVAL_COMMAND_TAG])
const isProseLang = (lang?: string) => !!lang && PROSE_LANGS.has(lang.toLowerCase())

/** Module constant so the options reference is stable across renders — Pierre
 *  diffs options/files by reference first (same pattern as CMD_CODE_OPTIONS in
 *  ToolDetails.tsx; the `file` object below is memoized for the same reason). */
const PROSE_CODE_OPTIONS = { overflow: 'wrap' } as const

/** The copy button, plus any caller-supplied actions (e.g. the pencil edit
 *  button), as one reusable row -- shared between the header and the footer
 *  duplicate so the two stay visually identical without a copy-pasted JSX
 *  block. */
function CodeBlockActions(
  { headerActions, copied, onCopy }: { headerActions?: React.ReactNode; copied: boolean; onCopy: () => void },
) {
  return (
    <div className={`flex items-center gap-1 opacity-0 group-hover/code:opacity-100 group-focus-within/code:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
      {headerActions}
      <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={onCopy} title={copied ? i18nT('components.codeBlock.copied') : i18nT('components.codeBlock.copy')} aria-label={copied ? i18nT('components.codeBlock.copied') : i18nT('components.codeBlock.copy')}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  )
}

/* Styling note for the gutter button and annotation chips: they render inside
 * Pierre's shadow DOM (the library portals render-prop content into its rows),
 * where the dashboard's stylesheets — Tailwind included — do not apply. They
 * must therefore style themselves INLINE. Theme CUSTOM PROPERTIES do inherit
 * through the shadow boundary, so colors ride `var(--accent)` etc. with
 * literal fallbacks. */
const chipBaseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  borderRadius: 9999,
  padding: '1px 8px',
  margin: '2px 0',
  fontSize: 12,
  lineHeight: '16px',
  fontFamily: 'inherit',
  cursor: 'pointer',
}

/** A code thread's count chip. It registers itself as the thread's live
 *  anchor, so the popover tracks it on scroll and can find it from the
 *  sidebar (it sits in Pierre's shadow DOM, out of `querySelector`'s reach). */
function ThreadChip(
  { meta, active, onActivate }: { meta: CodeLineAnnotationMeta; active: boolean; onActivate: (id: string) => void },
) {
  // Filled while unread or selected (the page's active thread, which stays
  // set after its popover closes), outline once read — mirrors the prose
  // overlay's bubble language. The label names which of the two filled it,
  // so the fill never has to be guessed.
  const filled = meta.unread || active
  const label = meta.unread
    ? i18nT('components.codeBlock.comment_thread_unread', { count: meta.count })
    : active
      ? i18nT('components.codeBlock.comment_thread_selected', { count: meta.count })
      : i18nT('components.codeBlock.comment_thread', { count: meta.count })
  const ref = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const el = ref.current
    return el ? holdCodeChip(meta.id, el) : undefined
  }, [meta.id])
  return (
    <button
      ref={ref}
      type="button"
      // CommentThreadPopover's click-away skips this marker (read through
      // the composed path), so re-clicking the open thread's chip keeps
      // the popover and its unsent reply.
      data-mc-code-chip=""
      aria-label={label}
      title={label}
      style={{
        ...chipBaseStyle,
        border: '1px solid var(--accent, #5a8cff)',
        background: filled ? 'var(--accent, #5a8cff)' : 'transparent',
        color: filled ? 'var(--accent-fg, #fff)' : 'var(--accent, #5a8cff)',
      }}
      onClick={() => onActivate(meta.id)}
    >
      <MessageSquare size={12} aria-hidden="true" />
      {meta.count}
    </button>
  )
}

/** The lines a gutter press comments on: the selected range when the hovered
 *  line is inside it, else the hovered line alone. */
function commentRange(hovered: number, selected: SelectedLineRange | null): { start: number; end: number } {
  if (!selected) return { start: hovered, end: hovered }
  const lo = Math.min(selected.start, selected.end)
  const hi = Math.max(selected.start, selected.end)
  return hovered >= lo && hovered <= hi ? { start: lo, end: hi } : { start: hovered, end: hovered }
}

const gutterLabel = (r: { start: number; end: number } | null) => (r && r.start !== r.end
  ? i18nT('components.codeBlock.comment_on_lines', { start: r.start, end: r.end })
  : i18nT('components.codeBlock.add_comment'))

/** GitHub-model gutter button: hovering a line shows it; clicking comments on
 *  {@link commentRange}. Pierre moves ONE button between rows, so its label is
 *  re-read on every pointer entry and focus, and names the range it acts on. */
function GutterButton(
  { getHoveredLine, selectedRef, onComment }: {
    getHoveredLine: () => { lineNumber: number } | undefined
    selectedRef: React.MutableRefObject<SelectedLineRange | null>
    onComment: (range: { start: number; end: number }, x: number, y: number) => void
  },
) {
  const rangeNow = () => {
    const hovered = getHoveredLine()?.lineNumber
    return hovered == null ? null : commentRange(hovered, selectedRef.current)
  }
  const [label, setLabel] = useState(() => gutterLabel(rangeNow()))
  const refresh = () => setLabel(gutterLabel(rangeNow()))
  // Pierre's line selection listens for `pointerdown` on its `<pre>`, inside
  // the shadow root the button is slotted into, so it sees a press on the
  // button before React does. With a range selected, that press would start a
  // NEW one-line selection and drop the range it means to comment on, so a
  // native listener stops it first. With nothing selected the press passes
  // through: a drag that starts on the button is how a range is picked.
  const keepSelectionOnPress = useCallback((el: HTMLButtonElement | null) => {
    el?.addEventListener('pointerdown', e => { if (selectedRef.current) e.stopPropagation() })
  }, [selectedRef])
  return (
    <button
      type="button"
      ref={keepSelectionOnPress}
      aria-label={label}
      title={label}
      onPointerEnter={refresh}
      onPointerMove={refresh}
      onFocus={refresh}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 18,
        height: 18,
        border: 'none',
        borderRadius: 4,
        background: 'var(--accent, #5a8cff)',
        color: 'var(--accent-fg, #fff)',
        cursor: 'pointer',
        padding: 0,
        // Pierre's own utility button sits at z-index 4 over the line-number
        // column; without it the number span takes the click.
        position: 'relative',
        zIndex: 4,
        // Pierre's own button offset: it hangs past the number column's
        // right edge instead of covering the digits, so a drag that picks a
        // range can still start on the hovered line's number (Pierre ignores
        // a press on the utility itself).
        marginRight: 'calc(-1lh + 1ch)',
      }}
      onClick={e => {
        const r = rangeNow()
        if (r) onComment(r, e.clientX, e.clientY)
      }}
    >
      <MessageSquarePlus size={13} aria-hidden="true" />
    </button>
  )
}

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions, footerActions, sourceStartLine }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
    /** Actions for the footer duplicate, when it differs from the header's.
     *  Defaults to `headerActions`. Exists because the header and footer are
     *  separate rows under `max-two-buttons-per-row` (AUTOSDE) -- a header
     *  that already carries Run + Edit + Copy (legacy status, pre-existing)
     *  would push the NEW footer row over the 2-action cap if it just
     *  mirrored the header, so a caller with 2+ header actions passes a
     *  trimmed set here instead of letting the footer inherit all of them. */
    footerActions?: React.ReactNode
    /** 1-based line (in the enclosing markdown source) of this block's FIRST
     *  CONTENT line — the block assembler's `startLine`. Only set where the
     *  renderer runs with source positions (artifact pages); combined with a
     *  mounted `CodeCommentContext` it enables line-anchored commenting. */
    sourceStartLine?: number
  },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  // Await and check the result -- copyCode resolves false on the legacy
  // execCommand fallback reporting failure and never rejects, so the boolean is
  // the only failure signal. Flipping to the "Copied" tick unconditionally
  // would confirm a copy that never happened; mirrors the established pattern
  // in TailnetMobileCard.
  const copy = async () => {
    if ((await copyCode(code)) === false) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  const [contentRef, contentHeight] = useMeasuredHeight<HTMLDivElement>()
  const isTall = contentHeight > TALL_CODE_BLOCK_PX
  const prose = isProseLang(lang)
  // Stable file identity per (code, lang): Pierre diffs options/files by
  // reference first, so a fresh object every render would force re-renders.
  const file = useMemo(() => ({ name: `snippet.${lang || 'txt'}`, contents: code }), [code, lang])
  // Pierre HIGHLIGHTING is staged; the block itself is not. Mounting Pierre
  // costs ~90ms of main thread per block and a turn commits 4-5 at once —
  // measured on a real transcript: 21 long tasks (worst 441ms) in 12s of
  // scrolling, which is the reader's "scrolling卡顿". The stand-in below is the
  // REAL text at Pierre's exact metrics (not an empty bar), so a queued block
  // is readable immediately and the release restyles without moving layout.
  // The earlier attempt to stage whole blocks starved on scroll churn (every
  // remount re-queued); the latchKey makes admission one-way per content, so
  // only the FIRST mount pays the queue and remounts render instantly.
  // Viewport-gated: a block far from the viewport never even queues — the
  // burst the queue spreads out is the mount-everything commit, and most of
  // those blocks are off-screen. It joins the queue ~600px before the reader
  // reaches it, so the highlight usually lands before the block is seen.
  const nearRef = useRef<HTMLDivElement>(null)
  const near = useNearViewport(nearRef, `${VIEWPORT_PRELOAD_MARGIN_PX}px 0px`)
  const highlighted = useStagedMount(!complete, `cb\u0000${lang ?? ''}\u0000${code.length}\u0000${code.slice(0, 40)}`, !near)

  // ── Line-anchored commenting (artifact pages only) ───────────────────────
  // Selection capture and highlight painting for prose live in light DOM and
  // cannot reach Pierre's shadow-DOM rows, so code commenting rides Pierre's
  // own primitives instead: line selection + the gutter utility to CREATE,
  // `lineAnnotations` to SHOW. Inert (context null / no sourcepos) everywhere
  // but the artifact detail surface.
  const ctx = useContext(CodeCommentContext)
  // Bundle the narrowed values once: closures below capture `commenting.ctx`
  // (already non-null) instead of re-narrowing `ctx` through TS closure rules.
  const commenting = ctx != null && sourceStartLine != null && complete
    ? { ctx, blockStartLine: sourceStartLine }
    : null
  // The selection is CONTROLLED (Pierre's `selectedLines`) so a posted comment
  // can clear it. The ref mirrors the state: the gutter button's native
  // pointerdown listener and its click handler both read it, and neither may
  // see a stale render's value.
  const [selected, setSelected] = useState<SelectedLineRange | null>(null)
  const selectedRef = useRef<SelectedLineRange | null>(null)
  const select = useCallback((r: SelectedLineRange | null) => { selectedRef.current = r; setSelected(r) }, [])
  // A comment just posted on this block: drop the range it was made on, or
  // its shading would read as that comment's highlight. A range picked since
  // (a different one) is the reader's next pick and stays.
  const posted = ctx && sourceStartLine != null && ctx.postedFor?.blockStartLine === sourceStartLine
    ? ctx.postedFor
    : null
  const postedSeq = posted?.seq ?? null
  useEffect(() => {
    const cur = selectedRef.current
    if (!posted || !cur) return
    if (Math.min(cur.start, cur.end) === posted.start && Math.max(cur.start, cur.end) === posted.end) select(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fires once per post (seq)
  }, [postedSeq, select])
  const annotations = useMemo<LineAnnotation<unknown>[] | undefined>(() => {
    if (!commenting) return undefined
    const metas = commenting.ctx.annotationsFor(commenting.blockStartLine)
    if (metas.length === 0) return undefined
    return metas.map(m => ({ lineNumber: m.line, metadata: m }))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- commenting is derived per-render from ctx/sourceStartLine/complete
  }, [ctx, sourceStartLine, complete])
  // Prose tags keep their soft-wrap; commenting adds the interaction options
  // on top. Without commenting the stable module constant is passed as-is.
  const codeOptions = useMemo(
    () => commenting
      ? { ...(prose ? PROSE_CODE_OPTIONS : {}), enableLineSelection: true, onLineSelectionChange: select, onLineSelected: select, enableGutterUtility: true }
      : prose ? PROSE_CODE_OPTIONS : undefined,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only the boolean presence matters
    [commenting != null, prose],
  )
  const renderGutter = commenting
    ? (getHoveredLine: () => { lineNumber: number } | undefined) => (
      <GutterButton
        getHoveredLine={getHoveredLine}
        selectedRef={selectedRef}
        onComment={({ start, end }, x, y) => commenting.ctx.onCommentRange({ blockStartLine: commenting.blockStartLine, start, end, x, y })}
      />
    )
    : undefined
  const renderAnnotation = commenting
    ? (a: LineAnnotation<unknown>) => {
      const m = a.metadata as CodeLineAnnotationMeta
      return <ThreadChip meta={m} active={commenting.ctx.activeId === m.id} onActivate={commenting.ctx.onActivate} />
    }
    : undefined
  // Each routed thread's fallback anchor is this block, so a sidebar click on
  // a thread whose chip is not mounted yet still scrolls here.
  useEffect(() => {
    const el = nearRef.current
    if (!el || !annotations) return
    const releases = annotations.map(a => holdCodeBlock((a.metadata as CodeLineAnnotationMeta).id, el))
    return () => releases.forEach(r => r())
  }, [annotations])

  return (
    <div ref={nearRef} className="code-block group/code rounded-xl border border-border bg-bg-elevated overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1">
        <span className="text-muted text-[13px] font-mono">{lang || 'code'}</span>
        <CodeBlockActions headerActions={headerActions} copied={copied} onCopy={copy} />
      </div>
      {/* tabIndex=0 + role/label: a horizontally-scrollable region must be keyboard
          focusable so keyboard-only users can scroll it (axe scrollable-region-focusable).
          The region role is a labelled landmark, so the tabIndex here is intentional.
          A prose block no longer scrolls horizontally, but it keeps the focus stop:
          conditioning tabIndex on the tag would move keyboard behavior with content
          type, and a focusable non-scrolling region is harmless. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div ref={contentRef} className="pierre-surface" tabIndex={0} role="region" aria-label={lang ? `${lang} code` : 'code'}>
        {complete && highlighted ? (
          <PierreCode
            file={file}
            langHint={lang}
            options={codeOptions}
            lineAnnotations={annotations}
            selectedLines={commenting ? selected : undefined}
            renderAnnotation={renderAnnotation}
            renderGutterUtility={renderGutter}
          />
        ) : (
          /* `pierre-plain` is what makes the swap a restyle instead of a reflow.
             The utilities here LOSE to `.msg-content pre` (two selectors beat one
             class), which imposes 10px vertical padding and a 4px margin -- and
             the existing `.code-block>pre` reset misses this element because it
             sits inside `.pierre-surface`, not directly under the block. Measured
             in a browser: 4px of padding surplus plus 8px of unreset margin made
             every code block 12px taller than the Pierre surface it stands in
             for, so a row with three of them dropped 36px the moment its chunks
             resolved, moving a reader scrolling above it. This stand-in is the
             real code text -- a queued block is readable, never a bare bar.
             Prose tags wrap here too (mirrors PlainFilePairFallback's wraps
             branch), so the stand-in and the highlighted surface agree on line
             count and the swap stays a restyle in the prose case as well. */
          <pre className={`pierre-plain ${prose ? 'whitespace-pre-wrap break-words' : 'overflow-x-auto'} px-3 py-2 m-0`}><code className="text-[13px] font-mono leading-5">{code}</code></pre>
        )}
        {!complete && <div className="px-3 pb-2 text-muted text-[12px] italic animate-pulse">{i18nT('components.codeBlock.generating')}</div>}
      </div>
      {isTall && (
        // border-transparent at rest, group-hover reveals it with the actions:
        // an always-visible border painted an empty ~28px strip under every
        // tall block at rest (actions are opacity-0 until hover), which read
        // as missing content rather than a footer. The height is still
        // reserved unconditionally, so revealing the border on hover does not
        // shift layout.
        <div data-testid="code-block-footer" className={`flex items-center justify-end px-3 py-1 border-t border-transparent group-hover/code:border-border [@media(hover:none)]:border-border transition-colors ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
          <CodeBlockActions headerActions={footerActions ?? headerActions} copied={copied} onCopy={copy} />
        </div>
      )}
    </div>
  )
})
