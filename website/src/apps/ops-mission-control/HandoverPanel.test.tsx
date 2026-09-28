/**
 * HandoverPanel — the "Copy as text" button.
 *
 * The button used to early-return whenever `navigator.clipboard` was falsy (no
 * secure context, no permission) and otherwise swallow a rejection, so a
 * blocked clipboard rendered NO feedback while also silently copying nothing.
 * It now routes through the shared `copyToClipboard` helper and gates the
 * "Copied" confirmation on the resolved boolean, so both directions are pinned
 * here: a successful copy confirms, and a failed one does not.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import HandoverPanel from './HandoverPanel'
import { opsApi, type HandoverDigest } from './api'

vi.mock('../../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../../utils/clipboard'

vi.mock('./api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('./api')>()
  return {
    ...mod,
    opsApi: {
      ...mod.opsApi,
      handover: vi.fn(),
      signals: vi.fn(),
      providers: vi.fn(),
    },
  }
})

function digest(text = 'shift handover text'): HandoverDigest {
  return {
    headline: 'All quiet.',
    text,
    open_work: { waiting_on_you: [], stalled_without_diagnosis: [], escalated: [] },
    recurring_patterns: [],
    coverage: { watching: [], not_configured: [], any_watching: false },
    autonomy: null,
  } as unknown as HandoverDigest
}

describe('HandoverPanel copy-as-text', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    vi.mocked(opsApi.handover).mockResolvedValue(digest())
    vi.mocked(opsApi.providers).mockResolvedValue({ providers: [] })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('copies the digest text through the shared helper and confirms, then reverts', async () => {
    renderWithProviders(<HandoverPanel />)

    const btn = await screen.findByText(i18nT('apps.opsMissionControl.handoverPanel.copy_as_text'))
    fireEvent.click(btn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('shift handover text'))
    await waitFor(() =>
      expect(screen.getByText(i18nT('apps.opsMissionControl.handoverPanel.copied'))).toBeInTheDocument(),
    )

    vi.advanceTimersByTime(2100)
    await waitFor(() =>
      expect(screen.getByText(i18nT('apps.opsMissionControl.handoverPanel.copy_as_text'))).toBeInTheDocument(),
    )
  })

  it('renders no confirmation when the copy fails', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    renderWithProviders(<HandoverPanel />)

    const btn = await screen.findByText(i18nT('apps.opsMissionControl.handoverPanel.copy_as_text'))
    fireEvent.click(btn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    expect(screen.queryByText(i18nT('apps.opsMissionControl.handoverPanel.copied'))).toBeNull()
  })
})
