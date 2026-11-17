/**
 * A manifest's `permissions.chatLaunch`, as the trust prompt and the detail page show it.
 *
 * The person's chats an app's page may open: a key prefix, the agents, an optional
 * folder and the memory modes. The gateway parses and enforces it
 * (`apps/chat_launch.py`, `dashboard/app_chat_launch.py`); this reads it only to
 * say what it asks for, and a value it cannot read is shown as nothing, the way the
 * gateway grants nothing for it.
 */
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** The memory modes a declaration may name, in the order the gateway keeps them. */
const MEMORY_MODES = ['persistent', 'incognito', 'temporary'] as const
type ChatLaunchMemoryMode = typeof MEMORY_MODES[number]

export interface ChatLaunchDeclaration {
  keyPrefix: string
  agents: string[]
  folder?: string
  memoryModes: ChatLaunchMemoryMode[]
}

function isMemoryMode(value: unknown): value is ChatLaunchMemoryMode {
  return typeof value === 'string' && (MEMORY_MODES as readonly string[]).includes(value)
}

/** The declared memory modes in the gateway's order, `['persistent']` when omitted,
 *  or undefined when the value names none or an unknown one (the gateway then
 *  declares nothing at all). */
function knownMemoryModes(value: unknown): ChatLaunchMemoryMode[] | undefined {
  if (value === undefined || value === null) return ['persistent']
  if (!Array.isArray(value) || value.length === 0 || !value.every(isMemoryMode)) return undefined
  return MEMORY_MODES.filter(mode => value.includes(mode))
}

/** The declaration in a raw `permissions.chatLaunch`, or undefined when it is not one. */
export function knownChatLaunch(value: unknown): ChatLaunchDeclaration | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const raw = value as Record<string, unknown>
  const agents = raw.agents
  if (typeof raw.keyPrefix !== 'string' || !raw.keyPrefix) return undefined
  if (!Array.isArray(agents) || agents.length === 0 || !agents.every(a => typeof a === 'string' && a)) return undefined
  if (raw.folder !== undefined && typeof raw.folder !== 'string') return undefined
  const memoryModes = knownMemoryModes(raw.memoryModes)
  if (!memoryModes) return undefined
  const folder = typeof raw.folder === 'string' ? raw.folder.trim() : ''
  return { keyPrefix: raw.keyPrefix, agents: agents as string[], ...(folder ? { folder } : {}), memoryModes }
}

/** The sentence naming what the app may open, in the trust prompt's or the detail page's words. */
export function chatLaunchLine(launch: ChatLaunchDeclaration, where: 'trust' | 'detail'): string {
  const agents = launch.agents.join(', ')
  const folder = launch.folder
  if (where === 'trust') {
    return folder
      ? i18nT('components.appstore.trustAppModal.chat_launch_desc', { agents, folder })
      : i18nT('components.appstore.trustAppModal.chat_launch_desc_unfiled', { agents })
  }
  return folder
    ? i18nT('pages.appDetailPage.chat_launch_desc', { agents, folder })
    : i18nT('pages.appDetailPage.chat_launch_desc_unfiled', { agents })
}

/** One memory mode in the words the trust prompt and the detail page use for it. */
function memoryModeLabel(mode: ChatLaunchMemoryMode): string {
  switch (mode) {
    case 'persistent': return i18nT('components.appstore.trustAppModal.chat_launch_memory_persistent')
    case 'incognito': return i18nT('components.appstore.trustAppModal.chat_launch_memory_incognito')
    case 'temporary': return i18nT('components.appstore.trustAppModal.chat_launch_memory_temporary')
  }
}

/** The rest of the declaration, on both surfaces: the key prefix its chats are under
 *  and the memory modes they may take. */
export function chatLaunchScopeLine(launch: ChatLaunchDeclaration): string {
  return i18nT('components.appstore.trustAppModal.chat_launch_scope', {
    keyPrefix: launch.keyPrefix,
    memoryModes: fmtList(launch.memoryModes.map(memoryModeLabel), { type: 'disjunction' }),
  })
}
