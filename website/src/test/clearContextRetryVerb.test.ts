import { describe, expect, it } from 'vitest'

import en from '../i18n/locales/en.json'
import enManual from '../i18n/locales/en.manual.json'

/**
 * The refusal body carries its own remedy.
 *
 * There is deliberately NO retry button beside this notice: the header and per-row Clear
 * controls already re-issue the request, so a third trigger for one destructive action was
 * dropped. That subtraction is only honest while the copy still TELLS the reader what to do,
 * which is what this asserts -- on the catalog, so it holds for every surface that renders
 * the notice, including the capture harness.
 */
describe('the clear-context refusal names its own remedy', () => {
  const manual = (enManual as { pages: { channelPage: Record<string, string> } }).pages.channelPage
  const channel = (en as { pages: { channelPage: Record<string, string> } }).pages.channelPage

  it('tells the reader to try again once the members finish', () => {
    const body: string = manual.clear_context_busy_error
    expect(body, 'the refusal body string moved').toContain('Try again')
  })

  it('offers no separate retry label, so no second verb can contradict the body', () => {
    expect(
      channel.retry,
      'a retry label came back without a control to carry it; the notice would name two ' +
        'different actions for one request',
    ).toBeUndefined()
  })
})
