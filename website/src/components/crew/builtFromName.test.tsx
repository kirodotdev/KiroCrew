import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockKit = vi.hoisted(() => ({ get: vi.fn(), preview: vi.fn(), apply: vi.fn() }))
vi.mock('../../api/crewCapabilities', async (orig) => {
  const real = await orig<typeof import('../../api/crewCapabilities')>()
  return { ...real, crewWorkerKitApi: mockKit }
})

import { kitTemplateName, templateDisplayName, useBuiltFromName } from './builtFromName'

describe('templateDisplayName', () => {
  it('labels the default template and the source-defined agents a crewmate runs on as the default', () => {
    for (const id of ['kirocrew', 'kirocrew-mate-conductor', 'kirocrew-mate-worker', '']) {
      expect(templateDisplayName(id)).toBe('kirocrew (default)')
    }
  })

  it('keeps a template the user picked under its own name', () => {
    expect(templateDisplayName('release-captain')).toBe('release-captain')
  })
})

describe('kitTemplateName', () => {
  it('names a private copy by the template it was forked from, never by its id', () => {
    expect(kitTemplateName({ template: 'crew-5eed01', origin: 'release-helper', private_copy: true })).toBe('release-helper')
    expect(kitTemplateName({ template: 'crew-5eed01', origin: '', private_copy: true })).toBe('Its own copy')
  })

  it('names any other kit by its template, and no kit as none', () => {
    expect(kitTemplateName({ template: 'release-helper', origin: 'release-helper', private_copy: false })).toBe('release-helper')
    expect(kitTemplateName({ template: '', origin: '', private_copy: false })).toBeNull()
    expect(kitTemplateName(undefined)).toBeNull()
  })
})

describe('useBuiltFromName', () => {
  it('returns a failed kit read as an error, never as the default template', async () => {
    mockKit.get.mockRejectedValue(new Error('kit unreadable'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const { result } = renderHook(
      () => useBuiltFromName({ member: 'ada', kiroAgent: 'ada', ownsFile: true, enabled: true }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.error).toBe('kit unreadable'))
    expect(result.current.name).toBe('')
  })
})
