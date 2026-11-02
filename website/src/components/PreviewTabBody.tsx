import { lazy, Suspense, useCallback, useEffect, useRef } from 'react'
import { Bot, Globe, Undo2 } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { Btn } from './ui'
import { browserLiveStore, useBrowserLiveSessionTurns } from '../hooks/useBrowserLive'
import { browserStageStore, useBrowserStage } from '../hooks/useBrowserLive.stage'
import WebPreviewPanel from './WebPreviewPanel'

const BrowserLiveStage = lazy(() => import('../pages/chat/BrowserLiveStage'))

/**
 * The side panel's Preview tab, with its two modes.
 *
 *   - Local preview: `WebPreviewPanel`, the developer's own dev server, with
 *     page annotation in the desktop app. Unchanged, and kept MOUNTED while the
 *     other mode shows (hidden, `active` false so its native view hides too), so
 *     an iframe's app state and an annotation in progress survive a look at the
 *     agent.
 *   - Agent browser: `BrowserLiveStage`, the agent's browser big, live or
 *     replaying. Shown while the session's stage is on the panel.
 *
 * The switch between them is one segmented control, shown only once the session
 * has browsed. Local preview IS Bring back: it clears the stage, so the chat
 * card unfolds. The panel closing or the person moving to another tab clears it
 * too, so the picture never sits in a place nobody can see.
 */
export default function PreviewTabBody({ sessionKey, active = true }: { sessionKey?: string | null; active?: boolean }) {
  const slot = sessionKey ?? null
  const stage = useBrowserStage(slot)
  const turns = useBrowserLiveSessionTurns(slot)
  const agent = !!stage && stage.surface === 'panel'
  const showSwitch = agent || turns.length > 0

  const bringBack = useCallback(() => { if (slot) browserStageStore.close(slot) }, [slot])
  const showAgent = useCallback(() => {
    if (!slot) return
    const cur = browserLiveStore.current(slot)
    const live = !!cur && !cur.turnOver && cur.state !== 'done' && cur.state !== 'error'
    browserStageStore.open(slot, { turnId: live ? null : cur?.turnId ?? null, mode: live ? 'live' : 'replay' })
  }, [slot])

  // Shown, then not shown (another tab, the panel closed): the picture goes back
  // to the chat. Only a real true -> false: the body can mount inactive while the
  // host is still opening the panel for a fresh stage.
  const wasActive = useRef(active)
  useEffect(() => {
    if (wasActive.current && !active && agent && slot) browserStageStore.close(slot)
    wasActive.current = active
  }, [active, agent, slot])

  const segBtn = (on: boolean) => `inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] cursor-pointer border-0 transition-colors ${
    on ? 'bg-accent-subtle text-accent font-semibold' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
  }`

  return (
    <div className="flex flex-col h-full min-h-0" data-testid="preview-tab-body" data-mode={agent ? 'agent' : 'local'}>
      {showSwitch && (
        <div className="flex items-center gap-2 px-2 py-1.5 border-b border-border shrink-0">
        <div role="group" aria-label={i18nT('components.previewTabBody.preview_mode')} className="flex items-center gap-1">
          <button type="button" aria-pressed={agent} className={segBtn(agent)} onClick={agent ? undefined : showAgent} data-testid="preview-mode-agent">
            <Bot size={13} aria-hidden="true" />
            {i18nT('components.previewTabBody.agent_browser')}
          </button>
          <button type="button" aria-pressed={!agent} className={segBtn(!agent)} onClick={agent ? bringBack : undefined} data-testid="preview-mode-local">
            <Globe size={13} aria-hidden="true" />
            {i18nT('components.previewTabBody.local_preview')}
          </button>
        </div>
        {/* The same as Local preview, said as what it does to the chat card. */}
        {agent && (
          <Btn className="ml-auto !px-1.5 shrink-0" onClick={bringBack} aria-label={i18nT('pages.chat.browserLiveStage.bring_back')} title={i18nT('pages.chat.browserLiveStage.bring_back')} data-testid="browser-live-stage-bring-back">
            <Undo2 size={14} aria-hidden="true" />
          </Btn>
        )}
        </div>
      )}
      <div className="relative flex-1 min-h-0">
        <div className="absolute inset-0" hidden={agent}>
          <WebPreviewPanel sessionKey={sessionKey} active={active && !agent} />
        </div>
        {agent && stage && slot && (
          <div className="absolute inset-0">
            <Suspense fallback={null}>
              <BrowserLiveStage sessionKey={slot} stage={stage} active={active} />
            </Suspense>
          </div>
        )}
      </div>
    </div>
  )
}
