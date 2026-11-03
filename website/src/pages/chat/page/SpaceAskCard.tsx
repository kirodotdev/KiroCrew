/**
 * The ask card Space opens on an empty line of a doc (PoC), after Notion's: a full
 * width bar with the request field, and a line saying what it is about. Everything
 * is the keyboard's: Up and Down choose how many blocks above it is about, Enter
 * sends, Backspace on an empty field or Escape puts it away. Labels are hardcoded
 * English like the rest of the doc's conductor layer.
 */
import { ArrowUp, Sparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

interface Props {
  /** How many blocks above it is about; 0 is the spot itself. */
  blocks: number
  /** How many there are to take in. */
  max: number
  onGrow: () => void
  onShrink: () => void
  onSend: (text: string) => void
  onCancel: () => void
}

function aboutLine(blocks: number): string {
  if (blocks === 0) return 'Nothing selected: about the doc, answered here'
  return blocks === 1 ? 'About the block above' : `About the ${blocks} blocks above`
}

export default function SpaceAskCard({ blocks, max, onGrow, onShrink, onSend, onCancel }: Props) {
  const [text, setText] = useState('')
  const box = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { box.current?.focus() }, [])
  const send = () => { const t = text.trim(); if (t) onSend(t) }
  return (
    <div data-testid="space-ask" data-sa="root">
      <div data-sa="bar">
        <Sparkles size={16} aria-hidden data-sa="icon" />
        <textarea
          ref={box}
          rows={1}
          value={text}
          aria-label="Ask the conductor"
          data-testid="space-ask-input"
          placeholder="Ask the conductor to edit, explain or write…"
          onChange={e => {
            setText(e.target.value)
            // Grow with the text, a line at a time.
            e.target.style.height = 'auto'
            e.target.style.height = `${e.target.scrollHeight}px`
          }}
          onKeyDown={e => {
            e.stopPropagation()
            if (e.nativeEvent.isComposing) return
            const el = e.currentTarget
            const firstLine = !el.value.slice(0, el.selectionStart).includes('\n')
            const lastLine = !el.value.slice(el.selectionEnd).includes('\n')
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
            else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
            else if (e.key === 'Backspace' && el.value === '') { e.preventDefault(); onCancel() }
            else if (e.key === 'ArrowUp' && firstLine) { e.preventDefault(); onGrow() }
            else if (e.key === 'ArrowDown' && lastLine) { e.preventDefault(); onShrink() }
          }}
        />
        <button type="button" aria-label="Send" data-testid="space-ask-send" disabled={!text.trim()} onClick={send}
          onMouseDown={e => e.preventDefault()} data-sa="send">
          <ArrowUp size={14} />
        </button>
      </div>
      <div data-sa="hint" aria-live="polite">
        <span data-testid="space-ask-about">{aboutLine(blocks)}</span>
        <span data-sa="keys">
          {max > 0 && <><kbd>↑</kbd><kbd>↓</kbd> choose · </>}<kbd>Enter</kbd> send · <kbd>⌫</kbd> close
        </span>
      </div>
    </div>
  )
}
