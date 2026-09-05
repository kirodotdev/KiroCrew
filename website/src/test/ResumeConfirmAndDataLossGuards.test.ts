// The three findings blocking at baae42375d: the resume confirm hedging over a resolved
// source, and two data-loss routes GPT graded blocking.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { unsentConfirmKey } from '../hooks/useSessionActions'

const CHAT_PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')
const EN = JSON.parse(readFileSync(join(__dirname, '..', 'i18n', 'locales', 'en.manual.json'), 'utf-8'))

describe('the resume confirm names where the unsent work actually is', () => {
  it('picks the composer rider for a draft on this very screen', () => {
    expect(unsentConfirmKey('here', 'resume')).toBe('hooks.useSessionActions.resume_unsent_confirm_here')
  })

  it('picks the other-window rider when the draft is in another window', () => {
    expect(unsentConfirmKey('other-window', 'resume')).toBe('hooks.useSessionActions.resume_unsent_confirm_window')
  })

  it('keeps the hedge only where the surface genuinely is not identifiable', () => {
    expect(unsentConfirmKey('elsewhere', 'resume')).toBe('hooks.useSessionActions.resume_unsent_confirm')
  })

  it('leaves the close route on its own strings', () => {
    expect(unsentConfirmKey('here')).toBe('hooks.useSessionActions.close_unsent_confirm_here')
    expect(unsentConfirmKey('here', 'close')).toBe('hooks.useSessionActions.close_unsent_confirm_here')
  })

  it('resolves the source at the resume site instead of testing the boolean', () => {
    expect(CHAT_PAGE).toContain('slotUnsentWorkSource(activeSlot)')
    expect(CHAT_PAGE).toContain("unsentConfirmKey(resumeUnsentAt, 'resume')")
    // The catch-all must no longer be hardcoded on this route.
    expect(CHAT_PAGE).not.toContain("i18nT('hooks.useSessionActions.close_unsent_confirm', { base })")
  })

  it('says THE CURRENT TAB, since the base string already spends "this session"', () => {
    const riders = EN.hooks.useSessionActions
    expect(riders.resume_unsent_confirm_here).toContain('The current tab')
    expect(riders.resume_unsent_confirm_here).not.toContain('This session')
    for (const key of ['resume_unsent_confirm_here', 'resume_unsent_confirm_window', 'resume_unsent_confirm']) {
      expect(riders[key]).toContain('{{base}}')
    }
  })
})

describe('an embedded composer claims unrecoverable until its draft is durable', () => {
  const embed = readFileSync(join(__dirname, '..', 'app-sdk', 'ChatEmbed.tsx'), 'utf-8')
  const side = readFileSync(join(__dirname, '..', 'pages', 'chat', 'SideChat.tsx'), 'utf-8')
  const persistence = readFileSync(join(__dirname, '..', 'hooks', 'useSlotDraftPersistence.ts'), 'utf-8')

  it('reports whether the current text has been written', () => {
    expect(persistence).toContain('): boolean {')
    expect(persistence).toContain('setPersisted(writeSideDraft(id, slot, text))')
    expect(persistence).toContain('setPersisted(false)')
  })

  it('passes that answer to the registration in both hosts', () => {
    expect(embed).toContain('const draftPersisted = useSlotDraftPersistence(')
    expect(embed).toContain('draft.trim().length > 0, draftPersisted)')
    expect(side).toContain('const draftPersisted = useSlotDraftPersistence(')
    expect(side).toContain('draft.trim().length > 0, draftPersisted)')
  })

  it('never leaves the recoverable default to speak for an unpersisted draft', () => {
    expect(embed).not.toContain('draft.trim().length > 0)')
    expect(side).not.toContain('draft.trim().length > 0)')
  })
})

describe('a mode switch carries the pending knowledge selection', () => {
  const knowledge = readFileSync(join(__dirname, '..', 'pages', 'chat', 'useKnowledgeFetch.ts'), 'utf-8')

  it('exposes a carry that both banks and live state can answer', () => {
    expect(knowledge).toContain('carryPendingKnowledge')
    // A COPY: the source is released by `dropCarriedKnowledge` after the delete succeeds,
    // so a rejected delete leaves the surviving slot holding its own selection.
    expect(knowledge).not.toContain('slotMapRef.current.delete(from)')
    // Live state matters: whether the slot-change effect has banked the value is not knowable
    // to the caller, so reading only the map would drop it half the time.
    expect(knowledge).toContain('prevSlotRef.current === from ? pendingKnowledge : null')
  })

  it('is called by the migration that moves the other buckets', () => {
    expect(CHAT_PAGE).toContain('knowledgeFetchRef.current.carryPendingKnowledge(from, to)')
  })

  it('is returned from the hook, so the ref can reach it', () => {
    expect(knowledge).toContain('carryPendingKnowledge, dropCarriedKnowledge }')
  })
})
