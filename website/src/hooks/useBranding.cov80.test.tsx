/**
 * BrandingProvider / useBranding — bot name + avatar fetched once on mount.
 *
 * Every branch here is a fallback: the defaults before the fetch resolves, the
 * per-field fallback when the backend answers with blanks, and the swallowed
 * rejection that must leave the defaults in place rather than blank the header.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const branding = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({ api: { branding } }))

import { BrandingProvider, useBranding } from './useBranding'

function Consumer() {
  const b = useBranding()
  return <span data-testid="zzq-brand">{`${b.botName}|${b.avatar}`}</span>
}

function renderProvider() {
  // In production BrandingProvider runs on the app-wide QueryClient; tests
  // supply their own (see useBrandingDirectLocal.test.tsx). retry: false so the
  // rejection case settles at once rather than waiting out the retry ladder.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <BrandingProvider>
        <Consumer />
      </BrandingProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  branding.mockReset()
})

describe('BrandingProvider', () => {
  it('adopts the backend bot name and avatar', async () => {
    branding.mockResolvedValue({ bot_name: 'Zzqbot', avatar: '/zzq/avatar.png' })
    renderProvider()
    await waitFor(() =>
      expect(screen.getByTestId('zzq-brand').textContent).toBe('Zzqbot|/zzq/avatar.png'),
    )
  })

  it('falls back per field when the backend answers with blanks', async () => {
    branding.mockResolvedValue({ bot_name: '', avatar: '' })
    renderProvider()
    await waitFor(() => expect(branding).toHaveBeenCalled())
    expect(screen.getByTestId('zzq-brand').textContent).toBe('Kiro Crew|/logo.png')
  })

  it('keeps the defaults when the fetch rejects', async () => {
    branding.mockRejectedValue(new Error('zzq offline'))
    renderProvider()
    await waitFor(() => expect(branding).toHaveBeenCalled())
    expect(screen.getByTestId('zzq-brand').textContent).toBe('Kiro Crew|/logo.png')
  })
})

describe('useBranding', () => {
  it('returns the defaults with no provider above it', () => {
    render(<Consumer />)
    expect(screen.getByTestId('zzq-brand').textContent).toBe('Kiro Crew|/logo.png')
    expect(branding).not.toHaveBeenCalled()
  })
})
