import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import MarkdownRenderer, { COPY_FAILED_FLASH_MS } from '../components/MarkdownRenderer'
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
const CHIP = 'md-broken-image-chip'

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue({ status: 404 })
  vi.stubGlobal('fetch', fetchMock)
  vi.mocked(copyToClipboard).mockClear()
})

function renderErrored(content: string) {
  const utils = render(<MarkdownRenderer content={content} />)
  let img = utils.container.querySelector('img')
  if (!img) {
    const approval = utils.container.querySelector('button')
    if (approval) fireEvent.click(approval)
    img = utils.container.querySelector('img')
  }
  if (!img) throw new Error('no <img> rendered')
  fireEvent.error(img)
  return utils
}

describe('broken-image fallback chip', () => {
  it('names the missing-file condition only after the probe confirms 404', async () => {
    const { getByTestId, getByText, findByText } = renderErrored(`![账户列表](${GONE})`)
    getByText('账户列表')
    await findByText(MISSING_LABEL)
    // The probe re-asked the same endpoint the <img> failed on.
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/file-raw?path=${encodeURIComponent(GONE)}`,
      { method: 'HEAD' },
    )
    // The chip is an interactive copy affordance, not a mute span; its tooltip
    // leads with the on-disk path so a truncated chip discloses the target.
    const chip = getByTestId(CHIP)
    expect(chip.getAttribute('title')).toContain(GONE)
  })

  it('states the load failure through the shared ErrorNotice, without a hand-off that could unmount a draft', async () => {
    const { getByTestId, findByText, queryByRole } = renderErrored(`![shot](${GONE})`)
    await findByText(MISSING_LABEL)
    const notice = getByTestId('md-broken-image-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice).toHaveTextContent(MISSING_LABEL)
    // The wording lives in the notice, not in the copy chip.
    expect(getByTestId(CHIP)).not.toHaveTextContent(MISSING_LABEL)
    // This renderer also previews unsaved drafts (the meeting note), so the
    // fallback never offers the navigation that would unmount one.
    expect(queryByRole('button', { name: /ask the agent/i })).toBeNull()
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

  it('copies the on-disk path on click and confirms via the title flash once the write lands', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    const { getByTestId } = renderErrored(`![账户列表](${GONE})`)
    const chip = getByTestId(CHIP)
    await act(async () => { fireEvent.click(chip) })
    expect(copyToClipboard).toHaveBeenCalledWith(GONE)
    expect(chip.getAttribute('title')).toBe('Copied!')
  })

  it('renders a refused write as the one failure surface — the notice in the bubble — never as "Copied!" and never in the flow', async () => {
    vi.useFakeTimers()
    // The probe stays in flight, so the only thing that could change the
    // sentence around the chip is the refused copy.
    fetchMock.mockReturnValue(new Promise(() => {}))
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    const { getByRole, getByTestId, getAllByTestId, getAllByRole, queryByRole, queryByTestId, container } = renderErrored(`Missing: ![账户列表](${GONE}) here.`)
    const chip = getByRole('button', { name: /账户列表/ })
    const flow = container.querySelector('p')!
    const flowBefore = flow.innerHTML
    await act(async () => { fireEvent.click(chip) })
    expect(chip.getAttribute('title')).not.toBe('Copied!')
    const notices = getAllByTestId('md-chip-copy-error')
    expect(notices).toHaveLength(1)
    // This chip shows its alt and copies the PATH, which is in the tooltip, not
    // on the page — "select the text to copy it manually" would have the reader
    // copy the alt. The notice names the object that failed to copy instead.
    expect(notices[0]).toHaveTextContent(/^Couldn’t copy the image path 01-accounts\.png\.$/)
    expect(notices[0]).not.toHaveTextContent(/select the text/i)
    expect(getByRole('tooltip').contains(notices[0])).toBe(true)
    // Nothing beside the chip: the sentence around it is byte-for-byte what
    // it was. The copy notice is the only alert besides the image's own
    // load-failure notice, which was already there before the click.
    expect(flow.innerHTML).toBe(flowBefore)
    expect(getAllByRole('alert')).toEqual([getByTestId('md-broken-image-error'), notices[0]])
    expect(queryByRole('button', { name: 'Dismiss' })).toBeNull()
    act(() => { vi.advanceTimersByTime(COPY_FAILED_FLASH_MS) })
    expect(queryByTestId('md-chip-copy-error')).toBeNull()
    vi.useRealTimers()
  })

  it('copies via keyboard activation (Enter)', () => {
    const { getByTestId } = renderErrored(`![shot](${GONE})`)
    const chip = getByTestId(CHIP)
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
    const { getByText, getByTestId } = renderErrored(`![](${GONE})`)
    getByText(GONE)
    // Label already shows the path — the tooltip must not restate it.
    expect(getByTestId(CHIP).getAttribute('title')).toBe('Click to copy')
  })

  it('decodes a producer-emitted angle-bracket destination back to the on-disk path', () => {
    const spaced = '/tmp/shots/screen one.png'
    const { getByTestId } = renderErrored('![shot](</tmp/shots/screen%20one.png>)')
    fireEvent.click(getByTestId(CHIP))
    expect(copyToClipboard).toHaveBeenCalledWith(spaced)
  })
})
