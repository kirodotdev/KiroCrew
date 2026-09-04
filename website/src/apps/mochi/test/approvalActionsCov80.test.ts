/**
 * approvalActions — which endpoint a decision goes to, and the row label.
 *
 * The routing half is already pinned by `mochiApprovalPayload.test.ts`; the two
 * predicates around it were not. `isGrant` decides whether the pet says the tool
 * ran, and `approvalLabel` is what the dialog shows — an unbounded label is what
 * pushed the buttons off the panel, so the truncation is behaviour, not polish.
 */
import { describe, it, expect } from 'vitest'

import { approvalLabel, approvalRoute, isGrant } from '../panel/approvalActions'

describe('approvalRoute', () => {
  it('sends the two approval verbs to the approval endpoint', () => {
    expect(approvalRoute('approve')).toEqual({ kind: 'approval', action: 'approve' })
    expect(approvalRoute('reject')).toEqual({ kind: 'approval', action: 'reject' })
  })

  it('sends every trust verb to the slot endpoint, which is the only one that widens policy', () => {
    for (const verb of ['trust', 'trust_reads', 'trust_command', 'trust_base']) {
      expect(approvalRoute(verb)).toEqual({ kind: 'slot', action: verb })
    }
  })

  it('treats an unknown verb as an approval rather than 400-ing the route', () => {
    expect(approvalRoute('yolo')).toEqual({ kind: 'approval', action: 'approve' })
  })
})

describe('isGrant', () => {
  it('is true for everything except an explicit reject', () => {
    expect(isGrant('approve')).toBe(true)
    expect(isGrant('trust')).toBe(true)
    expect(isGrant('trust_reads')).toBe(true)
    expect(isGrant('zzq-unknown')).toBe(true)
    expect(isGrant('reject')).toBe(false)
  })
})

describe('approvalLabel', () => {
  it('joins tool and input on one line, collapsing runs of whitespace', () => {
    expect(approvalLabel('fs_read', '  /tmp/a\n\n  /tmp/b ')).toBe('fs_read /tmp/a /tmp/b')
  })

  it('drops an absent or blank input instead of leaving a trailing space', () => {
    expect(approvalLabel('fs_read')).toBe('fs_read')
    expect(approvalLabel('fs_read', '   ')).toBe('fs_read')
  })

  it('truncates past the cap and marks the cut', () => {
    const out = approvalLabel('t', 'x'.repeat(200))
    expect(out).toHaveLength(73) // 72 kept + the ellipsis character
    expect(out.endsWith('…')).toBe(true)
  })

  it('leaves a line exactly at the cap untouched', () => {
    const out = approvalLabel('x'.repeat(10), '', 10)
    expect(out).toBe('x'.repeat(10))
    expect(out.endsWith('…')).toBe(false)
  })

  it('honours a custom cap', () => {
    expect(approvalLabel('abcdef', '', 3)).toBe('abc…')
  })
})
