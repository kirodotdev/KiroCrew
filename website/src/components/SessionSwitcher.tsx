import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useAppSelector } from '../store'
import type { ChatFolder, ChatSlot } from '../types'
import { orderFoldersWithPaths } from '../utils/folderTree'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { i18nT } from '../i18n/t'
import { OverlayPortal } from './overlayRoot'
import { statusOf, statusDotClass } from '../pages/chat/sessionStatus'
import {
  commitSessionSwitcher,
  MAX_SWITCHER_SESSIONS,
  setSessionSwitcherIndex,
  useSessionSwitcher,
} from '../lib/sessionSwitcher'

/** A quick tap switches instantly with no overlay; holding past this paints the
 *  grid. The gesture STATE attaches on keydown regardless, so correctness never
 *  depends on the paint landing. */
export const SWITCHER_SHOW_DELAY_MS = 120

export function projectFolderName(project: string | undefined): string {
  const normalized = project?.replace(/[\\/]+$/, '')
  return normalized?.split(/[\\/]/).pop() || ''
}

export default function SessionSwitcher() {
  const switcher = useSessionSwitcher()
  const slots = useAppSelector(state => state.dashboard.slots)
  const unreadSlots = useAppSelector(state => state.dashboard.unreadSlots)
  const [visible, setVisible] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const { data: chatFolders = [] } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
    enabled: switcher !== null,
  })
  const folderPaths = useMemo(
    () => new Map(orderFoldersWithPaths(chatFolders).map(({ folder, path }) => [folder.id, path])),
    [chatFolders],
  )

  const open = switcher !== null
  useEffect(() => {
    if (!open) {
      setVisible(false)
      return
    }
    const timer = window.setTimeout(() => setVisible(true), SWITCHER_SHOW_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [open])

  const selectedIndex = switcher?.index ?? -1
  useEffect(() => {
    if (!visible) return
    listRef.current
      ?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ block: 'nearest' })
  }, [visible, selectedIndex])

  if (!switcher || !visible) return null

  const slotsByKey = new Map(slots.map(slot => [slot.key, slot]))
  const unread = new Set(unreadSlots)
  const titleOf = (key: string) => {
    const title = slotsByKey.get(key)?.title
    return title && title !== key ? title : key
  }

  return (
    <OverlayPortal>
    <div className="pointer-events-none fixed inset-0 z-[95] flex items-center justify-center">
      <div
        role="listbox"
        aria-label={i18nT('hooks.useKeyboardShortcuts.switcher_dialog_label')}
        aria-activedescendant={`session-switcher-${selectedIndex}`}
        tabIndex={-1}
        className="pointer-events-auto flex w-[min(1080px,calc(100vw-32px))] flex-col overflow-hidden rounded-xl border border-border bg-bg-elevated text-text shadow-lg"
      >
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-[13px] font-semibold text-text-strong">
            {i18nT('pages.chat.sessionFlyout.recent_sessions')}
          </span>
          <span className="text-[11px] text-muted">
            {i18nT('hooks.useKeyboardShortcuts.switcher_recent_count', { count: MAX_SWITCHER_SESSIONS })}
          </span>
        </div>
        <div
          ref={listRef}
          className="grid max-h-[70vh] grid-cols-2 gap-2 overflow-y-auto p-2 sm:grid-cols-3 lg:grid-cols-5"
        >
          {switcher.keys.map((key, index) => {
            const slot: ChatSlot | undefined = slotsByKey.get(key)
            const selected = index === selectedIndex
            const status = slot ? statusOf(slot) : null
            const isUnread = !status && unread.has(key)
            const title = titleOf(key)
            const chatFolder = slot?.folder_id ? folderPaths.get(slot.folder_id) : ''
            const context = chatFolder || projectFolderName(slot?.project)
              || i18nT('pages.chatSidebar.unfiled')
            const agent = agentOrDefaultLabel(slot?.agent, '')

            return (
              <button
                type="button"
                key={key}
                id={`session-switcher-${index}`}
                role="option"
                tabIndex={-1}
                aria-selected={selected}
                aria-current={index === 0 ? 'true' : undefined}
                data-slot-key={key}
                onMouseMove={() => setSessionSwitcherIndex(index)}
                onMouseDown={event => {
                  if (event.button !== 0) return
                  event.preventDefault()
                  commitSessionSwitcher(index)
                }}
                className={`flex min-h-[120px] cursor-pointer flex-col rounded-lg border p-3 text-left transition-colors ${
                  selected
                    ? 'border-accent bg-accent-subtle text-text-strong shadow-[0_0_0_3px_var(--accent-glow)]'
                    : 'border-border-strong bg-bg-elevated text-text hover:bg-bg-hover'
                }`}
              >
                <span className="flex items-center justify-between gap-2">
                  <span
                    aria-hidden="true"
                    className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(status, isUnread, 'bg-border-strong')}`}
                  />
                  <span className="text-[10px] text-muted">
                    {index === 0
                      ? i18nT('components.commandPalette.providers.recentsProvider.current')
                      : index + 1}
                  </span>
                </span>
                <span className={`mt-2 block min-h-10 overflow-hidden text-[13px] leading-5 ${selected ? 'font-semibold' : ''}`}>
                  {title}
                </span>
                <span className="mt-auto block w-full truncate text-[11px] text-muted" title={context}>
                  {context}
                </span>
                <span className="block w-full truncate text-[11px] text-muted">
                  {i18nT('pages.sessionsTab.agent')} · {agent}
                </span>
              </button>
            )
          })}
        </div>
        <span className="sr-only" role="status" aria-live="polite">
          {titleOf(switcher.keys[selectedIndex] ?? '')}
        </span>
        <div className="shrink-0 border-t border-border px-3 py-1.5 text-right text-[11px] text-muted">
          {i18nT('hooks.useKeyboardShortcuts.switcher_hint')}
        </div>
      </div>
    </div>
    </OverlayPortal>
  )
}
