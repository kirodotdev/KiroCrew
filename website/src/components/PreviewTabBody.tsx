import { useCallback, useEffect, useMemo, useRef } from 'react'
import { Bot, Globe, Undo2 } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { Btn } from './ui'
import { browserLiveStore, isBrowsingNow, useBrowserLiveNewest } from '../hooks/useBrowserLive'
import { browserStageStore, usePanelBrowserStage } from '../hooks/useBrowserLive.stage'
import WebPreviewPanel from './WebPreviewPanel'
import { lazyRetry } from './lazyRetry'

// A chunk that fails to load shows its notice in the agent view's place, with
// Try Again, and Local preview stays one click away.
// No hand-off: the local preview kept mounted beside it can hold an annotation
// in progress.
const BrowserLiveStage = lazyRetry(() => import('../pages/chat/BrowserLiveStage'), {
  layout: 'page',
  title: () => i18nT('pages.chat.browserLiveStage.stage_load_failed'),
  scope: 'browser-live-stage',
  askAgent: false,
})

/**
 * The side panel's Preview tab, with its two modes.
 *
 *   - Local preview: `WebPreviewPanel`, the developer's own dev server, with
 *     page annotation in the desktop app. Unchanged, and kept MOUNTED while the
 *     other mode shows (hidden, `active` false so its native view hides too), so
 *     an iframe's app state and an annotation in progress survive a look at the
 *     agent.
 *   - Agent browser: `BrowserLiveStage`, the agent's browser big, live or
 *     replaying. Shown while a panel stage of one of its sessions is open.
 *
 * Its sessions are `sessionKey` and `followKeys`: a Bot's page passes the Bot's
 * own sessions, so their browsing shows here although the Bot's thread draws no
 * card for it. Agent browser opens on the one browsing now, else the one that
 * browsed last.
 *
 * The switch between them is one segmented control, shown only once one of its
 * sessions has browsed. Local preview IS Bring back: it clears the stage, so the
 * chat card unfolds. The panel closing or the person moving to another tab
 * clears it too, so the picture never sits in a place nobody can see.
 */
export default function PreviewTabBody({ sessionKey, active = true, followKeys }: {
  sessionKey?: string | null
  active?: boolean
  /** More sessions whose agent browser this tab shows, besides `sessionKey`. */
  followKeys?: readonly string[]
}) {
  const slot = sessionKey ?? null
  const keys = useMemo(() => [...new Set([slot, ...(followKeys ?? [])])].filter((k): k is string => !!k), [slot, followKeys])
  const stage = usePanelBrowserStage(keys)
  const newest = useBrowserLiveNewest(keys)
  const agent = !!stage
  const showSwitch = agent || !!newest

  const shownKey = stage?.sessionKey ?? null
  const bringBack = useCallback(() => { if (shownKey) browserStageStore.close(shownKey) }, [shownKey])
  const showAgent = useCallback(() => {
    const target = newest?.sessionKey ?? slot
    if (!target) return
    const cur = browserLiveStore.current(target)
    const live = isBrowsingNow(cur)
    browserStageStore.open(target, { turnId: live ? null : cur?.turnId ?? null, mode: live ? 'live' : 'replay' })
  }, [newest, slot])

  // Shown, then not shown (another tab, the panel closed): the picture goes back
  // to the chat. Only a real true -> false: the body can mount inactive while the
  // host is still opening the panel for a fresh stage.
  const wasActive = useRef(active)
  useEffect(() => {
    if (wasActive.current && !active && shownKey) browserStageStore.close(shownKey)
    wasActive.current = active
  }, [active, shownKey])

  // One big view per tab: a stage opened for another of its sessions closes the
  // older one, so Local preview or leaving the tab cannot reveal it or leave its
  // card folded.
  useEffect(() => {
    if (!stage) return
    for (const k of keys) {
      if (k !== stage.sessionKey && browserStageStore.get(k)?.surface === 'panel') browserStageStore.close(k)
    }
  }, [stage, keys])

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
        {stage && (
          <div className="absolute inset-0">
            {/* Keyed by session: a Bot's page can move the big view to another of its sessions. */}
            <BrowserLiveStage key={stage.sessionKey} sessionKey={stage.sessionKey} stage={stage} active={active} />
          </div>
        )}
      </div>
    </div>
  )
}
