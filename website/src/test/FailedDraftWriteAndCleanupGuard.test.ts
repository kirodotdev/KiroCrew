// GPT's blocker at eb09eb2b0: a failed draft write reported as durable. Plus the last
// counted delete route that deleted drafts with no unsent-work guard.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetForTests, loadSideDrafts, writeSideDraft } from '../utils/sideComposerDrafts'

const SIDEBAR = readFileSync(join(__dirname, '..', 'pages', 'ChatSidebar.tsx'), 'utf-8')
const EN = JSON.parse(readFileSync(join(__dirname, '..', 'i18n', 'locales', 'en.manual.json'), 'utf-8'))

describe('a side-draft write reports whether storage actually holds the text', () => {
  beforeEach(() => __resetForTests())
  afterEach(() => vi.restoreAllMocks())

  it('reports true when the write lands, and the draft is findable', () => {
    expect(writeSideDraft('composer-a', 'slot-1', 'typed')).toBe(true)
    expect(loadSideDrafts()['slot-1']).toEqual(['composer-a'])
  })

  it('reports FALSE when storage refuses the write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    expect(writeSideDraft('composer-b', 'slot-1', 'typed')).toBe(false)
  })

  it('reports true for empty text, where storage agrees with the composer', () => {
    expect(writeSideDraft('composer-c', 'slot-1', '   ')).toBe(true)
    expect(loadSideDrafts()['slot-1']).toBeUndefined()
  })

  it('is the value the persistence hook tiers the claim on', () => {
    const hook = readFileSync(join(__dirname, '..', 'hooks', 'useSlotDraftPersistence.ts'), 'utf-8')
    expect(hook).toContain('writeSideDraft(id, slot, text) ? { slot, text } : null')
    // The unconditional form is what let a failed write earn the short TTL.
    expect(hook).not.toContain('setPersisted(true)')
  })
})

describe('the bulk session cleanup asks before discarding unsent work', () => {
  it('resolves unsent work for the previewed sessions before firing the request', () => {
    expect(SIDEBAR).toContain('archivable.filter(s => slotHasUnsentWork(s.key))')
    expect(SIDEBAR).toContain("i18nT('pages.chatSidebar.cleanup_unsent_confirm'")
  })

  it('gates the mutation on that answer, not on its result', () => {
    const guardAt = SIDEBAR.indexOf('slotHasUnsentWork(s.key)')
    const fireAt = SIDEBAR.indexOf('cleanupMutation.mutate(')
    expect(guardAt).toBeGreaterThan(-1)
    // Ordering is the whole point: in onSuccess the server has already archived.
    expect(guardAt).toBeLessThan(fireAt)
  })

  it('names the sessions at risk, so the user is not asked to guess', () => {
    const copy = EN.pages.chatSidebar.cleanup_unsent_confirm
    expect(copy).toContain('{{base}}')
    expect(copy).toContain('{{names}}')
  })
})
