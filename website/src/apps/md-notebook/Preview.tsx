/**
 * Markdown rendering with click-to-edit blocks.
 *
 * Deliberately line-based rather than a full parser: each source line maps to
 * one rendered block, which is what makes "click this paragraph to edit exactly
 * these source lines" possible. Fenced code is the one multi-line block.
 */
import type { CSSProperties, ReactNode } from 'react'
import {
  ACCENT,
  ACCENT_BG,
  FONT_MONO,
  HEADING_FG,
  HEADING_RAIL,
  HEADING_RAIL_GAP,
  HEADING_RAIL_INDENT,
  HEADING_RULE_SOFT,
  HEADING_RULE_STRONG,
  RAIL_X,
} from './constants'
import Clickable from '../../components/Clickable'
import { BlockEditor } from './BlockEditor'
import { CodeFenceBlock } from './CodeFenceBlock'
import { MermaidBlock } from './MermaidBlock'
import { NoteImage } from './NoteImage'
import {
  FM_RE,
  LIST_MARKER_RE,
  fenceLang,
  indentPx,
  parseMermaidBlock,
  parseTable,
  resolveNoteImageSrc,
} from './utils'
import type { ParsedTable, TableAlign } from './utils'
import type { EditRange } from './types'
import { urlTransform } from '../../utils/urlTransform'

/**
 * Inline spans: code, bold, italic, wikilinks, links and images.
 *
 * The image alternative sits last but still wins over the link one for
 * `![alt](src)`: its match starts at the `!`, one character earlier than the
 * link's `[`, and the engine takes the leftmost match before it ever considers
 * alternative order. Appending it therefore leaves every existing group number
 * untouched, which is why it is not spliced in beside the link branch.
 */
const INLINE_RE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[\[([^\][|]+?)(?:\|([^\]]+?))?\]\])|(\[([^\]]+)\]\(([^)]+)\))|(!\[([^\]]*)\]\(([^)]+)\))/g

/**
 * Render one line's inline markup to React nodes.
 *
 * `noteDir` is the directory of the note being read; it is what turns a
 * relative image source into a file the dashboard can serve. Optional because
 * every other span renders without it, so a caller that has no note context
 * (a test, a preview of loose text) still gets everything but relative images.
 */
export function inline(text: string, key: number | string, noteDir?: string): ReactNode[] {
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
    } else if (m[10]) {
      // An image whose source is refused (or relative with no note directory to
      // resolve it against) degrades to its alt text rather than to a broken
      // frame, matching what a refused link does just above. NoteImage owns
      // that presentation for both failures, so a refused source and a file
      // that vanished later cannot look different to the reader.
      nodes.push(
        <NoteImage key={k} src={resolveNoteImageSrc(m[12], noteDir)} alt={m[11]} rawSrc={m[12]} />,
      )
    }
    last = m.index + m[0].length
    i++
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

const HEADING_SIZES = ['1.802em', '1.602em', '1.424em', '1.266em', '1.125em', '1em']

const CELL_PAD = '5px 8px'
const CELL_BORDER = '1px solid var(--border)'

/**
 * Render a parsed table.
 *
 * Cells wrap rather than being held on one line: a note's tables carry prose,
 * and inside an 800px reading column nowrap would push almost every one of them
 * into horizontal scrolling. The wrapper still scrolls, so a genuinely wide
 * table stays reachable instead of overflowing the column.
 */
