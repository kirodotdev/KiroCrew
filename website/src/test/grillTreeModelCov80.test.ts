// Pure reducer + selector coverage for the grill question tree. Every action
// branch, the subtree walk used by `prune`, and each selector's filter.
import { describe, it, expect } from 'vitest'
import {
  grillReducer,
  promotedResearch,
  answeredClarifiers,
  nodeDepth,
  suggestedMaxCycles,
  type GrillNode,
  type GrillAction,
} from '../apps/auto-research/grillTreeModel'

function node(over: Partial<GrillNode> & { id: string }): GrillNode {
  return {
    parent: null,
    kind: 'clarifier',
    text: 'qq-' + over.id,
    recommended: '',
    answer: '',
    origin: 'grill',
    status: 'open',
    ...over,
  }
}

describe('grillReducer', () => {
  it('addChildren promotes research nodes and leaves clarifiers open', () => {
    const out = grillReducer([], {
      type: 'addChildren',
      nodes: [node({ id: 'r1', kind: 'research' }), node({ id: 'c1' })],
    })
    expect(out.map(n => [n.id, n.status])).toEqual([['r1', 'promoted'], ['c1', 'open']])
  })

  it('setAnswer stores the answer and marks the node answered', () => {
    const out = grillReducer([node({ id: 'c1' })], { type: 'setAnswer', id: 'c1', answer: 'zzz' })
    expect(out[0]).toMatchObject({ answer: 'zzz', status: 'answered' })
  })

  it('setAnswer leaves unrelated nodes untouched', () => {
    const tree = [node({ id: 'c1' }), node({ id: 'c2' })]
    const out = grillReducer(tree, { type: 'setAnswer', id: 'c1', answer: 'zzz' })
    expect(out[1]).toBe(tree[1])
  })

  it('accept copies the recommendation into the answer', () => {
    const out = grillReducer([node({ id: 'c1', recommended: 'rec-a' })], { type: 'accept', id: 'c1' })
    expect(out[0]).toMatchObject({ answer: 'rec-a', status: 'answered' })
  })

  it('investigateInstead converts a clarifier into a promoted research node', () => {
    const tree = [node({ id: 'c1', recommended: 'rec-a', answer: 'aaa', origin: 'emergent' })]
    const out = grillReducer(tree, { type: 'investigateInstead', id: 'c1' })
    expect(out[0]).toMatchObject({
      kind: 'research', origin: 'grill', status: 'promoted', recommended: '', answer: '',
    })
  })

  it('togglePromote flips a research node between promoted and open', () => {
    const promoted = [node({ id: 'r1', kind: 'research', status: 'promoted' })]
    const opened = grillReducer(promoted, { type: 'togglePromote', id: 'r1' })
    expect(opened[0].status).toBe('open')
    expect(grillReducer(opened, { type: 'togglePromote', id: 'r1' })[0].status).toBe('promoted')
  })

  it('togglePromote ignores a clarifier', () => {
    const out = grillReducer([node({ id: 'c1' })], { type: 'togglePromote', id: 'c1' })
    expect(out[0].status).toBe('open')
  })

  it('prune marks the node and every descendant pruned', () => {
    const tree = [
      node({ id: 'root', kind: 'root' }),
      node({ id: 'a', parent: 'root' }),
      node({ id: 'b', parent: 'a' }),
      node({ id: 'c', parent: 'b' }),
      node({ id: 'other', parent: 'root' }),
    ]
    const out = grillReducer(tree, { type: 'prune', id: 'a' })
    const status = Object.fromEntries(out.map(n => [n.id, n.status]))
    expect(status).toEqual({
      root: 'open', a: 'pruned', b: 'pruned', c: 'pruned', other: 'open',
    })
  })

  it('edit replaces only the text', () => {
    const out = grillReducer([node({ id: 'c1', answer: 'keep' })], { type: 'edit', id: 'c1', text: 'new-text' })
    expect(out[0]).toMatchObject({ text: 'new-text', answer: 'keep' })
  })

  it('returns the tree unchanged for an unknown action', () => {
    const tree = [node({ id: 'c1' })]
    expect(grillReducer(tree, { type: 'nope' } as unknown as GrillAction)).toBe(tree)
  })
})

describe('promotedResearch', () => {
  it('returns promoted research depth-first with a defaulted origin', () => {
    const tree = [
      node({ id: 'root', kind: 'root' }),
      node({ id: 'r1', parent: 'root', kind: 'research', status: 'promoted', text: ' aaa ', origin: '' }),
      node({ id: 'r1a', parent: 'r1', kind: 'research', status: 'promoted', text: 'bbb', origin: 'emergent' }),
      node({ id: 'r2', parent: 'root', kind: 'research', status: 'promoted', text: 'ccc' }),
    ]
    expect(promotedResearch(tree)).toEqual([
      { text: 'aaa', origin: 'grill' },
      { text: 'bbb', origin: 'emergent' },
      { text: 'ccc', origin: 'grill' },
    ])
  })

  it('drops pruned, non-promoted, clarifier and blank-text nodes', () => {
    const tree = [
      node({ id: 'r1', kind: 'research', status: 'pruned', text: 'pruned' }),
      node({ id: 'r2', kind: 'research', status: 'open', text: 'open' }),
      node({ id: 'r3', kind: 'research', status: 'promoted', text: '   ' }),
      node({ id: 'c1', status: 'promoted', text: 'clar' }),
    ]
    expect(promotedResearch(tree)).toEqual([])
  })
})

describe('answeredClarifiers', () => {
  it('returns trimmed q/a pairs for answered clarifiers only', () => {
    const tree = [
      node({ id: 'c1', status: 'answered', text: ' qA ', answer: ' aA ' }),
      node({ id: 'c2', status: 'answered', answer: '  ' }),
      node({ id: 'c3', status: 'open', answer: 'aC' }),
      node({ id: 'r1', kind: 'research', status: 'answered', answer: 'aR' }),
    ]
    expect(answeredClarifiers(tree)).toEqual([{ q: 'qA', a: 'aA' }])
  })
})

describe('nodeDepth', () => {
  const tree = [
    node({ id: 'root', kind: 'root' }),
    node({ id: 'a', parent: 'root' }),
    node({ id: 'b', parent: 'a' }),
  ]

  it('counts hops to the root', () => {
    expect(nodeDepth(tree, 'root')).toBe(0)
    expect(nodeDepth(tree, 'a')).toBe(1)
    expect(nodeDepth(tree, 'b')).toBe(2)
  })

  it('returns -1 for an absent id', () => {
    expect(nodeDepth(tree, 'ghost')).toBe(-1)
  })

  it('stops instead of looping when a parent id is missing', () => {
    expect(nodeDepth([node({ id: 'orphan', parent: 'gone' })], 'orphan')).toBe(1)
  })

  it('stops on a parent cycle', () => {
    const cyclic = [node({ id: 'x', parent: 'y' }), node({ id: 'y', parent: 'x' })]
    expect(nodeDepth(cyclic, 'x')).toBe(2)
  })
})

describe('suggestedMaxCycles', () => {
  it('is zero for no committed sub-questions', () => {
    expect(suggestedMaxCycles(0)).toBe(0)
    expect(suggestedMaxCycles(-3)).toBe(0)
  })

  it('is N + ceil(N/3) + 1 otherwise', () => {
    expect(suggestedMaxCycles(1)).toBe(3)
    expect(suggestedMaxCycles(3)).toBe(5)
    expect(suggestedMaxCycles(4)).toBe(7)
  })
})
