import { memo, useId } from 'react'
import { FileText, Pencil, X } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/** Longest snippet we hand to the DOM as visible text; the visible width is CSS-truncated. */
const SNIPPET_MAX_CHARS = 80

/**
 * The paste's first non-blank line with inner whitespace collapsed — uncapped.
 * Feeds the hover title, where the WHOLE line is the point (the visible label
 * is cut at `SNIPPET_MAX_CHARS`; a `title` attribute lays out nothing, so
 * length costs no rendering). Empty when the paste has no non-blank line.
 */
export function pasteFirstLine(content: string): string {
  for (const raw of content.split('\n')) {
    const line = raw.replace(/\s+/g, ' ').trim()
    if (line) return line
  }
  return ''
}

/**
 * First-line preview of a paste for the pill label: `pasteFirstLine` hard-capped
 * so a 10k-char first line never lands in the DOM as text. Empty when the paste
 * has no non-blank line.
 */
export function pasteSnippet(content: string): string {
  const line = pasteFirstLine(content)
  return line.length > SNIPPET_MAX_CHARS ? line.slice(0, SNIPPET_MAX_CHARS) + '…' : line
}

export interface PasteBlockChipProps {
  seq: number
  lines: number
  /** First-line preview shown as the pill's label (see `pasteSnippet`). Falls
   *  back to the generic "Pasted text" label when empty/omitted. */
  snippet?: string
  /** The whole normalized first line (see `pasteFirstLine`), for the hover
   *  title; defaults to `snippet`. */
  firstLine?: string
  /** Visual state. `dragging` dims the chip; `selected` shows an accent ring. */
  state?: 'idle' | 'dragging' | 'selected'
  /** Click on the chip body (never the ✕). Passed the chip element for anchoring. */
  onOpen?: (el: HTMLElement) => void
  /** The ✕ button, and Backspace/Delete while the chip itself has focus. */
  onRemove?: () => void
  /** ←/→ while the chip has focus: hand the caret back to the host, before/after the chip. */
  onEscape?: (side: 'before' | 'after') => void
  /** Passthrough for the drag wiring phase 2 adds; presentational only here. */
  draggable?: boolean
  /** Tab-focusable (default). Inside the editor pass false: a focusable chip steals
   *  DOM focus from the contenteditable and Lexical loses its selection. */
  focusable?: boolean
  className?: string
}

/**
 * The inline paste pill rendered in the composer at a paste token's position:
 * a FileText icon, a first-line snippet of the pasted content (so pills are
 * telling apart at a glance) with the line count, and a ✕ to remove.
 *
 * Names and hints (UX review on #11100):
 * - The accessible name LEADS with the visible snippet — "def main(): · Pasted
 *   text · 6 lines" — so a voice-control user can activate the pill by what
 *   they see; without a snippet it is the generic "Pasted text · N lines".
 * - The hover title carries the WHOLE first line (the label is cut at 80
 *   chars) and then names the two actions the pill hides — "Click to edit ·
 *   drag to reorder" — because a label that merely restates itself leaves the
 *   feature discoverable only by trying. The same hint is the pill's
 *   `aria-describedby` text.
 *
 * Presentational only — it imports no Lexical. Phase 2's `PasteBlockNode`
 * decorates a `<PasteBlockChip>` and wires `draggable` + the drag handlers; the
 * chip's job is geometry, states, and keyboard/click semantics.
 *
 * Geometry matches the non-image file chip in `ChatInput`'s `FilePreviewStrip`
 * (`px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text`).
 * The whole chip is a `role="button"` so Enter/Space open the preview; the ✕
 * stops propagation so removing never also opens.
 */
function PasteBlockChip({
  seq,
  lines,
  snippet,
  firstLine,
  state = 'idle',
  onOpen,
  onRemove,
  onEscape,
  draggable,
  focusable = true,
  className,
}: PasteBlockChipProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const hintId = useId()
  const label = i18nT('components.pasteBlockChip.label', { count: lines })
  const count = i18nT('components.pasteBlockChip.count', { count: lines })
  const actions = i18nT('components.pasteBlockChip.actions')
  // Snippet first, so the spoken/voice-control name starts with the visible text.
  const name = snippet ? `${snippet} · ${label}` : label
  const title = `${firstLine || snippet || label}\n${actions}`

  const stateClass =
    state === 'dragging'
      ? 'opacity-40'
      : state === 'selected'
        ? 'ring-1 ring-accent border-accent'
        : 'hover:border-accent'

  const open = (e: { currentTarget: HTMLElement }) => {
    onOpen?.(e.currentTarget)
  }

  return (
    <span
      role="button"
      tabIndex={focusable ? 0 : -1}
      data-paste-seq={seq}
      data-testid={`paste-token-${seq}`}
      draggable={draggable}
      aria-label={name}
      aria-describedby={hintId}
      title={title}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open(e)
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
          if (!onEscape) return
          e.preventDefault()
          e.stopPropagation()
          onEscape(e.key === 'ArrowLeft' ? 'before' : 'after')
        } else if (e.key === 'Backspace' || e.key === 'Delete') {
          if (!onRemove) return
          e.preventDefault()
          e.stopPropagation()
          onRemove()
        }
      }}
      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text cursor-pointer select-none align-baseline transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent ${stateClass}${className ? ` ${className}` : ''}`}
    >
      <FileText size={12} aria-hidden className="shrink-0 text-accent" />
      {snippet ? (
        <span className="inline-flex items-baseline gap-1 min-w-0">
          <span className="truncate max-w-[140px]" data-testid="paste-chip-snippet">{snippet}</span>
          <span className="shrink-0 text-muted">· {count}</span>
        </span>
      ) : (
        <span>{label}</span>
      )}
      <span id={hintId} className="sr-only">{actions}</span>
      {/* Editability cue for keyboard, touch and non-hovering users (UX review on
          #11100): the title hint reaches only pointer users who pause. Decorative
          — the pill itself is the button and its description names the action. */}
      <Pencil size={11} aria-hidden data-testid="paste-chip-edit-glyph" className="shrink-0 text-muted" />
      <button
        type="button"
        tabIndex={-1}
        aria-label={i18nT('components.pasteBlockChip.remove')}
        title={i18nT('components.pasteBlockChip.remove')}
        className="text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0 flex items-center"
        onClick={(e) => {
          e.stopPropagation()
          onRemove?.()
        }}
        onKeyDown={(e) => { e.stopPropagation() }}
        onMouseDown={(e) => { e.stopPropagation() }}
      >
        <X size={12} aria-hidden />
      </button>
    </span>
  )
}

export default memo(PasteBlockChip)
