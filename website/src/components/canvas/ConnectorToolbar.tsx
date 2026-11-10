/**
 * The floating toolbar over a selected connector, after FigJam's: colour, line
 * type, dashed, arrowheads, delete.
 */

import { CornerDownRight, Ellipsis, Minus, MoveHorizontal, MoveRight, Spline, Trash2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { CONN_INK, type Connector, type ConnectorArrows, type ConnectorLine } from './canvasModel'
import SwatchPicker from './SwatchPicker'

interface Props {
  conn: Connector
  onChange: (patch: Partial<Connector>) => void
  onDelete: () => void
}

const sep = <span className="mx-1 h-5 w-px bg-border" />

function Toggle({ on, label, onClick, children }: { on: boolean; label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" aria-label={label} title={label} aria-pressed={on} onClick={onClick}
      className={`flex h-7 w-7 items-center justify-center rounded-lg transition-colors ${on ? 'bg-accent text-accent-fg' : 'text-text hover:bg-bg-hover'}`}>
      {children}
    </button>
  )
}

export default function ConnectorToolbar({ conn, onChange, onDelete }: Props) {
  const { t } = useTranslation()
  const line = conn.line ?? 'elbow'
  const arrows = conn.arrows ?? 'end'
  const lines: { line: ConnectorLine; label: string; icon: ReactNode }[] = [
    { line: 'elbow', label: t('components.canvas.line_elbow'), icon: <CornerDownRight size={15} /> },
    { line: 'straight', label: t('components.canvas.line_straight'), icon: <Minus size={15} /> },
    { line: 'curve', label: t('components.canvas.line_curve'), icon: <Spline size={15} /> },
  ]
  const arrowModes: { arrows: ConnectorArrows; label: string; icon: ReactNode }[] = [
    { arrows: 'end', label: t('components.canvas.arrows_end'), icon: <MoveRight size={15} /> },
    { arrows: 'both', label: t('components.canvas.arrows_both'), icon: <MoveHorizontal size={15} /> },
    { arrows: 'none', label: t('components.canvas.arrows_none'), icon: <Minus size={15} /> },
  ]
  return (
    <div className="flex items-center gap-1 rounded-xl border border-border bg-bg-elevated px-1.5 py-1 shadow-lg" data-testid="canvas-connector-toolbar">
      <SwatchPicker label={t('components.canvas.line_colour')} value={conn.color} fallback={CONN_INK} show="stroke"
        onPick={color => onChange({ color })} onReset={() => onChange({ color: undefined })} />
      {sep}
      {lines.map(l => <Toggle key={l.line} on={line === l.line} label={l.label} onClick={() => onChange({ line: l.line, route: undefined })}>{l.icon}</Toggle>)}
      {sep}
      <Toggle on={!!conn.dashed} label={t('components.canvas.line_dashed')} onClick={() => onChange({ dashed: !conn.dashed || undefined })}><Ellipsis size={15} /></Toggle>
      {sep}
      {arrowModes.map(a => <Toggle key={a.arrows} on={arrows === a.arrows} label={a.label} onClick={() => onChange({ arrows: a.arrows })}>{a.icon}</Toggle>)}
      {sep}
      <button type="button" aria-label={t('components.canvas.delete_connector')} title={t('components.canvas.delete')} onClick={onDelete} className="rounded-lg p-1.5 text-muted hover:bg-bg-hover hover:text-text">
        <Trash2 size={14} />
      </button>
    </div>
  )
}
