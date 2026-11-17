import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { ErrorCard, limitResetTime } from '../pages/chat/ErrorCard'
import { fmtDateTime } from '../i18n/format'
import { FEATURE_REQUEST_FORM_URL } from '../prompts/featureRequest'

const PROSE = "You've hit your session limit \u00b7 resets 5:50pm"
const AT = '2026-11-14T23:50:00+00:00'

afterEach(cleanup)

/**
 * A spent usage limit whose provider sentence names when it opens again: the
 * backend stamps `meta.reset_at` (an ISO instant) on the row, and the card adds
 * one read-only line with that time in the reader's locale and zone.
 */
describe('ErrorCard — a usage limit that names when it resets', () => {
  it('shows the reset time under the provider sentence, beside Resume', () => {
    render(<ErrorCard content={PROSE} meta={{ kind: 'usage_limit', reset_at: AT }} onContinue={() => undefined} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent("You've hit your session limit")
    expect(screen.getByTestId('error-card-limit-reset')).toHaveTextContent(
      `The limit resets on ${fmtDateTime(Date.parse(AT))}.`,
    )
    expect(screen.getByTestId('error-card-continue')).toBeVisible()
  })

  it('shows it on the row with no Resume and on the feature-request form row too', () => {
    render(<ErrorCard content={PROSE} meta={{ kind: 'usage_limit', reset_at: AT }} />)
    expect(screen.getByTestId('error-card-limit-reset')).toBeVisible()
    cleanup()
    render(<ErrorCard content={PROSE} meta={{ kind: 'usage_limit', reset_at: AT }} featureRequestFormUrl={FEATURE_REQUEST_FORM_URL} />)
    expect(screen.getByTestId('error-card-limit-reset')).toBeVisible()
  })

  it.each([
    ['absent', { kind: 'usage_limit' }],
    ['not a date', { kind: 'usage_limit', reset_at: 'soon' }],
    ['a number, not an ISO instant', { kind: 'usage_limit', reset_at: 1_900_000_000 }],
    ['on a row that is not a usage limit', { kind: 'auth_required', reset_at: AT }],
  ])('shows nothing when the time is %s', (_label, meta) => {
    render(<ErrorCard content={PROSE} meta={meta} onContinue={() => undefined} />)
    expect(screen.queryByTestId('error-card-limit-reset')).toBeNull()
    expect(limitResetTime(meta)).toBeUndefined()
  })
})
