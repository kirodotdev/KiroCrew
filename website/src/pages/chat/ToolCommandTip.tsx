import { useMemo, type CSSProperties } from 'react'
import { formatShellForTip, type ShellPieceKind } from '../../utils/shellTipFormat'
import { i18nT } from '../../i18n/t'

/** Past this many characters the bubble shows the head and a cut marker that
 *  points at the opened step, which carries the whole payload. A
 *  pointer-events-none bubble cannot scroll, so an unbounded one would paint a
 *  wall of text over the transcript. */
export const TIP_CHAR_CAP = 1200
/** Same bound in lines, for commands made of many short stages. */
export const TIP_LINE_CAP = 12

/** Classes the tool pill wears while this bubble is open over it. See the
 *  `pillTipOpen` note in ToolCallLine for the colour reasoning. */
export const PILL_TIP_OPEN_CLASS = 'bg-bg-hover outline-solid outline-1 -outline-offset-1 outline-muted'

const PIECE_STYLE: Record<ShellPieceKind, CSSProperties> = {
  program: { color: 'var(--text-strong)', fontWeight: 600 },
  flag: { color: 'var(--json-key)' },
  string: { color: 'var(--json-str)' },
  redirect: { color: 'var(--muted)' },
  text: { color: 'var(--text)' },
}

/** A pipe hands output to the next stage; `&&`, `||` and `;` only sequence
 *  commands. The gutter paints the two apart so a reader can tell a pipeline
 *  from a chain at a glance. */
const PIPE_OPS = new Set(['|', '|&'])

function capChars(text: string): { text: string; cut: boolean } {
  return text.length > TIP_CHAR_CAP ? { text: text.slice(0, TIP_CHAR_CAP), cut: true } : { text, cut: false }
}

/** Where the bubble had to stop: an ellipsis and a muted pointer to the
 *  opened step, which carries the whole payload. */
function CutMarker() {
  return (
    <span data-testid="tool-command-tip-cut" className="text-muted">
      … {i18nT('pages.chat.toolCallLine.tip_cut_hint')}
    </span>
  )
}

/**
 * What the tool pill's hover bubble shows in place of the native `title`.
 *
 * A shell command is laid out one pipeline stage per line, the joining
 * operator in a gutter (a pipe in the accent, `&&` / `||` / `;` muted) and
 * the program, flags, strings and redirections told apart by colour -- the
 * long one-liner the native tooltip wrapped mid-word becomes a readable
 * pipeline. Anything else (a prose tool title) is plain wrapped text. The
 * formatted grid is `aria-hidden` and the verbatim text rides along for screen
 * readers, so the bubble never announces a reflowed command as if it were the
 * real one.
 */
export function ToolCommandTip({ text, shell }: { text: string; shell: boolean }) {
  const lines = useMemo(() => {
    if (!shell) return null
    const { text: head, cut } = capChars(text)
    const formatted = formatShellForTip(head)
    if (formatted) return { formatted: formatted.slice(0, TIP_LINE_CAP), cut: cut || formatted.length > TIP_LINE_CAP, verbatim: null }
    // Multi-line (heredoc, script): verbatim, still bounded.
    const raw = head.split(/\r?\n/)
    return { formatted: null, verbatim: raw.slice(0, TIP_LINE_CAP).join('\n'), cut: cut || raw.length > TIP_LINE_CAP }
  }, [text, shell])

  if (!lines) {
    const { text: head, cut } = capChars(text)
    return (
      <div className="max-w-[min(26rem,calc(100vw-2rem))] whitespace-pre-wrap [overflow-wrap:anywhere] text-[12px] leading-[1.45] text-text">
        {head}
        {cut && <div className="mt-1"><CutMarker /></div>}
      </div>
    )
  }

  return (
    <div data-testid="tool-command-tip" className="max-w-[min(36rem,calc(100vw-2rem))] font-mono text-[11.5px] leading-[1.55]">
      <span className="sr-only">{text}</span>
      <div aria-hidden="true" className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2">
        {lines.formatted ? lines.formatted.map((line, i) => (
          <div key={i} className="contents">
            <span
              data-op-kind={i === 0 ? undefined : PIPE_OPS.has(line.op) ? 'pipe' : 'sequence'}
              className={`select-none text-right ${i > 0 && PIPE_OPS.has(line.op) ? 'text-accent' : 'text-muted'}`}
            >
              {i === 0 ? '$' : line.op}
            </span>
            <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">
              {line.pieces.map((p, j) => <span key={j} style={PIECE_STYLE[p.kind]}>{p.text}</span>)}
            </span>
          </div>
        )) : (
          <>
            <span className="select-none" style={{ color: 'var(--muted)' }}>$</span>
            <span className="whitespace-pre-wrap [overflow-wrap:anywhere] text-text">{lines.verbatim}</span>
          </>
        )}
        {lines.cut && (
          <>
            <span />
            <CutMarker />
          </>
        )}
      </div>
    </div>
  )
}
