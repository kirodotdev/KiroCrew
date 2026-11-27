import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { PeopleRow } from '../../api/sharedCrewUi'
import type { Slot } from './types'
import { useSidebarCreators } from './creators'

const human = (id: string, login: string) => ({ id, kind: 'human' as const, display: { name: '', login, captured_at: null } })
const slot = (key: string, creator: ReturnType<typeof human>) =>
  ({ key, title: key, running: false, messages: 1, attribution: { creator, availability: 'recorded', created_at: 1 } }) as unknown as Slot

describe('useSidebarCreators', () => {
  it('tags the owner row when the owner and a teammate carry the same e-mail, so the two read apart', () => {
    const email = 'bo@example.test'
    const people: PeopleRow[] = [
      { principal: human('p_me', 'siren@example.test'), sources: ['observed'], is_viewer: true },
      { principal: human('p_owner', email), sources: ['owner'], is_owner: true },
      { principal: human('p_bo', email), sources: ['observed'] },
    ]
    const { result } = renderHook(() => useSidebarCreators({
      shown: true,
      selectedKeys: new Set(),
      people,
      viewerIds: new Set(['p_me']),
      rows: [slot('a', human('p_owner', email)), slot('b', human('p_bo', email))],
    }))
    const labels = result.current.creatorFilterRows.map(row => row.label)
    expect(labels).toContain(`${email} (Crew owner)`)
    expect(labels).toContain(email)
    expect(new Set(labels).size).toBe(labels.length)
  })
})
