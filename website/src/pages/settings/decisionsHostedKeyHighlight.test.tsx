/**
 * A settings-search link to the hosted provider's API key lands on that provider's
 * entry in the decision-model picker, and highlights it, even when the picker mounts
 * late.
 *
 * The key field is drawn once the reader chooses the hosted entry, so the link does
 * not open anything: the registry entry's `settingId` names the entry itself, which is
 * always drawn, and the highlight hook waits for it to mount instead of probing once by
 * label. Here the picker's chunk is held until the hook's first probe has run, which is
 * the slow-mount ordering.
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const gate = vi.hoisted(() => {
  let release!: () => void
  const promise = new Promise<void>(resolve => {
    release = resolve
  })
  return { promise, release }
})

// The picker's chunk resolves only when the test says so.
vi.mock('./DecisionsProviderPicker', async importOriginal => {
  await gate.promise
  return importOriginal()
})

import { api } from '../../api/client'
import { useSettingHighlight } from '../../hooks/useSettingHighlight'
import { consentOf, stubGateway, stubHostedProvider } from '../../test/decisionsCardTestUtils'
import { namedCeiling } from '../../test/namedCeiling'
import { queryClient } from '../../api/queryClient'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'
import { QueryClientProvider } from '@tanstack/react-query'

/** Ceiling for the card, then the late picker, to draw. */
const LATE_PICKER_DRAWS = namedCeiling('LATE_PICKER_DRAWS', 5000)

function Location() {
  return <div data-testid="loc">{useLocation().search}</div>
}

function SettingsHost() {
  useSettingHighlight()
  return <FeaturePreviewsSection />
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
  localStorage.clear()
  queryClient.clear()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  queryClient.clear()
  vi.restoreAllMocks()
})

describe('the API key search link with a late picker', () => {
  it('waits for the hosted entry to mount and highlights it, opening nothing', async () => {
    stubGateway(consentOf(true, { configured_endpoint: 'http://127.0.0.1:8104/v1/systemone' }))
    stubHostedProvider({ active: 'laya', configured_endpoint: 'http://127.0.0.1:8104/v1/systemone' })
    vi.spyOn(api, 'secretsList').mockResolvedValue({ names: [] } as never)
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/settings/developer?sub=feature-previews&highlight=developer.jev-api-key']}>
          <Routes>
            <Route path="/settings/*" element={<SettingsHost />} />
          </Routes>
          <Location />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findByRole('switch', { name: 'Decisions' }, LATE_PICKER_DRAWS)
    // The hook's first probe runs while the picker's chunk is still held.
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(screen.getByTestId('loc').textContent).toContain('highlight=developer.jev-api-key')
    gate.release()
    const entry = await screen.findByTestId('decisions-provider-hosted-entry', {}, LATE_PICKER_DRAWS)
    // The flash must not look like the purple ring of a selected option beside it.
    await waitFor(() => expect(entry.style.outlineStyle).toBe('dashed'), LATE_PICKER_DRAWS)
    await waitFor(() => {
      expect(screen.getByTestId('loc').textContent).not.toContain('highlight=')
    }, LATE_PICKER_DRAWS)
    // The link opened nothing and chose nothing: the reader opens the entry by picking it.
    expect(screen.queryByTestId('decisions-hosted-key')).toBeNull()
    expect(screen.getByRole('radio', { name: /Jev, hosted by TypeSafe/ })).not.toBeChecked()
    expect(screen.queryByRole('button', { name: /^Use / })).toBeNull()
  })
})
