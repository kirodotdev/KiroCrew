import { describe, expect, it } from 'vitest'
import type { CardPlanStep } from '../api/cards'
import { fillBody, fillPath } from './runPlan'

const goalStep: CardPlanStep = {
  method: 'PUT',
  path: '/api/members/{{step0.member_id}}/goals/long-term',
  body: { member: '{{step0.name}}', long_term: 'watch PRs', expected: '' },
  fill: [
    { field: 'slug', source: 'step', step: 0, key: 'member_id', in: 'path' },
    { field: 'member', source: 'step', step: 0, key: 'name' },
  ],
}

describe('runPlan fills', () => {
  it('puts an earlier response into the path, encoded as one segment', () => {
    expect(fillPath(goalStep, [{ member_id: 'm-1a2b', name: 'scout' }])).toBe(
      '/api/members/m-1a2b/goals/long-term',
    )
    expect(fillPath(goalStep, [{ member_id: 'a/b', name: 'scout' }])).toBe(
      '/api/members/a%2Fb/goals/long-term',
    )
  })

  it('leaves the body alone for a path fill and fills its body fields', () => {
    expect(fillBody(goalStep, {}, [{ member_id: 'm-1a2b', name: 'scout' }])).toEqual({
      member: 'scout',
      long_term: 'watch PRs',
      expected: '',
    })
  })

  it('keeps a step without path fills on its planned path', () => {
    const plain: CardPlanStep = { method: 'POST', path: '/api/agents', body: { name: 'scout' } }
    expect(fillPath(plain, [])).toBe('/api/agents')
  })
})
