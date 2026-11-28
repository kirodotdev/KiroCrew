import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { sharedCrewKeys } from './sharedCrewSocket'
import { useEditRuleViewer } from './useSharedCrew'

describe('useEditRuleViewer', () => {
  it('keeps one viewer object across renders while the mode answer is unchanged', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    client.setQueryData(sharedCrewKeys.mode, { viewer_ids: ['p_owner_local'], edit_rule: { is_owner: true, relay_teammate: false } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const { result, rerender } = renderHook(() => useEditRuleViewer(), { wrapper })
    const first = result.current
    expect(first).not.toBeNull()
    rerender()
    expect(result.current).toBe(first)
  })
})
