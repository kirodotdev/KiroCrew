import { createElement, useEffect, useState, type ReactElement } from 'react'
import { DecoratorNode, type EditorConfig, type LexicalNode, type SerializedLexicalNode } from 'lexical'

/**
 * The transient drop indicator opened under the pointer while a pill is dragged.
 *
 * It is its own inline `DecoratorNode` (not a CSS overlay) so it sits in the
 * text flow at exactly the offset the pill will land on, and it stays ZERO
 * width: the text around it does not move until the drop. It renders an accent
 * insertion caret that fades in after mount; `PillDragPlugin` removes it (after
 * fading it out) when the target changes or the drag ends. It never serializes
 * into the value (`getTextContent()` → '') and is not keyboard-selectable.
 *
 * This file is a `.ts` (not `.tsx`) per the phase-2 spec's fixed file list, so
 * its render uses `createElement` rather than JSX.
 */
export type SerializedDropGapNode = SerializedLexicalNode

/**
 * Rendered drop indicator: an insertion caret, not a let-open. The host span is
 * ZERO width so the surrounding text never moves while dragging; the 2px accent
 * line (with a soft halo) is absolutely centred on that point and fades in on
 * mount / out when the gap closes. `drop-gap` / `wide` stay as marker classes:
 * tests select on them and `PillDragPlugin` closes a gap by removing `wide`
 * directly, so the fade is bound to that class (`[&.wide]:opacity-100`) rather
 * than to React state.
 */
const GAP_CLASS =
  'drop-gap relative inline-block w-0 h-[1.25em] align-text-bottom overflow-visible opacity-0 [&.wide]:opacity-100 transition-opacity [transition-duration:120ms]'
const CARET_CLASS =
  'absolute -left-px top-0 h-full w-0.5 rounded-sm bg-accent shadow-[0_0_0_3px_var(--accent-subtle)]'

function DropGap(): ReactElement {
  const [wide, setWide] = useState(false)
  useEffect(() => {
    const id = requestAnimationFrame(() => setWide(true))
    return () => cancelAnimationFrame(id)
  }, [])
  return createElement(
    'span',
    { 'data-testid': 'drop-gap', className: `${GAP_CLASS}${wide ? ' wide' : ''}` },
    createElement('span', { 'aria-hidden': true, className: CARET_CLASS }),
  )
}

export class DropGapNode extends DecoratorNode<ReactElement> {
  static getType(): string {
    return 'drop-gap'
  }

  static clone(node: DropGapNode): DropGapNode {
    return new DropGapNode(node.__key)
  }

  static importJSON(_serialized: SerializedDropGapNode): DropGapNode {
    return $createDropGapNode()
  }

  exportJSON(): SerializedDropGapNode {
    return { type: 'drop-gap', version: 1 }
  }

  createDOM(): HTMLElement {
    const span = document.createElement('span')
    span.className = 'drop-gap-host'
    return span
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  isKeyboardSelectable(): boolean {
    return false
  }

  getTextContent(): string {
    return ''
  }

  decorate(_editor: unknown, _config: EditorConfig): ReactElement {
    return createElement(DropGap)
  }
}

export function $createDropGapNode(): DropGapNode {
  return new DropGapNode()
}

export function $isDropGapNode(node: LexicalNode | null | undefined): node is DropGapNode {
  return node instanceof DropGapNode
}
