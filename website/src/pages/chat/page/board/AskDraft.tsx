/**
 * A new conversation being started (PoC): the pin where the person pointed and an
 * input beside it, with a send arrow that lights up once there is text. Enter
 * sends, Esc cancels. Placed by its parent at the pin's screen position.
 */
import { ArrowUp } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { PinGlyph } from './ConductorThread'

interface Props {
  /** What it is about, shown as the field's hint: "To do", "Whole board". */
  about: string
  onSend: (text: string) => void
  onCancel: () => void
}

export default function AskDraft({ about, onSend, onCancel }: Props) {
  const [text, setText] = useState('')
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => { input.current?.focus() }, [])
  const send = () => {
    const t = text.trim()
    if (t) onSend(t)
  }
  return (
    <div data-testid="board-ask" className="flex items-center gap-2">
      <PinGlyph state="read" />
      <div className="flex w-[300px] items-center gap-2 rounded-full border border-accent bg-bg-elevated py-1 pl-3.5 pr-1 shadow-xl">
        <input ref={input} value={text} data-testid="board-ask-input" aria-label="Ask the conductor" placeholder={`Ask the conductor about ${about}`}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => {
            e.stopPropagation()
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); send() }
            else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
          }}
          className="min-w-0 flex-1 bg-transparent text-sm text-text outline-none placeholder:text-muted" />
        <button type="button" aria-label="Send" data-testid="board-ask-send" disabled={!text.trim()} onClick={send}
          className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-accent text-accent-fg disabled:bg-bg-hover disabled:text-muted">
          <ArrowUp size={15} />
        </button>
      </div>
    </div>
  )
}
