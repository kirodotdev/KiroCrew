/** In-place editors: one markdown block, and the note's inline title. */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import Clickable from '../../components/Clickable'
import { i18nT } from '../../i18n/t'
import {
  ACCENT_BG,
  DOC_BODY_LINE_HEIGHT,
  DOC_BODY_PX,
  DOC_H1_PX,
  DOC_HEADING_WEIGHTS,
  FONT_BODY,
} from './constants'
import { carriedMarker, isEmptyListItem, shiftListItem } from './utils'
import { useImeGuard } from '../../hooks/useImeGuard'

export interface BlockEditorProps {
  initial: string
  onCommit: (text: string) => void
  onCancel: () => void
  /** Fired on the FIRST keystroke so the parent marks the note dirty before the
   * blur/commit — otherwise a capture-phase sync shortcut sees dirty=false,
   * reloads the note, and discards the in-progress block edit. */
  onDirty?: () => void
  /** Absent for blocks that are inherently multi-line, e.g. fenced code. */
  onSplit?: ((before: string, after: string, caret: number) => void) | null
  textStyle?: CSSProperties
  /** Column to land the caret on, past any carried list marker. */
  caret?: number
}

/**
 * Editor for one block of markdown source.
 *
 * Enter ENDS the block and opens a fresh one below (via `onSplit`) rather than
 * growing this one, so the next line can take its own format. Shift+Enter keeps
 * the older behaviour — a literal newline inside the same block.
 */
export function BlockEditor({
  initial,
  onCommit,
  onCancel,
  onDirty,
  onSplit,
  textStyle,
  caret,
}: BlockEditorProps) {
  const ime = useImeGuard()
  const [text, setText] = useState(initial)
  const ref = useRef<HTMLTextAreaElement>(null)
  // Set once we hand off to a new block, so the blur that follows unmounting
  // does not also commit this text over the range we just rewrote.
  const handedOff = useRef(false)

  // Grow to fit the WRAPPED content. A markdown paragraph is a single logical
  // line, so sizing by newline count would clip it to one visible row and hide
  // the rest; measuring scrollHeight keeps the block at its rendered height.
  const autoSize = useCallback(() => {
    const ta = ref.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
  }, [])

  useEffect(() => {
    const ta = ref.current
    if (!ta) return
    autoSize()
    // A block created by splitting starts the caret at an explicit column —
    // past any list marker carried down, not before it.
    const pos = typeof caret === 'number' ? Math.min(caret, ta.value.length) : ta.value.length
    ta.focus()
    ta.setSelectionRange(pos, pos)
  }, [autoSize, caret])

  // Replace the text and place the caret in one go. Writing the DOM node
  // directly keeps the caret from jumping to the end before React re-renders;
  // the state update that follows matches, so the control stays consistent.
  const rewrite = useCallback(
    (ta: HTMLTextAreaElement, next: string, pos: number) => {
      ta.value = next
      ta.setSelectionRange(pos, pos)
      setText(next)
      onDirty?.()
      autoSize()
    },
    [autoSize, onDirty],
  )

  return (
    <textarea
      ref={ref}
      value={text}
      spellCheck={false}
      rows={1}
      aria-label={i18nT('apps.mdNotebook.editor.blockAria')}
      onChange={e => {
        setText(e.target.value)
        onDirty?.()
        autoSize()
      }}
      {...ime.bindComposition<HTMLTextAreaElement>({
        onBlur: () => {
          if (!handedOff.current) onCommit(text)
        },
      })}
      onKeyDown={e => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          onCancel()
        } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          // Claim BEFORE the blur: a committing IME Enter must not commit the
          // block, and on a textarea the declined key must ALSO be consumed or
          // the browser inserts a literal newline into the draft (R3).
          if (!ime.claimEnter(e)) return
          e.currentTarget.blur()
        } else if (e.key === 'Tab') {
          // Tab nests a list item, Shift+Tab lifts it out. On anything that is
          // not a list item Tab keeps its native job of moving focus, so the
          // keyboard can still leave the editor.
          const ta = e.currentTarget
          const next = shiftListItem(text, ta.selectionStart, e.shiftKey)
          if (!next) return
          e.preventDefault()
          rewrite(ta, next.text, next.pos)
        } else if (e.key === 'Enter' && !e.shiftKey && onSplit) {
          // Shift+Enter falls through to the browser: a soft break in this block.
          if (!ime.claimEnter(e)) return
          const ta = e.currentTarget
          const before = text.slice(0, ta.selectionStart)
          const after = text.slice(ta.selectionEnd)
          // Enter on an empty list item leaves the list rather than adding
          // another empty one — the marker is dropped and the caret stays put.
          if (isEmptyListItem(before, after)) {
            setText('')
            autoSize()
            return
          }
          const prefix = carriedMarker(before)
          handedOff.current = true
          onSplit(before, prefix + after, prefix.length)
        }
      }}
      style={{
        width: '100%',
        boxSizing: 'border-box',
        resize: 'none',
        border: `1px solid ${ACCENT_BG}`,
        outline: 'none',
        background: 'var(--bg-elevated)',
        color: 'var(--text)',
        borderRadius: '4px',
        padding: '2px 6px',
        display: 'block',
        overflow: 'hidden',
        // Typography defaults to body text so an edited paragraph keeps its
        // rendered look; blocks with their own scale override it.
        fontSize: `${DOC_BODY_PX}px`,
        fontFamily: FONT_BODY,
        lineHeight: DOC_BODY_LINE_HEIGHT,
        ...textStyle,
      }}
    />
  )
}

