/**
 * The reported bug, rendered.
 *
 * In Chinese the reasoning-effort popover showed a translated heading (`强度`) next
 * to an untranslated value (`High`), because the level labels lived as raw English
 * in an ALL-CAPS table in `lib/effort.ts` that no i18n gate could see. A catalog
 * assertion alone would not have caught it — the keys did not exist at all — so
 * this mounts the real component under a non-English language and reads what the
 * user would read.
 */

import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import { api } from '../api/client'
import { i18next } from '../i18n/index'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const baseProps = { slot: 'dashboard:1', onClose: vi.fn(), embedded: true }

beforeEach(() => {
  vi.spyOn(api, 'effortLevels').mockResolvedValue(['low', 'medium', 'high', 'max'] as never)
  vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue(undefined as never)
})

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('ReasoningEffortDropdown — level label localisation', () => {
  it('renders the English label in English', async () => {
    await i18next.changeLanguage('en')
    wrap(<ReasoningEffortDropdown {...baseProps} currentEffort="high" />)
    await waitFor(() => expect(screen.getAllByText('High').length).toBeGreaterThan(0))
  })

  it('renders the localised label in Chinese, not raw English', async () => {
    await i18next.changeLanguage('zh-CN')
    const zhHigh = i18next.t('lib.effort.high') as string
    // Guard the guard: if the catalog were missing the key, i18next would fall
    // back to English and this test would pass while the bug persisted.
    expect(zhHigh).not.toBe('High')

    wrap(<ReasoningEffortDropdown {...baseProps} currentEffort="high" />)
    await waitFor(() => expect(screen.getAllByText(zhHigh).length).toBeGreaterThan(0))
    expect(screen.queryByText('High')).toBeNull()
  })

  it('localises the inherited-default label too', async () => {
    await i18next.changeLanguage('zh-CN')
    wrap(<ReasoningEffortDropdown {...baseProps} currentEffort="" defaultEffort="high" />)
    const expected = i18next.t('components.reasoningEffortDropdown.default_with_level', {
      level: i18next.t('lib.effort.high'),
    }) as string
    await waitFor(() => expect(screen.getAllByText(expected).length).toBeGreaterThan(0))
    expect(screen.queryByText(/^Default/)).toBeNull()
  })
})
