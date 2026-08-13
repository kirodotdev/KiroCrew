import type { ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { sevOf } from './constants'
import Clickable from '../../components/Clickable'
import { readableOn } from './utils'
import { S } from './styles'
import type { Finding } from './types'

import { i18nT } from '../../i18n/t'
interface Props {
  finding: Finding
  index: number
  isOpen: boolean
  isActive: boolean
  flowScoped: boolean
  pinNum: number | undefined
  stepRange: string
  withMarks: (text: string) => ReactNode
  onEnter: () => void
  onLeave: () => void
  onClick: () => void
  rowRef: (el: HTMLDivElement | null) => void
}

// One finding row. `badge` is either a pin number or a step-range chip.
export default function FindingRow({
  finding: f, isOpen, isActive, flowScoped, pinNum, stepRange,
  withMarks, onEnter, onLeave, onClick, rowRef,
}: Props) {
  const s = sevOf(f.severity)
  const Icon = s.icon
  return (
    <div
      ref={rowRef}
      style={{
        ...S.finding,
        borderBottom: isActive ? '1px solid transparent' : '1px solid var(--border)',
        background: isActive ? 'var(--card)' : 'transparent',
        boxShadow: isActive ? ('inset 3px 0 0 ' + s.color) : 'none',
        borderRadius: isActive ? '8px' : '0',
      }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <Clickable style={S.fRow} onClick={onClick}>
        {flowScoped
          ? <span style={{ ...S.flowBadge, background: s.color, color: readableOn(s.color) }}>{stepRange}</span>
          : <span style={{ ...S.numBadge, background: s.color, color: readableOn(s.color), marginTop: '1px' }}>{String(pinNum || '·')}</span>}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
            <span style={S.fTitle}>{withMarks(f.title || 'Finding')}</span>
            <ChevronRight size={15} style={{ flexShrink: 0, marginTop: '2px', transform: isOpen ? 'rotate(90deg)' : 'none', transition: 'transform .15s', color: 'var(--muted)' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '7px', flexWrap: 'wrap' }}>
            <span style={{ ...S.sevPill, color: s.color, borderColor: s.color }}><Icon size={12} />{s.label}</span>
            {f.category ? <span style={S.fMeta}>{f.category}</span> : null}
            {flowScoped ? <span style={S.fMeta}>{'· steps ' + stepRange}</span> : null}
          </div>
        </div>
      </Clickable>
      {isOpen ? (
        <div style={S.fBody}>
          {f.location ? <div><span style={S.fLabel}>{i18nT('apps.designCritique.findingRow.where')} </span>{f.location}</div> : null}
          {f.evidence ? <div><div style={S.fLabel}>{i18nT('apps.designCritique.findingRow.what_i_saw')}</div>{withMarks(f.evidence)}</div> : null}
          {f.fix ? <div><div style={S.fLabel}>{/accessib/i.test(f.category || '') ? 'Fix' : 'Suggestion'}</div>{withMarks(f.fix)}</div> : null}
          {(Array.isArray(f.rules) && f.rules.length) ? (
            <div>
              <div style={S.fLabel}>{i18nT('apps.designCritique.findingRow.based_on')}</div>
              <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '4px' }}>
                {f.rules.map((r, ri) => <span key={ri} style={S.ruleBadge}>{r}</span>)}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
