import { PencilRuler, GripVertical, ChevronLeft, ChevronRight } from 'lucide-react'
import { shortLabel } from './utils'
import { S } from './styles'
import type { Scope, Flow, DiscoveryScreen } from './types'

import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
interface Props {
  scope: Scope
  picked: string[]
  refBrief: string
  dragId: string | null
  togglePick: (id: string) => void
  dropPickAt: (id: string, overId: string) => void
  movePick: (id: string, dir: number) => void
  useFlow: (f: Flow) => void
  setDragId: (id: string | null) => void
  setRefBrief: (v: string) => void
  runScoped: () => void
  onStartOver: () => void
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])

// Discovery found candidate screens. Routes are knowable; flows are a guess —
// so flows are offered as suggestions the user confirms and can reorder.
export default function ScopingPicker(p: Props) {
  const ime = useImeGuard()
  const { scope, picked, refBrief, dragId } = p
  const groups = new Map<string, DiscoveryScreen[]>()
  scope.screens.forEach(s => {
    const g = s.group || 'screens'
    if (!groups.has(g)) groups.set(g, [])
    groups.get(g)!.push(s)
  })
  const seeable = scope.screens.filter(s => s.canSee !== false).length

  return (
    <div style={S.scopeWrap}>
      <div style={S.scopeCard}>
        <h2 style={S.cardTitle}>{i18nT('apps.designCritique.scopingPicker.what_should_i_audit')}</h2>
        <p style={S.cardSub}>
          {scope.framework ? <b>{scope.framework}</b> : null}
          {scope.framework ? ' · ' : ''}
          {scope.screens.length + ' screens found · ' + seeable + ' I can render'}
        </p>
        {scope.note ? <p style={S.scopeNote}>{scope.note}</p> : null}

        {scope.flows.length ? (
          <div>
            <div style={S.sectionH}>{i18nT('apps.designCritique.scopingPicker.suggested_flows')}</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {scope.flows.map((f, i) => {
                const on = sameSet(picked, (f.screenIds || []))
                return (
                  <button key={i} style={{ ...S.flowCard, ...(on ? S.flowCardOn : {}) }} onClick={() => p.useFlow(f)}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={S.flowName}>{f.label || 'Flow ' + (i + 1)}</div>
                      <div style={S.flowSteps}>{(f.screenIds || []).map((id, n) => {
                        const sc = scope.screens.find(s => s.id === id)
                        return (n ? ' → ' : '') + (sc ? shortLabel(sc.label) : id)
                      }).join('')}</div>
                      {f.why ? <div style={S.flowWhy}>{f.why}</div> : null}
                    </div>
                    <span style={{ ...S.guessTag, ...(f.basis === 'observed' ? { color: 'var(--accent)', borderColor: 'var(--accent)' } : {}) }}>
                      {f.basis === 'observed' ? 'from links' : 'guess'}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        ) : null}

        <div>
          <div style={S.sectionH}>{i18nT('apps.designCritique.scopingPicker.screens')}<span style={{ fontWeight: 600, textTransform: 'none' }}>{'· ' + picked.length + ' picked'}</span></div>
          <p style={{ ...S.pickWhy, margin: '0 0 7px 2px' }}>{picked.length > 1
            ? 'Numbers are the order I’ll walk them — drag a row to reorder, or use ‹ › from the keyboard.'
            : 'Click a screen to pick it. Pick several to critique them as a flow.'}</p>
          <div style={S.scopeScrollFlex}>
            {Array.from(groups.entries()).flatMap(([g, list]) => [
              <div key={'g' + g} style={S.groupH}>{g}</div>,
              ...list.map(s => {
                const off = s.canSee === false
                const ord = picked.indexOf(s.id)
                const isDragging = dragId === s.id
                const canDrag = ord >= 0 && picked.length > 1
                return (
                  // Role, tab stop, click and keydown are gated on the SAME `off`
                  // flag, so a pickable row is a complete checkbox widget
                  // (role + tabIndex 0 + Enter/Space) and the unrenderable one keeps
                  // only the drag cleanup handlers — onDragEnd/onDrop, which are
                  // pointer-only and do nothing but preventDefault and clear dragId.
                  // Those two are in the rule's handler set, which is part of why it
                  // fires; it also cannot credit the ternary role, so it reads the
                  // widget as a bare div.
                  // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- role="checkbox" + tabIndex 0 + Enter/Space ship together on the branch that has the click
                  <div
                    key={s.id}
                    style={{ ...S.pickRow, ...(off ? S.pickRowOff : {}), ...(isDragging ? S.pickRowDrag : {}) }}
                    role={off ? undefined : 'checkbox'}
                    aria-checked={off ? undefined : ord >= 0}
                    aria-disabled={off || undefined}
                    tabIndex={off ? -1 : 0}
                    onClick={off ? undefined : () => p.togglePick(s.id)}
                    onKeyDown={off ? undefined : (e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        p.togglePick(s.id)
                      }
                    }}
                    title={off ? (s.why || 'I can’t render this one') : (s.ref || s.id)}
                    draggable={canDrag}
                    onDragStart={canDrag ? (e) => { p.setDragId(s.id); e.dataTransfer.effectAllowed = 'move' } : undefined}
                    onDragEnd={() => p.setDragId(null)}
                    onDragOver={(ord >= 0 && dragId && dragId !== s.id) ? (e) => { e.preventDefault(); p.dropPickAt(dragId, s.id) } : undefined}
                    onDrop={(e) => { e.preventDefault(); p.setDragId(null) }}
                  >
                    {canDrag ? <span style={S.grip} title={i18nT('apps.designCritique.scopingPicker.drag_to_reorder')}><GripVertical size={14} /></span> : null}
                    {ord >= 0 ? <span style={S.pickOrd}>{String(ord + 1)}</span> : <span style={S.pickBox} />}
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{shortLabel(s.label)}</span>
                    {(ord >= 0 && picked.length > 1) ? (
                      <span style={{ display: 'inline-flex', gap: '4px' }}>
                        <button style={{ ...S.iconBtn, opacity: ord === 0 ? 0.3 : 1 }} disabled={ord === 0} onClick={(e) => { e.stopPropagation(); p.movePick(s.id, -1) }} title={i18nT('apps.designCritique.scopingPicker.move_earlier')} aria-label={i18nT('apps.designCritique.scopingPicker.move_earlier')}><ChevronLeft size={12} /></button>
                        <button style={{ ...S.iconBtn, opacity: ord === picked.length - 1 ? 0.3 : 1 }} disabled={ord === picked.length - 1} onClick={(e) => { e.stopPropagation(); p.movePick(s.id, 1) }} title={i18nT('apps.designCritique.scopingPicker.move_later')} aria-label={i18nT('apps.designCritique.scopingPicker.move_later')}><ChevronRight size={12} /></button>
                      </span>
                    ) : null}
                    <span style={S.pickWhy}>{off ? (s.why || 'can’t render') : (s.ref || '')}</span>
                  </div>
                )
              }),
            ])}
          </div>
        </div>

        <div style={S.scopeFoot}>
          <input
            style={S.linkInput} value={refBrief}
            aria-label={i18nT('apps.designCritique.scopingPicker.optional_brief_who_is_it_for_and_what_is_the_mai')}
            placeholder={i18nT('apps.designCritique.scopingPicker.optional_who_is_it_for_and_what_s_the_main_task')}
            onChange={(e) => p.setRefBrief(e.target.value)}
            {...ime.bindEnter({ onEnter: () => { if (picked.length) p.runScoped() } })}
          />
          <button style={{ ...S.bigStart, ...(picked.length ? {} : S.startOff) }} disabled={!picked.length} onClick={p.runScoped}>
            <PencilRuler size={16} />
            {picked.length > 1 ? i18nT('apps.designCritique.scopingPicker.critique_this_flow_count_screens', { count: picked.length })
              : picked.length === 1 ? i18nT('apps.designCritique.scopingPicker.critique_this_screen') : i18nT('apps.designCritique.scopingPicker.pick_at_least_one_screen')}
          </button>
          <button style={{ ...S.linkBtn, alignSelf: 'center' }} onClick={p.onStartOver}>{i18nT('apps.designCritique.scopingPicker.start_over')}</button>
        </div>
      </div>
    </div>
  )
}
