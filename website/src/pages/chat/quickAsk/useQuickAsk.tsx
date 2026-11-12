import { useId, useState, type CSSProperties, type ReactNode } from 'react'
import { ShieldCheck } from 'lucide-react'

import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { browserLiveStore } from '../../../hooks/useBrowserLive'
import { askUi, useAskSurface } from './askUi'
import { answerErrorText, isSignInTakeover, originHost, QuickAskTargetRing, takesOver, zoomStyle } from './QuickAskParts'
import QuickAskInline from './QuickAskInline'
import QuickAskSheet from './QuickAskSheet'
import type { QuickAsk } from './types'

export interface QuickAskSlots {
  /** Drawn over the live frame: the target ring, or the desktop sheet. */
  frameOverlay: ReactNode
  /** Drawn under the frame: the folded hint, the phone's bottom sheet, or the takeover bar. */
  askSlot: ReactNode
  /** The frame's zoom onto a taken-over widget, else null. */
  zoom: CSSProperties | null
  /** A human check or a sign-in is taken over: the frame takes the person's input. */
  scoped: boolean
}

const NONE: QuickAskSlots = { frameOverlay: null, askSlot: null, zoom: null, scoped: false }

/**
 * What one live surface (the chat card, priority 1; the big view, priority 2
 * while showing) draws for the session's pending ask. See `askUi` for why
 * only one surface holds the open sheet.
 */
export function useQuickAsk(sessionKey: string, ask: QuickAsk | null, priority: number): QuickAskSlots {
  const instanceId = useId()
  const isMobile = useIsMobile()
  const view = useAskSurface(instanceId, ask?.id ?? null, priority)
  if (!ask || view === 'none') return NONE
  if (takesOver(ask) && ask.phase === 'taken_over') {
    return { frameOverlay: null, askSlot: <QuickAskTakeoverBar key={ask.id} ask={ask} sessionKey={sessionKey} />, zoom: zoomStyle(ask), scoped: true }
  }
  const ring = <QuickAskTargetRing ask={ask} />
  if (view === 'hint') {
    return { frameOverlay: ring, askSlot: <QuickAskInline ask={ask} onOpen={() => askUi.open(instanceId, ask.id)} />, zoom: null, scoped: false }
  }
  const collapse = () => askUi.collapse(ask.id)
  const sheet = <QuickAskSheet key={ask.id} ask={ask} sessionKey={sessionKey} mode={isMobile ? 'bottom' : 'overlay'} onCollapse={collapse} />
  return isMobile
    ? { frameOverlay: ring, askSlot: sheet, zoom: null, scoped: false }
    : { frameOverlay: sheet, askSlot: null, zoom: null, scoped: false }
}

/** While the person holds just the widget: what to do, Done, Decline. */
function QuickAskTakeoverBar({ ask, sessionKey }: { ask: QuickAsk; sessionKey: string }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const signIn = isSignInTakeover(ask)
  const send = async (action: 'done' | 'decline') => {
    setBusy(true)
    setError('')
    const r = await browserLiveStore.answerAsk(sessionKey, ask.id, action)
    setBusy(false)
    if (r.ok) askUi.markAnswered(ask.id)
    else setError(answerErrorText(r.error ?? ''))
  }
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-accent/50 bg-accent-subtle/60 px-3 py-2" role="group" aria-label={signIn ? i18nT('pages.chat.quickAsk.title_credentials', { host: originHost(ask.origin) }) : i18nT('pages.chat.quickAsk.title_human_check')} data-testid="quick-ask-takeover">
      <ShieldCheck size={14} aria-hidden="true" className="shrink-0 text-accent" />
      <span className="min-w-0 flex-1 text-[13px] text-text-strong">{signIn ? i18nT('pages.chat.quickAsk.sign_in_takeover_hint') : i18nT('pages.chat.quickAsk.takeover_hint')}</span>
      <Btn type="button" onClick={() => { void send('decline') }} disabled={busy} data-testid="quick-ask-decline">{i18nT('pages.chat.quickAsk.decline')}</Btn>
      <Btn primary type="button" onClick={() => { void send('done') }} disabled={busy} data-testid="quick-ask-done">{i18nT('pages.chat.quickAsk.done')}</Btn>
      <ErrorNotice variant="inline" message={error} className="basis-full" askAgent testId="quick-ask-error" />
    </div>
  )
}
