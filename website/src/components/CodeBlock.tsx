import { memo, useMemo, useState , useRef } from 'react'
import { Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { PierreCode } from '../pierre'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'
import { useStagedMount, VIEWPORT_PRELOAD_MARGIN_PX } from './pierreStaging'
import { useNearViewport } from '../hooks/useNearViewport'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
  },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  const copy = () => { copyCode(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }
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
        <div className={`flex items-center gap-1 opacity-0 group-hover/code:opacity-100 group-focus-within/code:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
          {headerActions}
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.codeBlock.copied') : i18nT('components.codeBlock.copy')} aria-label={copied ? i18nT('components.codeBlock.copied') : i18nT('components.codeBlock.copy')}>
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
      </div>
      {/* tabIndex=0 + role/label: a horizontally-scrollable region must be keyboard
          focusable so keyboard-only users can scroll it (axe scrollable-region-focusable).
          The region role is a labelled landmark, so the tabIndex here is intentional. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div className="pierre-surface scroll-fade" tabIndex={0} role="region" aria-label={lang ? `${lang} code` : 'code'}>
        {complete && highlighted ? (
          <PierreCode file={file} langHint={lang} />
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
             real code text -- a queued block is readable, never a bare bar. */
          <pre className="pierre-plain overflow-x-auto px-3 py-2 m-0"><code className="text-[13px] font-mono leading-5">{code}</code></pre>
        )}
        {!complete && <div className="px-3 pb-2 text-muted text-[12px] italic animate-pulse">{i18nT('components.codeBlock.generating')}</div>}
      </div>
    </div>
  )
})
