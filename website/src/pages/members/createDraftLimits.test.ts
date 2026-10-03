import { describe, expect, it } from 'vitest'
import { JOB_MAX, NAME_MAX } from '../../components/MeetCrewmatesFlow'
import { createDraftFromHref, draftFromParams } from './MembersPage'

describe('create-link drafts are held to the guided flow limits', () => {
  it('cuts name and goal to the flow input lengths', () => {
    const p = new URLSearchParams({ create: '1', name: 'n'.repeat(NAME_MAX + 40), goal: 'g'.repeat(JOB_MAX + 500) })
    const d = draftFromParams(p)
    expect(d.name).toHaveLength(NAME_MAX)
    expect(d.goal).toHaveLength(JOB_MAX)
  })

  it('drops control characters from both fields', () => {
    const d = draftFromParams(new URLSearchParams({ name: 'Re\u0007lease\u007f', goal: 'line one\nline\ttwo\u0000' }))
    expect(d).toEqual({ name: 'Release', goal: 'line onelinetwo' })
  })

  it('applies the same limits to a same-origin create link', () => {
    const href = `/members?create=1&name=${'x'.repeat(NAME_MAX + 5)}&goal=${'y'.repeat(JOB_MAX + 5)}`
    const d = createDraftFromHref(href, 'http://127.0.0.1:7777/chat')
    expect(d?.name).toHaveLength(NAME_MAX)
    expect(d?.goal).toHaveLength(JOB_MAX)
  })
})
