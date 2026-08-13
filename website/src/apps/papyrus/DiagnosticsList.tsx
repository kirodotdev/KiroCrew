/**
 * DiagnosticsList — parsed compiler errors and warnings, each clickable.
 *
 * The whole point of parsing the compiler log server-side is this list: clicking a
 * row moves the editor's cursor to the offending line, which is the difference
 * between "somewhere in a 900-line log" and "here". Rows with no line number are
 * still shown (the message is often self-explanatory) but are not interactive, so
 * a keyboard user is never handed a control that does nothing.
 *
 * Typesetting hints (over/underfull boxes) are collapsed behind a toggle: a paper
 * near its page limit produces dozens of them and they are never fatal.
 */
import { useMemo, useState } from 'react'
import { AlertTriangle, ChevronDown, ChevronRight, Ruler, XCircle } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { countDiagnostics } from './lib'
import type { Diagnostic } from './api'

import { i18nT } from '../../i18n/t'

export interface DiagnosticsListProps {
  diagnostics: Diagnostic[]
  /** Raw compiler log tail — shown when there are no parsed diagnostics at all. */
  log: string
  onJumpToLine: (line: number) => void
}

function LevelGlyph({ level }: { level: Diagnostic['level'] }) {
  if (level === 'error') return <XCircle className="lucide-inline shrink-0 text-danger" />
  if (level === 'warning') return <AlertTriangle className="lucide-inline shrink-0 text-warn" />
  return <Ruler className="lucide-inline shrink-0 text-muted" />
}

function DiagnosticRow({
  diagnostic,
  onJumpToLine,
}: {
  diagnostic: Diagnostic
  onJumpToLine: (line: number) => void
}) {
  const line = diagnostic.line
  const label = line !== null
    ? i18nT('apps.papyrus.diagnostics.jump_to_line', { line })
    : undefined

  const body = (
    <>
      <LevelGlyph level={diagnostic.level} />
      {line !== null && (
        <span className="shrink-0 font-mono text-[12px] text-muted">
          {i18nT('apps.papyrus.diagnostics.line_short', { line })}
        </span>
      )}
      <span className="min-w-0 flex-1 text-[13px] break-words">{diagnostic.message}</span>
      {diagnostic.file && (
        <span className="shrink-0 font-mono text-[12px] text-muted truncate max-w-[12rem]">
          {diagnostic.file}
        </span>
      )}
    </>
  )

  if (line === null) {
    return <div className="flex items-start gap-2 px-2 py-1 rounded">{body}</div>
  }
  return (
    <Clickable
      onClick={() => onJumpToLine(line)}
      aria-label={label}
      title={label}
      className="flex items-start gap-2 px-2 py-1 rounded cursor-pointer hover:bg-bg-hover focus-ring"
    >
      {body}
    </Clickable>
  )
}

export default function DiagnosticsList({ diagnostics, log, onJumpToLine }: DiagnosticsListProps) {
  const [showHints, setShowHints] = useState(false)
  const counts = useMemo(() => countDiagnostics(diagnostics), [diagnostics])

  const problems = useMemo(
    () => diagnostics.filter(d => d.level === 'error' || d.level === 'warning'),
    [diagnostics],
  )
  const hints = useMemo(() => diagnostics.filter(d => d.level === 'typesetting'), [diagnostics])

  if (diagnostics.length === 0) {
    return (
      <div className="p-2" data-testid="papyrus-diagnostics">
        {log ? (
          <pre className="max-h-40 overflow-auto rounded bg-bg-subtle p-2 font-mono text-[12px] text-muted whitespace-pre-wrap">
            {log}
          </pre>
        ) : (
          <div className="px-2 py-1 text-[13px] text-muted">
            {i18nT('apps.papyrus.diagnostics.no_messages')}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="p-1 space-y-0.5 overflow-y-auto" data-testid="papyrus-diagnostics">
      {problems.map((d, i) => (
        <DiagnosticRow key={`p${i}`} diagnostic={d} onJumpToLine={onJumpToLine} />
      ))}
      {hints.length > 0 && (
        <>
          <Clickable
            onClick={() => setShowHints(v => !v)}
            aria-expanded={showHints}
            className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer text-[12px] text-muted hover:bg-bg-hover focus-ring"
          >
            {showHints
              ? <ChevronDown className="lucide-inline shrink-0" />
              : <ChevronRight className="lucide-inline shrink-0" />}
            {i18nT('apps.papyrus.diagnostics.typesetting_hints', { count: counts.typesetting })}
          </Clickable>
          {showHints
            && hints.map((d, i) => (
              <DiagnosticRow key={`h${i}`} diagnostic={d} onJumpToLine={onJumpToLine} />
            ))}
        </>
      )}
    </div>
  )
}
