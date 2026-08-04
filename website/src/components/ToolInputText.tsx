import type { ReactNode } from 'react'
import { parseDiffLines, isDiffText, DIFF_BG, DIFF_FG } from '../utils/diffUtils'

/** Turn JSON string-escape whitespace (\n, \t, \r) into real characters so a
 *  multi-line command embedded in a JSON payload renders across lines in the
 *  surrounding <pre whitespace-pre-wrap>, instead of collapsing to one line
 *  littered with literal \n sequences. A genuine literal
 *  backslash-n in a shell command is JSON-encoded as \\n, whose leading \\ is
 *  consumed as its own escape pair first — so it is preserved as \n (not turned
 *  into a newline). Other escapes (\", \/) are left intact so the JSON stays
 *  syntactically legible and the highlighter's string matcher keeps working. */
function unescapeJsonWhitespace(s: string): string {
  return s.replace(/\\([\\ntr])/g, (_m, ch: string) => {
    switch (ch) {
      case '\\':
        return '\\' // collapse an escaped backslash pair to a single backslash
      case 'n':
        return '\n'
      case 't':
        return '\t'
      case 'r':
        return '\r'
      default:
        return _m
    }
  })
}

/** Inline tool input renderer with diff coloring and JSON syntax highlighting.
 *  Used in approval popups, activity viewer, and collapsed tool groups.
 *
 *  `raw` (default false) toggles the whitespace unescape: when false
 *  (Formatted), JSON string escapes \n/\t/\r render as real line breaks so a
 *  multi-line command is legible; when true (Raw), the text is highlighted but
 *  left byte-for-byte verbatim so the approver can inspect the exact payload,
 *  escaping and all. Both modes keep JSON/diff syntax highlighting. */
export function ToolInputText({ text, raw = false }: { text: string; raw?: boolean }): ReactNode {
  const trimmed = text.trimStart()
  // JSON-like highlighting — works on truncated JSON too
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    if (text.length > 50_000) return <span>{text}</span>
    // Formatted mode unescapes \n / \t so a multi-line command value renders
    // across real lines in the <pre whitespace-pre-wrap> host. Raw
    // mode leaves the payload verbatim for faithful pre-approval inspection.
    const jsonText = raw ? text : unescapeJsonWhitespace(text)
    const parts: ReactNode[] = []
    const re = /("(?:[^"\\]*(?:\\.[^"\\]*)*)")\s*:|("(?:[^"\\]*(?:\\.[^"\\]*)*)")|(true|false|null)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g
    let last = 0
    let m: RegExpExecArray | null
    let idx = 0
    while ((m = re.exec(jsonText)) !== null) {
      if (m.index > last) parts.push(<span key={idx++}>{jsonText.slice(last, m.index)}</span>)
      if (m[1]) parts.push(<span key={idx++} style={{color:'var(--json-key)'}}>{m[1]}</span>, <span key={idx++}>:</span>)
      else if (m[2]) parts.push(<span key={idx++} style={{color:'var(--json-str)'}}>{m[2]}</span>)
      else if (m[3]) parts.push(<span key={idx++} style={{color:'var(--json-bool)'}}>{m[3]}</span>)
      else if (m[4]) parts.push(<span key={idx++} style={{color:'var(--json-num)'}}>{m[4]}</span>)
      last = m.index + m[0].length
    }
    if (last > 0) {
      if (last < jsonText.length) parts.push(<span key={idx}>{jsonText.slice(last)}</span>)
      return <>{parts}</>
    }
  }
  // Diff highlighting — only if text contains diff markers
  if (isDiffText(text)) {
    const lines = parseDiffLines(text)
    return (
      <>
        {lines.map((line, i) => {
          if (line.type === 'meta') return null
          return <div key={i} className={`${DIFF_BG[line.type]} ${DIFF_FG[line.type]}`}>{line.type === 'hunk' ? line.content : (line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ') + line.content}</div>
        })}
      </>
    )
  }
  // Default: plain text — inherit parent's color so the rendering stays
  // visually consistent with the JSON-highlighted path (whose non-token
  // spans also inherit). Do not wrap in `text-muted`: that dims input/output
  // panels for tools that don't trip the JSON regex (edge whitespace, partial
  // streams, non-JSON shell output).
  return <span>{text}</span>
}
