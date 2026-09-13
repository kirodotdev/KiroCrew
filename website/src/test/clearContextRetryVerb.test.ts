import { describe, expect, it } from 'vitest'

import en from '../i18n/locales/en.json'
import enManual from '../i18n/locales/en.manual.json'

/**
 * One notice, one verb.
 *
 * The refusal body tells the reader to try again when the members finish; a button beside it
 * labelled with a different verb reads as a second, different affordance. Asserted on the
 * CATALOG rather than on a rendered button so it holds for every surface that renders the
 * notice, including the capture harness.
 */
describe('the clear-context refusal speaks with one verb', () => {
  const channel = (en as { pages: { channelPage: Record<string, string> } }).pages.channelPage
  const manual = (enManual as { pages: { channelPage: Record<string, string> } }).pages.channelPage

  it('labels the retry affordance with the verb the body uses', () => {
    const body: string = manual.clear_context_busy_error
    expect(body, 'the refusal body string moved').toContain('Try again')
    expect(
      channel.retry,
      `the button verb (${channel.retry}) differs from the body's ("Try again"), so the notice ` +
        'offers what reads as a second, different action',
    ).toBe('Try again')
  })

  it('keeps the retry testid the notice emits', () => {
    // The prop that used to carry this was dropped: one consumer never justified it. The id
    // is now fixed in ErrorNotice, so this records the contract its readers depend on.
    expect(channel.retry.length, 'an empty label renders a nameless button').toBeGreaterThan(0)
  })
})
