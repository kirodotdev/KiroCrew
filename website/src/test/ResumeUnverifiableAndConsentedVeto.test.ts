// GPT F2 and UX at 69194c04a: a veto overrode consent the user had already given, and the
// resume route asserted a draft loss it could not verify.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { unsentConfirmKey } from '../hooks/useSessionActions'

const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
const EN = JSON.parse(readFileSync(join(__dirname, '..', 'i18n', 'locales', 'en.manual.json'), 'utf-8'))
const RIDERS = EN.hooks.useSessionActions

describe('the resume route does not assert a loss it could not check', () => {
  it('maps the unverifiable tier to its own key, as the close route does', () => {
    expect(unsentConfirmKey('unverifiable', 'resume'))
      .toBe('hooks.useSessionActions.resume_unsent_confirm_unverifiable')
  })

  it('does NOT fall through to the copy that states the work will be lost', () => {
    expect(unsentConfirmKey('unverifiable', 'resume'))
      .not.toBe('hooks.useSessionActions.resume_unsent_confirm')
    expect(RIDERS.resume_unsent_confirm).toContain('It will be lost')
  })

  it('says the CHECK failed, never that a draft exists', () => {
    const copy = RIDERS.resume_unsent_confirm_unverifiable
    expect(copy).toContain("Couldn't check")
    expect(copy).not.toContain('It will be lost')
    expect(copy).not.toContain('has unsent work')
  })

  it('keeps the close route own unverifiable branch intact', () => {
    expect(unsentConfirmKey('unverifiable')).toBe('hooks.useSessionActions.close_unsent_confirm_unverifiable')
  })
})

describe('a remote veto outranks consent; consent gates only the local signal', () => {
  it('honours the veto UNCONDITIONALLY, outside the consent gate', () => {
    expect(HOOK).toContain('closingIntentVetoed(intent) || (consentedAt === null && slotUnsentWorkSource(slotKey) !== null)')
  })

  it('no longer lets consent suppress another window\u2019s veto', () => {
    // The suppressed form destroyed window B's draft: A consented to its OWN draft only.
    expect(HOOK).not.toContain('consentedAt === null && (closingIntentVetoed(intent)')
  })

  it('still gates the LOCAL re-read on consent, so A does not block on its own draft', () => {
    expect(HOOK).toContain('consentedAt === null && slotUnsentWorkSource(slotKey) !== null')
  })

  it('cannot deadlock on the closing window itself: it never answers its own intent', () => {
    // `storage` fires only in OTHER same-origin contexts, so a veto is always remote.
    const reg = readFileSync(join(__dirname, '..', 'utils', 'slotComposerRegistry.ts'), 'utf-8')
    expect(reg).toContain("window.addEventListener('storage', answerClosingIntent)")
  })

  it('no longer aborts on a bare veto, which stalled the close while a composer stayed dirty', () => {
    expect(HOOK).not.toContain('if (closingIntentVetoed(intent) || appeared)')
  })

  it('states the layout-effect publish, not a debounce the registry does not use', () => {
    // The claim is published in a layout effect (useSlotComposerRegistration); only the draft
    // COPY is debounced, so the old premise named the wrong mechanism.
    expect(HOOK).toContain('publishes a claim in a layout effect')
    expect(HOOK).not.toContain('reads a claim published on a debounce')
  })
})
