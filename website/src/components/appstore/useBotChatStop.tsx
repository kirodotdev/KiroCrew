/**
 * The notice for an uninstall or disable that landed while some Bot chats running
 * the app's tools could not be stopped, with its retry and restart actions. Shared
 * by the Library and the app detail page, so both read the same response fields
 * (`bot_chat_stop_failed` on a 200, a `bot_chat_stop_incomplete` 409) the same way.
 *
 * An uninstall from the detail page leaves the page, so it hands the notice to the
 * Library through the route state (`carryState`), which `initialFromRoute` reads
 * once and `useAppActions` then takes out of the history entry.
 */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { MemberRosterRow } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../ErrorNotice'
import BotChatStopActions from './BotChatStopActions'
import { botChatStopFailedNotice, botChatStopFailedSlugs, botChatStopIncompleteSlugs } from './backendRefused'

export interface BotStopState {
  action: 'uninstall' | 'disable'
  /** The app's name, which the API takes. */
  app: string
  /** The app's display name, which the notice shows. */
  display: string
  slugs: string[]
}

function parseJsonBody(body: string): unknown {
  try {
    return JSON.parse(body)
  } catch {
    return null
  }
}

/** The Bots a failed call names when it is a Bot-chat stop's own 409, else null. */
export function stopResultOf(e: unknown): string[] | null {
  return e instanceof ApiError && e.status === 409 ? botChatStopIncompleteSlugs(parseJsonBody(e.body)) : null
}

/** The route state that carries a stop notice to the Library, or undefined for none. */
export function carryState(stop: BotStopState): { botStop: BotStopState } | undefined {
  return stop.slugs.length ? { botStop: stop } : undefined
}

/** The notice a route state carries, when it is one `carryState` wrote. */
export function initialFromRoute(state: unknown): BotStopState | null {
  const stop = state && typeof state === 'object' ? (state as { botStop?: unknown }).botStop : undefined
  if (!stop || typeof stop !== 'object') return null
  const s = stop as Partial<BotStopState>
  if ((s.action !== 'uninstall' && s.action !== 'disable') || typeof s.app !== 'string' || typeof s.display !== 'string') return null
  const slugs = Array.isArray(s.slugs) ? s.slugs.filter((x): x is string => typeof x === 'string') : []
  return slugs.length ? { action: s.action, app: s.app, display: s.display, slugs } : null
}

/** Where an app stands now, as the page reads it: the notice's lifetime follows it. */
export type BotStopAppState = 'enabled' | 'disabled' | 'absent'

/** The state of an app row a page holds (``undefined`` or not installed: absent). */
export function botStopAppState(row: { installed?: boolean; enabled?: boolean } | null | undefined): BotStopAppState {
  if (!row || row.installed === false) return 'absent'
  return row.enabled ? 'enabled' : 'disabled'
}

export function useBotChatStop(
  onError: (message: string) => void,
  initial: BotStopState | null = null,
  appState?: (app: string) => BotStopAppState | undefined,
): {
  botStop: BotStopState | null
  show: (action: BotStopState['action'], app: string, display: string, slugs: string[]) => void
  clear: () => void
  notice: ReactNode
} {
  const qc = useQueryClient()
  const [botStop, setBotStop] = useState<BotStopState | null>(initial)
  const [retrying, setRetrying] = useState(false)
  const botName = (slug: string): string => {
    // A stop that raised names no Bot: the gateway sends this placeholder.
    if (slug === 'unknown') return i18nT('pages.appsPage.bot_chat_stop_unknown_bot')
    const row = (qc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY) ?? []).find(r => r.slug === slug)
    return row?.display_name || row?.name || slug
  }
  const show = (action: BotStopState['action'], app: string, display: string, slugs: string[]) =>
    setBotStop(slugs.length ? { action, app, display, slugs } : null)
  // The notice belongs to the disable or uninstall it reports: once the app turns
  // enabled again, or installed again, its retry would undo that (a retry repeats
  // the action), so the notice goes. Read as a change of state, so a list not yet
  // refetched after the action does not clear it.
  const lastState = useRef<{ app: string; state: BotStopAppState } | null>(null)
  const current = botStop && appState ? appState(botStop.app) : undefined
  useEffect(() => {
    const seen = lastState.current
    lastState.current = botStop && current !== undefined ? { app: botStop.app, state: current } : null
    if (!botStop || current === undefined || seen?.app !== botStop.app || seen.state === current) return
    const undone = botStop.action === 'disable' ? current === 'enabled' : seen.state === 'absent'
    if (undone) setBotStop(null)
  }, [botStop, current])
  // Stop the Bot chats again by asking for the same action again: a disable runs
  // the app's teardown again, and an uninstalled app's stop runs from the Bot
  // tracking tables, answering 409 with the Bots still running, 404 with none.
  const retry = async () => {
    if (!botStop) return
    setRetrying(true)
    try {
      let slugs: string[]
      try {
        const result = botStop.action === 'disable'
          ? await api.disableApp(botStop.app)
          : await api.uninstallApp(botStop.app)
        slugs = botChatStopFailedSlugs(result)
      } catch (e) {
        // Only the stop's own answers are a stop result: 404 (nothing of the app is
        // left running) and a 409 whose code says the Bot chats did not stop. Any
        // other refusal leaves the notice as it was and shows as an error.
        const stopped = e instanceof ApiError && e.status === 404
        const named = stopResultOf(e)
        if (!stopped && named === null) throw e
        slugs = named ?? []
      }
      show(botStop.action, botStop.app, botStop.display, slugs)
    } catch (e) {
      onError((e as Error)?.message || i18nT('pages.appsPage.failed_to_disable', { name: botStop.display }))
    } finally {
      setRetrying(false)
    }
  }
  const notice = botStop ? (
    <ErrorNotice
      message={botChatStopFailedNotice(botStop.action, botStop.display, botStop.slugs, botName)}
      onDismiss={() => setBotStop(null)}
      className="mb-4 animate-rise"
      impact="blocking"
      askAgent
      actions={
        <BotChatStopActions
          app={botStop.display}
          bots={botStop.slugs.map(botName).join(', ')}
          count={botStop.slugs.length}
          retrying={retrying}
          onRetry={() => { void retry() }}
        />
      }
    />
  ) : null
  return { botStop, show, clear: () => setBotStop(null), notice }
}
