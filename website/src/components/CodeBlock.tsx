import { memo, useMemo, useState , useRef } from 'react'
import { Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { PierreCode } from '../pierre'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'
import { useStagedMount, VIEWPORT_PRELOAD_MARGIN_PX } from './pierreStaging'
import { useNearViewport } from '../hooks/useNearViewport'
import { useMeasuredHeight } from '../hooks/useMeasuredHeight'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

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
 *  horizontal scroll (that is the reported requirement, not an oversight). */
const PROSE_LANGS = new Set(['markdown', 'md', 'text', 'txt', 'plaintext', 'plain'])
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

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions, footerActions }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
    /** Actions for the footer duplicate, when it differs from the header's.
     *  Defaults to `headerActions`. Exists because the header and footer are
     *  separate rows under `max-two-buttons-per-row` (AUTOSDE) -- a header
     *  that already carries Run + Edit + Copy (legacy status, pre-existing)
     *  would push the NEW footer row over the 2-action cap if it just
     *  mirrored the header, so a caller with 2+ header actions passes a
     *  trimmed set here instead of letting the footer inherit all of them. */
    footerActions?: React.ReactNode
  },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  // Await and check the result -- copyCode resolves false on the legacy
  // execCommand fallback reporting failure, and can still reject outright.
  // Flipping to the "Copied" tick unconditionally would confirm a copy that
  // never happened; mirrors the established pattern in TailnetMobileCard.
  const copy = async () => {
    try {
      if ((await copyCode(code)) === false) return
    } catch {
      return
    }
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
      <div ref={contentRef} className="pierre-surface scroll-fade" tabIndex={0} role="region" aria-label={lang ? `${lang} code` : 'code'}>
        {complete && highlighted ? (
          <PierreCode file={file} langHint={lang} options={prose ? PROSE_CODE_OPTIONS : undefined} />
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
