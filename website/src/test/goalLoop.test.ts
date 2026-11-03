/**
 * What the page keeps of the goal loop, which the gateway now arms from the saved
 * file (`test_doc_goal_loop.py`): reading a goal wake in the conductor's transcript
 * as its own thread, split per goal, and the board's goal rate.
 */
import { describe, it, expect } from 'vitest'
import { GOALS_BANNER, isGoalWake } from '../pages/chat/page/goalLoop'
import { GOALS_THREAD, buildAskTurn, deriveThreads, goalCheckPending, latestGoalReport, splitGoalBlocks } from '../pages/chat/page/docThreads'
import { boardHasGoals, boardGoalEvery, parseBoard, serializeBoard } from '../pages/chat/page/board/boardModel'
import type { ChatMessage } from '../types'

describe('goal wakes in the transcript', () => {
  it('split per goal: each [goal id] block goes to that goal\'s thread; writes apply once from the check', () => {
    const messages = [
      { role: 'user', content: '[Doc ask q1]\nwhy?\n\n---\ncontext' },
      { role: 'assistant', content: 'Because.' },
      { role: 'nudge', content: `[auto-nudge cycle 3]\n${GOALS_BANNER}` },
      { role: 'assistant', content: 'Checked both.\n[goal gabc1]\nMarked #26162 done.\n[goal gxyz2]\nhi\n```doc-append\n- checked\n```' },
    ] as ChatMessage[]
    const threads = deriveThreads(messages)
    expect(threads.q1.messages.map(m => m.text)).toEqual(['why?', 'Because.'])
    expect(threads[GOALS_THREAD].actions).toHaveLength(1)
    expect(threads[GOALS_THREAD].messages.map(m => m.text)).toEqual(['Checked both.'])
    expect(latestGoalReport(threads, 'gabc1')).toBe('Marked #26162 done.')
    expect(latestGoalReport(threads, 'gxyz2')).toBe('hi')
    expect(goalCheckPending(threads)).toBe(false)
    expect(isGoalWake('[auto-nudge cycle 1]\nsomething else')).toBe(false)
  })

  it('a check still running shows every goal working; a reply on a goal\'s pin continues its thread', () => {
    const messages = [
      { role: 'nudge', content: `[auto-nudge cycle 1]\n${GOALS_BANNER}` },
      { role: 'assistant', content: '[goal gabc1]\nTwo tickets moved.' },
      { role: 'user', content: '[Doc ask goal-gabc1]\nwhich two?\n\n---\ncontext' },
      { role: 'assistant', content: '#1 and #2.' },
      { role: 'nudge', content: `[auto-nudge cycle 2]\n${GOALS_BANNER}` },
    ] as ChatMessage[]
    const threads = deriveThreads(messages)
    expect(threads['goal-gabc1'].messages.map(m => `${m.who}: ${m.text}`)).toEqual(['conductor: Two tickets moved.', 'you: which two?', 'conductor: #1 and #2.'])
    expect(goalCheckPending(threads)).toBe(true)
    expect(splitGoalBlocks('no blocks here')).toEqual({ blocks: [], rest: 'no blocks here' })
  })

  it('a reply on a goal\'s pin tells the conductor it is about that goal\'s section', () => {
    const turn = buildAskTurn({ id: 'goal-gabc1', request: 'which two?', quote: '## Tickets\n- one', doc: 'x', title: null, copy: null })
    expect(turn.startsWith('[Doc ask goal-gabc1]\nwhich two?')).toBe(true)
    expect(turn).toContain("The goal's section:")
    expect(turn).toContain('## Tickets\n- one')
  })
})

describe('board goal rate', () => {
  it('keeps one rate for the board and knows when it has a goal', () => {
    const b = parseBoard(JSON.stringify({ goalEvery: 30, shapes: [{ id: 's', kind: 'section', x: 0, y: 0, w: 300, h: 300, text: 'Todo', goal: { text: 'check', every: 10 } }], connectors: [] }))
    expect(boardGoalEvery(b)).toBe(30)
    expect(boardHasGoals(b)).toBe(true)
    expect(parseBoard(serializeBoard(b)).goalEvery).toBe(30)
    expect(boardGoalEvery(parseBoard(JSON.stringify({ goalEvery: 7, shapes: [] })))).toBe(10)
  })
})
