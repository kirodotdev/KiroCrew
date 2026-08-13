import type { RefObject } from 'react'
import { Sparkle, Trash2, X, CornerDownLeft } from 'lucide-react'
import { Spinner } from './Motion'
import { S } from './styles'
import type { Ask, Sel } from './types'

import { i18nT } from '../../i18n/t'
interface Props {
  sel: Sel | null
  asks: Ask[]
  openAskId: string | null
  askDraft: string
  reduceMotion: boolean
  threadRef: RefObject<HTMLDivElement>
  setOpenAskId: (id: string | null) => void
  setSel: (s: Sel | null) => void
  setAskDraft: (v: string) => void
  askAbout: (quote: string, question: string) => void
  askFollowUp: (askId: string, question: string) => void
  /** A follow-up is in flight. Turns share one slot, so they must go one at a
   *  time — without this the input just silently dropped the second question. */
  pending: boolean
  removeAsk: (id: string) => void
}

export default function AskLayer(p: Props) {
  const { sel, asks, openAskId, askDraft, reduceMotion } = p

  // Floating "Ask about this" chip for the current selection.
  const askChip = sel ? (
    <button
      style={{ ...S.askChip, top: sel.top + 'px', left: sel.left + 'px' }}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => { p.setOpenAskId('new'); p.setAskDraft('') }}
    >
      <Sparkle size={13} />{i18nT('apps.designCritique.askLayer.ask_about_this')}
    </button>
  ) : null

  const activeAsk = openAskId && openAskId !== 'new' ? asks.find(a => a.id === openAskId) : null
  const composing = openAskId === 'new' && sel
  const anchorTop = composing ? sel!.top : 140
  const anchorLeft = composing ? sel!.left : 470

  const askPop = (activeAsk || composing) ? (
    <div
      style={{ ...S.askPop,
        top: Math.max(12, Math.min(anchorTop + 26, window.innerHeight - 300)) + 'px',
        left: Math.max(16, Math.min(anchorLeft - 180, window.innerWidth - 376)) + 'px' }}
    >
      <div style={S.askHead}>
        <div style={S.askQuote}>{'“' + (composing ? sel!.quote : activeAsk!.quote) + '”'}</div>
        {activeAsk ? (
          <button style={S.askIcon} title={i18nT('apps.designCritique.askLayer.remove_this_annotation')} aria-label={i18nT('apps.designCritique.askLayer.remove_this_annotation')} onClick={() => p.removeAsk(activeAsk.id)}>
            <Trash2 size={14} />
          </button>
        ) : null}
        <button style={S.askIcon} title={i18nT('apps.designCritique.askLayer.close')} aria-label={i18nT('apps.designCritique.askLayer.close')} onClick={() => { p.setOpenAskId(null); p.setSel(null) }}>
          <X size={15} />
        </button>
      </div>

      {activeAsk && activeAsk.turns.length ? (
        <div ref={p.threadRef} style={S.askThread}>
          {activeAsk.turns.map((t, i) => (
            <div key={i} style={{ marginBottom: i === activeAsk.turns.length - 1 ? 0 : '12px' }}>
              {t.q ? <div style={S.askQ}>{t.q}</div> : null}
              {t.pending
                ? <div style={{ ...S.askAnswer, color: 'var(--muted)', display: 'flex', gap: '7px', alignItems: 'center' }}><Spinner size={13} reduceMotion={reduceMotion} />{i18nT('apps.designCritique.askLayer.thinking')}</div>
                : <div style={{ ...S.askAnswer, marginTop: t.q ? '4px' : 0, color: t.failed ? 'var(--error, #e5484d)' : 'var(--text)' }}>{t.a}</div>}
            </div>
          ))}
        </div>
      ) : null}

      <div style={S.askRow}>
        <input
          style={{ ...S.askInput, ...(p.pending ? { opacity: 0.6 } : {}) }} value={askDraft} autoFocus
          disabled={p.pending}
          placeholder={activeAsk && activeAsk.turns.length ? 'Ask a follow-up…' : 'What don’t you understand about this?'}
          onChange={(e) => p.setAskDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !p.pending) {
              if (composing) p.askAbout(sel!.quote, askDraft)
              else p.askFollowUp(activeAsk!.id, askDraft)
            }
            if (e.key === 'Escape') { p.setOpenAskId(null); p.setSel(null) }
          }}
        />
        <button
          style={{ ...S.askIcon, ...S.askSend, ...(p.pending ? { opacity: 0.6, cursor: 'default' } : {}) }}
          title={i18nT('apps.designCritique.askLayer.ask')} aria-label={i18nT('apps.designCritique.askLayer.ask')}
          disabled={p.pending}
          onClick={() => {
            if (p.pending) return
            if (composing) p.askAbout(sel!.quote, askDraft)
            else p.askFollowUp(activeAsk!.id, askDraft)
          }}
        >
          <CornerDownLeft size={15} />
        </button>
      </div>
    </div>
  ) : null

  return <>{askChip}{askPop}</>
}
