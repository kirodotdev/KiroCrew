import { Check, X } from 'lucide-react'
import { STAGES, WRITING_STAGE, SCAN_STAGES, KIND_WAIT } from './constants'
import { Spinner, Sweep } from './Motion'
import { S } from './styles'
import type { Phase, Screen } from './types'

import { i18nT } from '../../i18n/t'
interface Props {
  phase: Phase
  elapsed: number
  writing: boolean
  reduceMotion: boolean
  screens: Screen[]
  pendingKind: string | null
  onCancel: () => void
}

export default function WaitingScreen({ phase, elapsed, writing, reduceMotion, screens, pendingKind, onCancel }: Props) {
  // Stage list: time-driven up front, real signal for the last one.
  let stageIdx = 0
  for (let i = 0; i < STAGES.length; i++) if (elapsed >= STAGES[i].at) stageIdx = i
  if (phase === 'uploading' || phase === 'scanning') stageIdx = 0
  const list = phase === 'scanning' ? SCAN_STAGES : [...STAGES, { at: Number.POSITIVE_INFINITY, label: WRITING_STAGE.label }]
  const activeIdx = writing ? list.length - 1 : stageIdx

  const many = screens.length > 1
  const shotSize = many ? { width: '150px', height: '100px' } : { width: '100%', maxWidth: '420px', height: '260px' }
  const shots = screens.length
    ? (
      <div style={S.waitRow}>
        {screens.map((sc, i) => (
          <div key={'w' + i} style={{ ...S.waitShot, ...shotSize }}>
            <img src={sc.url} style={S.waitShotImg} alt={sc.label || ''} />
            {phase === 'analyzing'
              ? <Sweep index={i} reduceMotion={reduceMotion} />
              : null}
          </div>
        ))}
      </div>
    )
    : (
      <div style={S.waitPlaceholder}>
        <Spinner size={18} reduceMotion={reduceMotion} />
        <span>{(pendingKind && KIND_WAIT[pendingKind]) || 'Getting the screens ready…'}</span>
      </div>
    )

  const title = phase === 'scanning' ? 'Looking for screens to audit'
    : phase === 'uploading'
    ? (many ? 'Uploading ' + screens.length + ' screens…' : 'Uploading your screenshot…')
    : many ? 'Reading your flow · ' + screens.length + ' screens'
    : screens.length ? 'Reading your screen'
    : 'Getting real pixels first'

  return (
    <div style={S.waitWrap}>
      <div style={S.waitCard}>
        <div style={S.waitHead}>
          <span style={S.waitTitle}>{title}</span>
        </div>
        {shots}
        <ul style={S.stageList}>
          {list.map((st, i) => {
            const done = i < activeIdx, now = i === activeIdx
            return (
              <li key={i} style={{ ...S.stageItem, color: now ? 'var(--text)' : 'var(--muted)', opacity: now || done ? 1 : 0.55, fontWeight: now ? 600 : 400 }}>
                <span style={S.stageMark}>
                  {done
                    ? <Check size={14} style={{ color: '#3fae6b' }} />
                    : <span style={now ? S.stageNow : S.stagePend} />}
                </span>
                {st.label}
              </li>
            )
          })}
        </ul>
        <button style={{ ...S.linkBtn, alignSelf: 'flex-start' }} onClick={onCancel} title={i18nT('apps.designCritique.waitingScreen.stop_this_run_and_discard_it')}>
          <X size={13} />{i18nT('apps.designCritique.waitingScreen.cancel_this_run')}
        </button>
        <div style={S.waitFoot}>
          {elapsed > 90
            ? 'Still going — a dense flow can take a couple of minutes. You can navigate away; the critique keeps running and will be here when you come back.'
            : 'You can navigate away — the critique keeps running.'}
        </div>
      </div>
    </div>
  )
}
