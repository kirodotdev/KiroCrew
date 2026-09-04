import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(undefined),
}))

/**
 * The broken-image fallback chip must say WHAT happened and hand the user the
 * path — chat images are read from disk at view time (/api/file-raw), so the
 * dominant failure is a local file deleted after the message was written (a
 * temp-directory screenshot cleaned up hours later). A mute icon-only fallback
 * left users unable to tell whether the message was broken or the file was
 * gone, with nothing to click (the reported UX gap this pins the fix for).
 *
 * The <img> error event carries no status, so the missing-file wording is
 * earned only by a HEAD probe confirming the backend's 404 — a 403 denial, a
 * transient hiccup, or a failed probe must all keep the generic wording, or
 * the chip asserts a deletion it never verified.
 */

const GONE = '/tmp/aws-control-tour/01-accounts.png'
const MISSING_LABEL = 'Image file no longer exists'
const GENERIC_LABEL = 'Image failed to load'

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue({ status: 404 })
  vi.stubGlobal('fetch', fetchMock)
  vi.mocked(copyToClipboard).mockClear()
})

function renderErrored(content: string) {
  const utils = render(<MarkdownRenderer content={content} />)
  const img = utils.container.querySelector('img')
  if (!img) throw new Error('no <img> rendered')
  fireEvent.error(img)
  return utils
}

describe('broken-image fallback chip', () => {
  it('names the missing-file condition only after the probe confirms 404', async () => {
    const { getByRole, getByText, findByText } = renderErrored(`![账户列表](${GONE})`)
    getByText('账户列表')
    await findByText(MISSING_LABEL)
    // The probe re-asked the same endpoint the <img> failed on.
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/file-raw?path=${encodeURIComponent(GONE)}`,
      { method: 'HEAD' },
    )
    // The chip is an interactive copy affordance, not a mute span; its tooltip
    // leads with the on-disk path so a truncated chip discloses the target.
    const chip = getByRole('button')
    expect(chip.getAttribute('title')).toContain(GONE)
  })

  it('keeps the generic wording when the probe answers 403 (denied is not deleted)', async () => {
    fetchMock.mockResolvedValue({ status: 403 })
    const { getByText, queryByText } = renderErrored(`![shot](${GONE})`)
    getByText(GENERIC_LABEL)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(queryByText(MISSING_LABEL)).toBeNull()
  })

  it('keeps the generic wording when the probe itself fails', async () => {
    fetchMock.mockRejectedValue(new Error('network down'))
    const { getByText, queryByText } = renderErrored(`![shot](${GONE})`)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    getByText(GENERIC_LABEL)
    expect(queryByText(MISSING_LABEL)).toBeNull()
  })

  it('copies the on-disk path on click and confirms via the title flash', async () => {
    const { getByRole } = renderErrored(`![账户列表](${GONE})`)
    const chip = getByRole('button')
    fireEvent.click(chip)
    expect(copyToClipboard).toHaveBeenCalledWith(GONE)
    expect(chip.getAttribute('title')).toBe('Copied!')
  })

  it('copies via keyboard activation (Enter)', () => {
    const { getByRole } = renderErrored(`![shot](${GONE})`)
    const chip = getByRole('button')
    expect(chip.getAttribute('tabindex')).toBe('0')
    fireEvent.keyDown(chip, { key: 'Enter' })
    expect(copyToClipboard).toHaveBeenCalledWith(GONE)
  })

  it('never probes a remote URL and uses the generic wording', async () => {
    const { getByText, queryByText } = renderErrored('![chart](https://example.com/chart.png)')
    getByText(GENERIC_LABEL)
    expect(queryByText(MISSING_LABEL)).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('falls back to the path as the visible label without repeating it in the tooltip', () => {
    const { getByText, getByRole } = renderErrored(`![](${GONE})`)
    getByText(GONE)
    // Label already shows the path — the tooltip must not restate it.
    expect(getByRole('button').getAttribute('title')).toBe('Click to copy')
  })

  it('decodes a producer-emitted angle-bracket destination back to the on-disk path', () => {
    const spaced = '/tmp/shots/screen one.png'
    const { getByRole } = renderErrored('![shot](</tmp/shots/screen%20one.png>)')
    fireEvent.click(getByRole('button'))
    expect(copyToClipboard).toHaveBeenCalledWith(spaced)
  })
})
