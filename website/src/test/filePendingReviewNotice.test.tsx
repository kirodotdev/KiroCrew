/**
 * A save refused because its settings file has a change waiting for review.
 *
 * The gateway answers every such save with `code: file_pending_review`; the
 * dashboard shows its own translated sentence (never the gateway's English) and
 * the error notice links to the review card on Settings > Security, so the user
 * can apply or discard the change without a terminal.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'
import { friendlyErrText } from '../api/client'
import {
  FILE_PENDING_REVIEW_CODE,
  FILE_PENDING_REVIEW_URL,
  filePendingReviewMessage,
} from '../api/apiError'

const GATEWAY_SENTENCE = 'This settings file was changed outside Kiro Crew, so this save was not made.'

const refusal = () =>
  JSON.stringify({ error: GATEWAY_SENTENCE, code: FILE_PENDING_REVIEW_CODE })

describe('a save refused for a file waiting for review', () => {
  it('reads as the translated sentence for every status the savers use', () => {
    for (const status of [400, 409]) {
      expect(friendlyErrText(status, refusal())).toBe(filePendingReviewMessage())
    }
    expect(filePendingReviewMessage()).toMatch(/Settings → Security/)
    expect(filePendingReviewMessage()).not.toBe(GATEWAY_SENTENCE)
  })

  it('links the error notice to the review card', () => {
    render(<ErrorNotice message={friendlyErrText(400, refusal())} />)
    const link = screen.getByRole('link', { name: 'Review the change' })
    expect(link).toHaveAttribute('href', FILE_PENDING_REVIEW_URL)
    expect(FILE_PENDING_REVIEW_URL).toBe('/settings/security?highlight=security.config-pending')
    expect(screen.getByRole('alert')).toHaveTextContent(filePendingReviewMessage())
  })

  it('leaves any other refusal unlinked', () => {
    const other = friendlyErrText(400, JSON.stringify({ error: 'nope', code: 'config_write_refused' }))
    render(<ErrorNotice message={other} />)
    expect(screen.queryByRole('link')).toBeNull()
  })
})
