import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const H = vi.hoisted(() => ({
  members: vi.fn(),
  autonudgeList: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    members: H.members,
    autonudgeList: H.autonudgeList,
  },
}))

import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import {
  PERPETUAL_DEFAULT_BANNER,
  PERPETUAL_DEFAULT_INSTRUCTION,
  isDefaultPerpetualBrief,
  perpetualBriefText,
  useCrewPerpetual,
} from './useCrewPerpetual'

describe('perpetualBriefText', () => {
  // The instruction row on the Crewmates page shows a loop's banner (else its
  // message). For a loop the owner's switch armed, both are the switch's own
  // default brief, which only restates the block's title -- hidden. Anything
  // the crewmate wrote itself is kept, whichever field carries it.
  it('treats exactly the default banner, the default instruction and the feature name as default', () => {
    expect(isDefaultPerpetualBrief(PERPETUAL_DEFAULT_BANNER)).toBe(true)
    expect(isDefaultPerpetualBrief(PERPETUAL_DEFAULT_INSTRUCTION)).toBe(true)
    expect(isDefaultPerpetualBrief('Perpetual mode')).toBe(true)
    expect(isDefaultPerpetualBrief('')).toBe(true)
    expect(isDefaultPerpetualBrief(undefined)).toBe(true)
    // A word changed is a brief of its own.
    expect(isDefaultPerpetualBrief(`${PERPETUAL_DEFAULT_BANNER}.`)).toBe(false)
    expect(isDefaultPerpetualBrief('Perpetual mode: watch the queue')).toBe(false)
  })

  it('hides the owner-armed default, keeps a custom banner, and falls through to a custom message under the default banner', () => {
    expect(perpetualBriefText({ banner: PERPETUAL_DEFAULT_BANNER, message: PERPETUAL_DEFAULT_INSTRUCTION })).toBe('')
    expect(perpetualBriefText({ banner: '', message: '' })).toBe('')
    expect(perpetualBriefText({ banner: 'watching PR #123', message: PERPETUAL_DEFAULT_INSTRUCTION })).toBe('watching PR #123')
    expect(perpetualBriefText({ banner: PERPETUAL_DEFAULT_BANNER, message: 'Triage the inbox.' })).toBe('Triage the inbox.')
    expect(perpetualBriefText({ banner: '', message: 'Patrol the queue.\nSecond line' })).toBe('Patrol the queue.\nSecond line')
  })
})

describe('useCrewPerpetual', () => {
  it('refreshes the roster after a registry update when floor polling is off', async () => {
    H.members.mockResolvedValue({
      members: [{ name: 'Radar', slug: 'radar', slot_key: 'member-radar', perpetual: 'on' }],
    })
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )

    renderHook(() => useCrewPerpetual('Radar', { poll: false }), { wrapper })

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
    })
    expect(H.autonudgeList).toHaveBeenCalledTimes(1)
  })
})
