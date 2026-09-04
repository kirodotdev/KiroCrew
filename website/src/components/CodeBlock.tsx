import { memo, useContext, useMemo, useState } from 'react'
import { Copy, Check, MessageSquare, MessageSquarePlus } from 'lucide-react'
import type { LineAnnotation, SelectedLineRange } from '@pierre/diffs'
import { copyCode } from '../utils/clipboard'
import { PierreCode } from '../pierre'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'
import { CodeCommentContext, type CodeLineAnnotationMeta } from './codeComments'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

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

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions, sourceStartLine }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
    /** 1-based line (in the enclosing markdown source) of this block's FIRST
     *  CONTENT line — the block assembler's `startLine`. Only set where the
     *  renderer runs with source positions (artifact pages); combined with a
     *  mounted `CodeCommentContext` it enables line-anchored commenting. */
    sourceStartLine?: number
  },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  const copy = () => { copyCode(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  // Stable file identity per (code, lang): Pierre diffs options/files by
  // reference first, so a fresh object every render would force re-renders.
  const file = useMemo(() => ({ name: `snippet.${lang || 'txt'}`, contents: code }), [code, lang])

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
  const [selected, setSelected] = useState<SelectedLineRange | null>(null)
  const annotations = useMemo<LineAnnotation<unknown>[] | undefined>(() => {
    if (!commenting) return undefined
    const metas = commenting.ctx.annotationsFor(commenting.blockStartLine)
    if (metas.length === 0) return undefined
    return metas.map(m => ({ lineNumber: m.line, metadata: m }))
    // eslint-disable-next-line react-hooks/exhaustive-deps -- commenting is derived per-render from ctx/sourceStartLine/complete
  }, [ctx, sourceStartLine, complete])
  const interactionOptions = useMemo(
    () => commenting
      ? { enableLineSelection: true, onLineSelected: (r: SelectedLineRange | null) => setSelected(r), enableGutterUtility: true }
      : undefined,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only the boolean presence matters
    [commenting != null],
  )
  // GitHub-model gutter button: hovering a line shows it; clicking comments on
  // the hovered line — or on the selected RANGE when the hovered line is part
  // of one (which is how a multi-line comment is made).
  const renderGutter = commenting
    ? (getHoveredLine: () => { lineNumber: number } | undefined) => (
      <button
        type="button"
        aria-label={i18nT('components.codeBlock.add_comment')}
        title={i18nT('components.codeBlock.add_comment')}
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
        }}
        onClick={e => {
          const hovered = getHoveredLine()?.lineNumber
          if (hovered == null) return
          const lo = selected ? Math.min(selected.start, selected.end) : hovered
          const hi = selected ? Math.max(selected.start, selected.end) : hovered
          const inRange = hovered >= lo && hovered <= hi
          commenting.ctx.onCommentRange({
            blockStartLine: commenting.blockStartLine,
            start: inRange ? lo : hovered,
            end: inRange ? hi : hovered,
            x: e.clientX,
            y: e.clientY,
          })
        }}
      >
        <MessageSquarePlus size={13} aria-hidden="true" />
      </button>
    )
    : undefined
  const renderAnnotation = commenting
    ? (a: LineAnnotation<unknown>) => {
      const m = a.metadata as CodeLineAnnotationMeta
      const active = commenting.ctx.activeId === m.id
      // Filled while unread or active, outline once read — mirrors the prose
      // overlay's bubble language so the two comment surfaces read as one.
      const filled = m.unread || active
      return (
        <button
          type="button"
          aria-label={i18nT('components.codeBlock.comment_thread', { count: m.count })}
          title={i18nT('components.codeBlock.comment_thread', { count: m.count })}
          style={{
            ...chipBaseStyle,
            border: '1px solid var(--accent, #5a8cff)',
            background: filled ? 'var(--accent, #5a8cff)' : 'transparent',
            color: filled ? 'var(--accent-fg, #fff)' : 'var(--accent, #5a8cff)',
          }}
          onClick={() => commenting.ctx.onActivate(m.id)}
        >
          <MessageSquare size={12} aria-hidden="true" />
          {m.count}
        </button>
      )
    }
    : undefined

  return (
    <div className="code-block group/code rounded-xl border border-border bg-bg-elevated overflow-hidden">
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
        {complete ? (
          <PierreCode
            file={file}
            langHint={lang}
            options={interactionOptions}
            lineAnnotations={annotations}
            renderAnnotation={renderAnnotation}
            renderGutterUtility={renderGutter}
          />
        ) : (
          <pre className="overflow-x-auto px-3 py-2 m-0"><code className="text-[13px] font-mono leading-relaxed">{code}</code></pre>
        )}
        {!complete && <div className="px-3 pb-2 text-muted text-[12px] italic animate-pulse">{i18nT('components.codeBlock.generating')}</div>}
      </div>
    </div>
  )
})
