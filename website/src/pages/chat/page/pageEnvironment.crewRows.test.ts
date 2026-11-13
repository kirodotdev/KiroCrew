/**
 * Two lists, on purpose. `chatPageSlots` is the page's own list and keeps a Crew
 * Mode root's crew members: the active-slot rules read it, and a crew member
 * opened from its root's Crew panel is the active chat in the main pane, so it
 * must stay listed or the page would drop it and pick another session.
 * `personChatPageSlots` leaves crew members out: it is what the collapsed-sidebar
 * flyout lists (`SessionsSidebarChrome` hands it to `SessionFlyout`) and what the
 * unread count counts (`surfaceUnreadKeys`). Dropping `crewModeView` from
 * `personChatPageSlots` reddens the flyout and unread tests; filtering crew
 * members out of `chatPageSlots` reddens the main-pane test.
 */
import { describe, expect, it } from 'vitest'
import type { ChatSlot } from '../../../types'
import { chatPageSlots, personChatPageSlots, surfaceUnreadKeys } from './pageEnvironment'
import { crewModePreviewOn } from '../../../test/crewModePreview'

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 1, running: false, mode: '', ...extra,
} as ChatSlot)

const ROWS = [
  row('plain'),
  row('root', { crew_mode_root: true }),
  row('member', { crew_root: 'root' }),
  row('slack-1', { surface: 'slack' }),
]

describe('chatPageSlots -- the main pane keeps a crew member', () => {
  it('keeps every chat-surface row, the crew member included, so its active slot stays selected', () => {
    const page = chatPageSlots(ROWS)
    expect(page.map(s => s.key)).toEqual(['plain', 'root', 'member'])
    // The rule the page's active-slot fallback applies: the active key is listed.
    expect(page.some(s => s.key === 'member')).toBe(true)
  })
})

describe('personChatPageSlots -- the flyout input', () => {
  it('drops the crew member and keeps the root and the person\'s rows', () => {
    expect(personChatPageSlots(chatPageSlots(ROWS)).map(s => s.key)).toEqual(['plain', 'root'])
  })

  it('leaves a list with no crew untouched', () => {
    const plain = [row('a'), row('b')]
    expect(personChatPageSlots(plain)).toEqual(plain)
  })
})

describe('surfaceUnreadKeys -- the unread count', () => {
  it('counts unread only on the person\'s rows: a crew member\'s unread never counts', () => {
    const visible = personChatPageSlots(chatPageSlots(ROWS))
    expect(surfaceUnreadKeys(['member', 'plain', 'slack-1'], visible)).toEqual(['plain'])
    expect(surfaceUnreadKeys([], visible)).toEqual([])
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