function tableNode(t: ParsedTable, key: number, noteDir?: string): ReactNode {
  const cell = (align: TableAlign, first: boolean): CSSProperties => ({
    padding: CELL_PAD,
    textAlign: align ?? 'left',
    verticalAlign: 'top',
    ...(first ? null : { borderLeft: CELL_BORDER }),
  })
  return (
    <div
      style={{
        overflowX: 'auto',
        border: CELL_BORDER,
        borderRadius: '6px',
        margin: '6px 0',
      }}
    >
      <table style={{ borderCollapse: 'collapse', width: '100%' }}>
        <thead>
          <tr>
            {t.header.map((text, c) => (
              <th
                key={c}
                style={{
                  ...cell(t.align[c], c === 0),
                  fontWeight: 600,
                  background: 'var(--card)',
                  borderBottom: CELL_BORDER,
                }}
              >
                {inline(text, `${key}-h${c}`, noteDir)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {t.rows.map((row, r) => (
            <tr key={r}>
              {row.map((text, c) => (
                <td
                  key={c}
                  style={{
                    ...cell(t.align[c], c === 0),
                    ...(r === 0 ? null : { borderTop: CELL_BORDER }),
                  }}
                >
                  {inline(text, `${key}-${r}-${c}`, noteDir)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

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
  /**
   * Directory of the note being read, absolute. Resolves its relative image
   * sources; without it those images fall back to their alt text.
   */
  noteDir?: string
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
  noteDir,
}: PreviewProps) {
  const body = content.replace(FM_RE, '')
  // `split('\n')` yields a trailing EMPTY segment whenever the body ends with a
  // newline — the near-universal case for a file out of a git-backed vault — and
  // for a body that is empty outright. That segment is not a line of content, so
  // it is dropped here rather than at the append region alone: the render loop
  // below keys blocks by index too, and leaving the phantom in would let the
  // trailing append region and the phantom's own block both match `editRange`
  // and mount two editors. Dropping it also puts `appendStart` AT the phantom's
  // slot instead of one past it, which is what appended a spurious blank line
  // (#3741). Exactly one segment goes, so a genuine trailing blank line stays.
  const rawLines = body.split('\n')
  const lines = rawLines[rawLines.length - 1] === '' ? rawLines.slice(0, -1) : rawLines
  const out: ReactNode[] = []
  let inCode = false
  let codeBuf: string[] = []
  let codeStart = 0
  /** Last line swallowed by a multi-line block, so the loop skips past it. */
  let skipTo = -1

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
    // Lines already consumed by a multi-line block (a table's delimiter row
    // and body, a mermaid fence) render as part of that block, not again on
    // their own.
    if (idx <= skipTo) return

    // Any line that is not a list item ends the list, so the rails stop there.
    // Checked up front because headings and code fences return early below.
    if (!LIST_MARKER_RE.test(line)) stack = []

    if (line.startsWith('```')) {
      if (!inCode) {
        // A closed ```mermaid fence renders as a diagram; clicking it opens
        // the fenced SOURCE, and an unclosed one falls through to the generic
        // code path below like any other run-away fence.
        const mermaid = parseMermaidBlock(lines, idx)
        if (mermaid) {
          skipTo = mermaid.end
          out.push(
            blk(
              idx,
              mermaid.end,
              <MermaidBlock code={mermaid.code} />,
              { fontSize: '12px', fontFamily: FONT_MONO },
              { split: false },
            ),
          )
          return
        }
      }
      if (inCode) {
        out.push(
          blk(
            codeStart,
            idx,
            <CodeFenceBlock code={codeBuf.join('\n')} lang={fenceLang(lines[codeStart])} />,
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

    // A table is the second multi-line block: clicking it opens the whole
    // source — header, delimiter and rows — because editing one row of pipes in
    // isolation from the header it aligns with is not a useful gesture. Enter
    // stays a literal newline there, so it adds a row instead of splitting the
    // block, and the editor uses the mono face that keeps the pipes lined up.
    const table = parseTable(lines, idx)
    if (table) {
      skipTo = table.end
      out.push(
        blk(
          idx,
          table.end,
          tableNode(table, idx, noteDir),
          { fontFamily: FONT_MONO },
          { split: false },
        ),
      )
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
                {inline(task[3], idx, noteDir)}
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
        color: HEADING_FG,
        marginTop: n <= 2 ? '14px' : '10px',
        marginRight: 0,
        marginBottom: n <= 2 ? '6px' : '4px',
        // The rail hangs in the column gutter so heading text stays flush with
        // body text; h1/h2 rule underneath instead and need no offset.
        marginLeft: n <= 2 ? 0 : `-${HEADING_RAIL_INDENT}px`,
      }
      // The accent is chrome, never the text colour — see HEADING_FG.
      // Longhand rather than the `border-bottom` shorthand on purpose: a
      // shorthand whose value contains color-mix() is dropped wholesale by a
      // strict CSS parser (jsdom does exactly that), losing the whole rule
      // rather than just its colour.
      if (n <= 2) {
        style.borderBottomWidth = n === 1 ? '2px' : '1px'
        style.borderBottomStyle = 'solid'
        style.borderBottomColor = n === 1 ? HEADING_RULE_STRONG : HEADING_RULE_SOFT
        style.paddingBottom = n === 1 ? '4px' : '3px'
      } else {
        style.borderLeftWidth = '2px'
        style.borderLeftStyle = 'solid'
        style.borderLeftColor = HEADING_RAIL
        style.paddingLeft = `${HEADING_RAIL_GAP}px`
      }
      out.push(
        // The editor inherits the heading's typography AND colour so the text
        // does not shift shade on click; the chrome is rendered-only, which is
        // what distinguishes the two states.
        blk(idx, idx, <Tag style={style}>{inline(head[2], idx, noteDir)}</Tag>, {
          fontSize: style.fontSize,
          fontWeight: style.fontWeight,
          lineHeight: style.lineHeight,
          color: style.color,
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
          blk(idx, idx, <div style={{ marginLeft: ind + 4 }}>{['• ', ...inline(li[2], idx, noteDir)]}</div>),
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
            <div style={{ marginLeft: ind + 4 }}>{[`${ol[2]}. `, ...inline(ol[3], idx, noteDir)]}</div>,
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
            {inline(line.slice(2), idx, noteDir)}
          </div>,
        ),
      )
      return
    }

    out.push(
      blk(
        idx,
        idx,
        line.trim() === '' ? <div style={{ height: '8px' }} /> : <div>{inline(line, idx, noteDir)}</div>,
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
      // Carries the heading accent custom properties declared in `styles.ts`;
      // without it the chrome resolves to nothing.
      className="mdnb-note"
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
