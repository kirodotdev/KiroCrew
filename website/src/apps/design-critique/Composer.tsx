import type { RefObject } from 'react'
import { Upload, Plus, X, ChevronLeft, ChevronRight, PencilRuler } from 'lucide-react'
import { KIND_LABEL, kindLabel } from './constants'
import { detectKind, recognise } from './utils'
import { S } from './styles'
import type { Blocked, StagedItem } from './types'

import { i18nT } from '../../i18n/t'
interface Props {
  staged: StagedItem[]
  refText: string
  dragging: boolean
  blocked: Blocked | null
  showAuth: boolean
  busy: boolean
  err: string
  inputRef: RefObject<HTMLInputElement>
  onPick: (e: React.ChangeEvent<HTMLInputElement>) => void
  onDrop: (e: React.DragEvent) => void
  onDragOver: (e: React.DragEvent) => void
  onDragLeave: (e: React.DragEvent) => void
  pickFile: () => void
  dropStaged: (i: number) => void
  moveStaged: (i: number, dir: number) => void
  clearStaged: () => void
  start: () => void
  setRefText: (v: string) => void
  setBlocked: (b: Blocked | null) => void
  setShowAuth: (fn: (v: boolean) => boolean) => void
  onTryAgain: () => void
}

export default function Composer(p: Props) {
  const { staged, refText, dragging, blocked, showAuth, busy, err, inputRef } = p

  const canStart = !busy && (staged.length > 0 || !!refText.trim())
  const det = detectKind(refText)
  const startLabel = staged.length > 1
    ? 'Critique this flow · ' + staged.length + ' screens'
    : staged.length === 1 ? 'Critique this screen'
    : refText.trim() ? 'Critique ' + (KIND_LABEL[(det || {}).kind as string] || 'this') : 'Critique'

  // What did they paste? Worked out live so we can say it back before they commit.
  const recog = (!staged.length && refText.trim()) ? recognise(det) : null

  const subLine = staged.length
    ? (staged.length > 1
        ? staged.length + ' screens · this order is the flow order — reorder or remove before you start.'
        : '1 screen · add another to critique it as a flow.')
    : ''

  const middle = staged.length
    ? (
      <div style={S.stagedRegion}>
        {staged.map((s, i) => (
          <div key={s.id} style={S.stagedItem}>
            <div style={S.stagedThumb}><img src={s.url} style={S.stagedImg} alt={s.file.name} /></div>
            <span style={S.stepChip}>{staged.length > 1 ? 'Step ' + (i + 1) : '1'}</span>
            <button style={S.killBtn} onClick={() => p.dropStaged(i)} title={i18nT('apps.designCritique.composer.remove')} aria-label={'Remove ' + s.file.name}><X size={13} /></button>
            <div style={S.stagedFoot}>
              {staged.length > 1 ? <button style={{ ...S.iconBtn, opacity: i === 0 ? 0.35 : 1 }} disabled={i === 0} onClick={() => p.moveStaged(i, -1)} title={i18nT('apps.designCritique.composer.move_earlier')} aria-label={i18nT('apps.designCritique.composer.move_earlier')}><ChevronLeft size={13} /></button> : null}
              {staged.length > 1 ? <button style={{ ...S.iconBtn, opacity: i === staged.length - 1 ? 0.35 : 1 }} disabled={i === staged.length - 1} onClick={() => p.moveStaged(i, 1)} title={i18nT('apps.designCritique.composer.move_later')} aria-label={i18nT('apps.designCritique.composer.move_later')}><ChevronRight size={13} /></button> : null}
              <span style={S.stagedName} title={s.file.name}>{s.file.name}</span>
            </div>
          </div>
        ))}
        <button style={S.addTile} onClick={p.pickFile}><Plus size={18} />{i18nT('apps.designCritique.composer.add_screens')}</button>
      </div>
    )
    : (
      <div
        style={{ ...S.dropTile, ...(dragging ? { borderColor: 'var(--accent)', background: 'color-mix(in srgb, var(--accent) 7%, transparent)' } : {}) }}
        onClick={p.pickFile} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); p.pickFile() } }}
      >
        <Upload size={24} />
        <div style={S.dropTitle}>{i18nT('apps.designCritique.composer.drop_screenshots_or_click_to_browse')}</div>
        <div style={S.dropSub}>{i18nT('apps.designCritique.composer.one_screen_or_several_in_order_for_a_flow_png_jp')}</div>
      </div>
    )

  return (
    <div style={S.composerMid} onDragOver={p.onDragOver} onDragLeave={p.onDragLeave} onDrop={p.onDrop}>
      <div style={S.card}>
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          multiple
          style={{ display: 'none' }}
          onChange={p.onPick}
          tabIndex={-1}
          aria-hidden="true"
        />
        <h2 style={S.cardTitle}>{blocked ? 'I couldn’t get in' : 'What should I critique?'}</h2>
        {blocked ? (
          <div style={S.blockedBox}>
            <div style={S.blockedSay}>{blocked.say}</div>
            <div style={S.blockedHint}>{blocked.hint}</div>
            {blocked.detail ? <div style={S.blockedDetail}>{blocked.detail}</div> : null}
            <div style={{ display: 'flex', gap: '8px', marginTop: '10px', flexWrap: 'wrap' }}>
              {blocked.fix === 'local' ? <button style={S.linkBtn} onClick={() => { p.setBlocked(null); p.setRefText('/') }}>{i18nT('apps.designCritique.composer.use_a_local_folder_instead')}</button> : null}
              {(blocked.fix === 'shots' || blocked.fix === 'local') ? <button style={S.linkBtn} onClick={() => { p.setBlocked(null); setTimeout(() => inputRef.current && inputRef.current.click(), 0) }}><Upload size={13} />{i18nT('apps.designCritique.composer.send_screenshots')}</button> : null}
              {blocked.auth ? <button style={S.linkBtn} onClick={() => p.setShowAuth(v => !v)}>{showAuth ? 'Hide access steps' : 'Fix my access'}</button> : null}
              {(blocked.fix === 'retry' || blocked.auth) ? <button style={S.linkBtn} onClick={() => { p.setBlocked(null); p.setShowAuth(() => false); p.onTryAgain() }}>{i18nT('apps.designCritique.composer.try_again')}</button> : null}
              {blocked.fix === 'retype' ? <button style={S.linkBtn} onClick={() => p.setBlocked(null)}>{i18nT('apps.designCritique.composer.fix_the_link')}</button> : null}
            </div>
            {(blocked.auth && showAuth) ? (
              <div style={S.authBox}>
                <div style={S.blockedHint}>{blocked.auth.lead}</div>
                <pre style={S.authCmds}>{blocked.auth.cmds.join('\n')}</pre>
                <div style={S.blockedHint}>{blocked.auth.tail}</div>
              </div>
            ) : null}
          </div>
        ) : null}
        <p style={S.cardSub}>
          {subLine}
          {staged.length ? <button style={S.clearLink} onClick={p.clearStaged}>{i18nT('apps.designCritique.composer.clear_all')}</button> : null}
        </p>
        {err ? <div style={{ fontSize: '12.5px', color: 'var(--error, #e5484d)', textAlign: 'center' }}>{err}</div> : null}
        {middle}
        {/* Decorative separator between the drop zone and the link field. A lone
            connector word ("OR") cannot be translated in isolation
            (check-source-strings bare-morpheme), and both sides already say what
            they take, so the rule is carrying the meaning. */}
        <div style={S.orRow} role="separator" aria-hidden="true"><span style={S.orLine} /></div>
        <input
          style={{ ...S.linkInput, opacity: staged.length ? 0.45 : 1 }}
          value={refText} disabled={staged.length > 0 || busy}
          placeholder={staged.length ? 'Using your screenshots — clear them to critique a link instead' : 'Figma link · git repo · a folder on this machine · a running URL (localhost or a deployed preview)'}
          onChange={(e) => p.setRefText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') p.start() }}
        />
        <button
          style={{ ...S.bigStart, ...(canStart ? {} : S.startOff) }} disabled={!canStart} onClick={p.start}
          title={canStart ? 'Start the critique' : 'Add screenshots or paste a link first'}
        >
          <PencilRuler size={16} />{startLabel}
        </button>
        {recog ? (
          <p style={{ ...S.cardHint, color: recog.ok ? 'var(--muted)' : 'var(--error, #e5484d)' }}>
            <b style={{ color: recog.ok ? 'var(--text)' : 'inherit' }}>{recog.ok ? kindLabel((det || {}).kind as string) : i18nT('apps.designCritique.composer.unrecognised')}</b>
            {recog.ok ? ' · ' : ' — '}
            {recog.text}
          </p>
        ) : null}
        <p style={S.cardHint}>{recog
          ? null
          : 'I render everything before judging it — never from code alone. If your app is already running, paste its URL and I can measure real colours and sizes instead of estimating them.'}</p>
      </div>
    </div>
  )
}