/**
 * Inline title, Obsidian-style: the note's FILENAME rendered as the first line
 * of the document rather than tucked into the app chrome. Click to edit;
 * committing renames the file on disk.
 *
 * It is deliberately not part of the markdown text — the file's own first line
 * stays whatever the user wrote, so nothing is consumed or hidden.
 */
export function InlineTitle({
  path,
  onRename,
  mb = '8px',
}: {
  path: string
  onRename: (next: string) => void
  mb?: string
}) {
  const ime = useImeGuard()
  const name = (path.split('/').pop() ?? path).replace(/\.md$/i, '')
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(name)
  const ref = useRef<HTMLTextAreaElement>(null)

  // Switching notes must never carry an open edit (or a stale value) across.
  useEffect(() => {
    setEditing(false)
    setValue(name)
  }, [path, name])

  const autoSize = useCallback(() => {
    const ta = ref.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
  }, [])

  useEffect(() => {
    if (!editing || !ref.current) return
    autoSize()
    ref.current.focus()
    ref.current.select()
  }, [editing, autoSize])

  // Absolute px rather than the em heading ramp: this renders in the chrome
  // header, which sets no reading-column font-size for em to resolve against.
  // The VALUE is derived from that ramp (`DOC_H1_PX`), so the title tracks h1
  // instead of drifting from it the next time the reading base moves.
  const shared: CSSProperties = {
    fontSize: `${DOC_H1_PX}px`,
    fontWeight: DOC_HEADING_WEIGHTS[0],
    lineHeight: 1.25,
    fontFamily: FONT_BODY,
  }

  if (!editing) {
    return (
      <Clickable
        className="mdnb-blk"
        onClick={() => {
          setValue(name)
          setEditing(true)
        }}
        aria-label={i18nT('apps.mdNotebook.editor.renameHint')}
        title={i18nT('apps.mdNotebook.editor.renameHint')}
        style={{
          ...shared,
          cursor: 'text',
          borderRadius: '4px',
          padding: '0 4px',
          margin: `0 -4px ${mb}`,
          whiteSpace: 'normal',
          overflowWrap: 'anywhere',
        }}
      >
        {name}
      </Clickable>
    )
  }
  return (
    <textarea
      ref={ref}
      value={value}
      spellCheck={false}
      rows={1}
      aria-label={i18nT('apps.mdNotebook.editor.titleAria')}
      onChange={e => {
        setValue(e.target.value.replace(/\n/g, ''))
        autoSize()
      }}
      {...ime.bindComposition<HTMLTextAreaElement>({
        onBlur: () => {
          setEditing(false)
          if (value.trim() && value.trim() !== name) onRename(value)
        },
      })}
      onKeyDown={e => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          setValue(name)
          setEditing(false)
        } else if (e.key === 'Enter') {
          if (!ime.claimEnter(e)) return
          e.currentTarget.blur()
        }
      }}
      style={{
        ...shared,
        width: '100%',
        boxSizing: 'border-box',
        display: 'block',
        resize: 'none',
        overflow: 'hidden',
        background: 'var(--bg-elevated)',
        color: 'var(--text)',
        border: `1px solid ${ACCENT_BG}`,
        borderRadius: '4px',
        padding: '0 4px',
        margin: `0 -4px ${mb}`,
        outline: 'none',
      }}
    />
  )
}
