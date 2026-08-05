import { openActivityToTab } from '../../store/chatSlice'
import { api } from '../../api/client'
import type { AppDispatch } from '../../store'

export type SlashInterceptResult = { intercepted: true } | { intercepted: false }

const SIDE_RE = /^\/side(?:\s+([\s\S]+))?$/

export async function interceptSlashCommand(
  raw: string,
  slot: string | null,
  dispatch: AppDispatch,
): Promise<SlashInterceptResult> {
  const trimmed = raw.trim()
  // Client-only command: replay the import gate, then the feature tour.
  // The App shell reads continueOnboarding while AgentImportFlow handles the
  // same event, so Settings can replay only the importer with a plain Event.
  if (trimmed === '/onboarding') {
    window.dispatchEvent(
      new CustomEvent('mc-start-import', { detail: { continueOnboarding: true } }),
    )
    return { intercepted: true }
  }
  const match = trimmed.match(SIDE_RE)
  if (!match) {
    return { intercepted: false }
  }
  if (!slot) {
    // Intentional diagnostic: the command was recognized but can't run
    // without an active slot, which is otherwise silent to the user.
    // eslint-disable-next-line no-console
    console.warn('[/side] no active slot — intercepted but not dispatched')
    return { intercepted: true }
  }
  const message = match[1]?.trim() ?? ''
  try {
    await api.sideOpen(slot)
  } catch (e: unknown) {
    // Intentional diagnostic breadcrumb for a silent side-open failure.
    // eslint-disable-next-line no-console
    console.warn('[/side] sideOpen failed:', e)
    return { intercepted: true }
  }
  dispatch(openActivityToTab('side'))
  if (message) {
    await api.sideTurn(slot, message).catch((e: unknown) => {
      // Intentional diagnostic breadcrumb for a silent side-turn failure.
      // eslint-disable-next-line no-console
      console.warn('[/side] sideTurn failed:', e)
    })
  }
  return { intercepted: true }
}
