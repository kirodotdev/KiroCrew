/**
 * Markdown rendering with click-to-edit blocks.
 *
 * Deliberately line-based rather than a full parser: each source line maps to
 * one rendered block, which is what makes "click this paragraph to edit exactly
 * these source lines" possible. Fenced code is the one multi-line block.
 */
import type { CSSProperties, ReactNode } from 'react'
import { ACCENT, ACCENT_BG, FONT_MONO, RAIL_X } from './constants'
import Clickable from '../../components/Clickable'
import { BlockEditor } from './BlockEditor'
import { FM_RE, LIST_MARKER_RE, indentPx } from './utils'
import type { EditRange } from './types'
import { urlTransform } from '../../utils/urlTransform'

/** Inline spans: code, bold, italic, wikilinks and links. */
const INLINE_RE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[\[([^\][|]+?)(?:\|([^\]]+?))?\]\])|(\[([^\]]+)\]\(([^)]+)\))/g

/** Render one line's inline markup to React nodes. */
export function inline(text: string, key: number | string): ReactNode[] {
  const nodes: ReactNode[] = []
  const re = new RegExp(INLINE_RE.source, 'g')
  let last = 0
  let i = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const k = `${key}-${i}`
    if (m[1]) {
      nodes.push(
        <code
          key={k}
          style={{
            background: 'var(--card)',
            border: '1px solid var(--border)',
            borderRadius: '4px',
            padding: '0 4px',
            fontSize: '0.9em',
            fontFamily: FONT_MONO,
          }}
        >
          {m[1].slice(1, -1)}
        </code>,
      )
    } else if (m[2]) {
      nodes.push(<strong key={k}>{m[2].slice(2, -2)}</strong>)
    } else if (m[3]) {
      nodes.push(<em key={k}>{m[3].slice(1, -1)}</em>)
    } else if (m[4]) {
      // Wikilink: shows the alias when given, else the target.
      nodes.push(
        <span key={k} style={{ color: ACCENT }}>
          {m[6] || m[5]}
        </span>,
      )
    } else if (m[7]) {
      // Sanitize the href. A markdown note is ordinary text a user can paste,
      // so `[open](javascript:...)` would otherwise render a link that runs
      // script in the dashboard's own origin when clicked. `urlTransform`
      // returns '' for a scheme it refuses, and a rejected link degrades to
      // plain text rather than silently pointing somewhere harmless-looking.
      const href = urlTransform(m[9])
      nodes.push(
        href ? (
          <a
            key={k}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            style={{ color: ACCENT }}
          >
            {m[8]}
          </a>
        ) : (
          <span key={k}>{m[8]}</span>
        ),
      )
    }
    last = m.index + m[0].length
    i++
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

const HEADING_SIZES = ['1.802em', '1.602em', '1.424em', '1.266em', '1.125em', '1em']

export interface PreviewProps {
  content: string
  onToggleCheckbox: (line: number) => void
  editRange: EditRange | null
  onStartEdit: (start: number, end: number) => void
  onCommitEdit: (text: string) => void
  onCancelEdit: () => void
  onSplitEdit: (before: string, after: string, caret: number) => void
  /** Mark the note dirty on the first edit keystroke, before commit. */
  onDirtyEdit?: () => void
}

export function Preview({
  content,
  onToggleCheckbox,
  editRange,
  onStartEdit,
  onCommitEdit,
  onCancelEdit,
  onSplitEdit,
  onDirtyEdit,
}: PreviewProps) {
  const body = content.replace(FM_RE, '')
  const lines = body.split('\n')
  const out: ReactNode[] = []
  let inCode = false
  let codeBuf: string[] = []
  let codeStart = 0

  // Stop block-edit activation for interactive children (checkboxes, links).
  const shield = (e: React.MouseEvent) => e.stopPropagation()

  /**
   * Wrap a rendered block so clicking it swaps in the editor for source lines
   * [start, end]. `split: false` keeps Enter a literal newline (fenced code).
   */
  const blk = (
    start: number,
    end: number,
    node: ReactNode,
    textStyle?: CSSProperties,
    opts?: { split?: boolean },
  ): ReactNode => {
    if (editRange && start === editRange.start) {
      return (
        <BlockEditor
          key={`edit-${start}`}
          initial={lines.slice(editRange.start, editRange.end + 1).join('\n')}
          onCommit={onCommitEdit}
          onCancel={onCancelEdit}
          onDirty={onDirtyEdit}
          onSplit={opts?.split === false ? null : onSplitEdit}
          textStyle={textStyle}
          caret={editRange.caret}
        />
      )
    }
    if (editRange && start > editRange.start && start <= editRange.end) return null
    return (
      <Clickable
        key={start}
        className="mdnb-blk"
        onClick={() => onStartEdit(start, end)}
        style={{ cursor: 'text', borderRadius: '4px', padding: '0 4px', margin: '0 -4px' }}
      >
        {node}
      </Clickable>
    )
  }

  /**
   * Nesting rails: a nested list item draws a vertical line at each ancestor's
   * indent, so stacked children form one continuous rail under the parent.
   * Drawn on the CHILDREN, which is why it begins below the parent row and
   * exists only when something is actually nested.
   */
  const withRails = (rails: number[], idx: number, el: ReactNode): ReactNode =>
    el && rails.length ? (
      <div key={`rail-${idx}`} style={{ position: 'relative' }}>
        {rails.map(x => (
          <div
            key={x}
            aria-hidden
            style={{
              position: 'absolute',
              left: `${x + RAIL_X}px`,
              top: 0,
              bottom: 0,
              width: '1px',
              background: 'var(--border)',
            }}
          />
        ))}
        {el}
      </div>
    ) : (
      el
    )

  // Indents of the enclosing list items, innermost last.
  let stack: number[] = []
  /** Ancestor indents for a list line at `ind`, updating the stack. */
  const enter = (ind: number): number[] => {
    while (stack.length && stack[stack.length - 1] >= ind) stack.pop()
    const rails = stack.slice()
    stack.push(ind)
    return rails
  }

  lines.forEach((line, idx) => {
    // Any line that is not a list item ends the list, so the rails stop there.
    // Checked up front because headings and code fences return early below.
    if (!LIST_MARKER_RE.test(line)) stack = []

    if (line.startsWith('```')) {
      if (inCode) {
        out.push(
          blk(
            codeStart,
            idx,
            <pre
              style={{
                background: 'var(--card)',
                border: '1px solid var(--border)',
                borderRadius: '6px',
                padding: '10px',
                fontSize: '12px',
                overflowX: 'auto',
                fontFamily: FONT_MONO,
              }}
            >
              {codeBuf.join('\n')}
            </pre>,
            { fontSize: '12px', fontFamily: FONT_MONO },
            { split: false },
          ),
        )
        codeBuf = []
      } else {
        codeStart = idx
      }
      inCode = !inCode
      return
    }
    if (inCode) {
      codeBuf.push(line)
      return
    }

    const task = /^(\s*)- \[( |x)\] (.*)$/.exec(line)
    if (task) {
      const ind = indentPx(task[1])
      const rails = enter(ind)
      out.push(
        withRails(
          rails,
          idx,
          blk(
            idx,
            idx,
            <div style={{ display: 'flex', gap: '6px', alignItems: 'baseline', marginLeft: ind }}>
              <input
                type="checkbox"
                checked={task[2] === 'x'}
                onClick={shield}
                onChange={() => onToggleCheckbox(idx)}
                aria-label={task[3] || 'task'}
                style={{ accentColor: ACCENT }}
              />
              <span
                style={
                  task[2] === 'x'
                    ? { color: 'var(--muted)', textDecoration: 'line-through' }
                    : undefined
                }
              >
                {inline(task[3], idx)}
              </span>
            </div>,
          ),
        ),
      )
      return
    }

    const head = /^(#{1,6}) (.*)$/.exec(line)
    if (head) {
      const n = head[1].length
      const Tag = `h${n}` as 'h1'
      const style: CSSProperties = {
        fontSize: HEADING_SIZES[n - 1],
        fontWeight: n <= 2 ? 700 : 600,
        lineHeight: 1.25,
        margin: n <= 2 ? '14px 0 6px' : '10px 0 4px',
      }
      out.push(
        blk(idx, idx, <Tag style={style}>{inline(head[2], idx)}</Tag>, {
          fontSize: style.fontSize,
          fontWeight: style.fontWeight,
          lineHeight: style.lineHeight,
        }),
      )
      return
    }

    const li = /^(\s*)[-*] (.*)$/.exec(line)
    if (li) {
      const ind = indentPx(li[1])
      out.push(
        withRails(
          enter(ind),
          idx,
          blk(idx, idx, <div style={{ marginLeft: ind + 4 }}>{['• ', ...inline(li[2], idx)]}</div>),
        ),
      )
      return
    }

    const ol = /^(\s*)(\d+)\. (.*)$/.exec(line)
    if (ol) {
      const ind = indentPx(ol[1])
      out.push(
        withRails(
          enter(ind),
          idx,
          blk(
            idx,
            idx,
            <div style={{ marginLeft: ind + 4 }}>{[`${ol[2]}. `, ...inline(ol[3], idx)]}</div>,
          ),
        ),
      )
      return
    }

    if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      out.push(
        blk(
          idx,
          idx,
          <hr style={{ border: 'none', borderTop: '1px solid var(--border)', pointerEvents: 'none' }} />,
        ),
      )
      return
    }

    if (line.startsWith('> ')) {
      out.push(
        blk(
          idx,
          idx,
          <div
            style={{
              borderLeft: `3px solid ${ACCENT_BG}`,
              paddingLeft: '8px',
              color: 'var(--muted)',
            }}
          >
            {inline(line.slice(2), idx)}
          </div>,
        ),
      )
      return
    }

    out.push(
      blk(
        idx,
        idx,
        line.trim() === '' ? <div style={{ height: '8px' }} /> : <div>{inline(line, idx)}</div>,
      ),
    )
  })

  // Trailing click-to-append region: clicking the empty space below the note
  // starts a new block at the end (an insertion — editRange with end < start).
  const appendStart = lines.length
  out.push(
    editRange && editRange.start === appendStart ? (
      <BlockEditor
        key="edit-append"
        initial=""
        onCommit={onCommitEdit}
        onCancel={onCancelEdit}
        onDirty={onDirtyEdit}
        onSplit={onSplitEdit}
      />
    ) : (
      <Clickable
        key="append"
        onClick={() => onStartEdit(appendStart, appendStart - 1)}
        style={{ minHeight: '80px', cursor: 'text' }}
      />
    ),
  )

  return (
    <div
      style={{
        fontSize: '13px',
        lineHeight: 1.55,
        display: 'flex',
        flexDirection: 'column',
        minHeight: '100%',
      }}
    >
      {out}
    </div>
  )
}
