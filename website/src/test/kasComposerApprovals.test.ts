import { describe, it, expect } from 'vitest'

import type { KasApproval, KasRunRow, KasRunsSnapshot } from '../api/client'
import { approvalsForSlot } from '../apps/workflows/kas/useKasWorkflows'

function row(id: string, slotKey: string | null): KasRunRow {
  return { workflowId: id, slotKey, status: 'running' } as unknown as KasRunRow
}
function approval(key: string, workflowId: string): KasApproval {
  return { key, workflowId } as unknown as KasApproval
}

describe('approvalsForSlot (gap 9)', () => {
  it('keeps only approvals of runs reporting into the slot', () => {
    const snap: KasRunsSnapshot = {
      runs: [row('wf_a', 's1'), row('wf_b', 's2'), row('wf_c', 's1')],
      approvals: [approval('k1', 'wf_a'), approval('k2', 'wf_b'), approval('k3', 'wf_c'), approval('k4', 'wf_unknown')],
      revision: 1,
    }
    expect(approvalsForSlot(snap, 's1').map(a => a.key)).toEqual(['k1', 'k3'])
    expect(approvalsForSlot(snap, 's2').map(a => a.key)).toEqual(['k2'])
    expect(approvalsForSlot(snap, 'nope')).toEqual([])
  })

  it('is empty when the snapshot has no runs or approvals', () => {
    expect(approvalsForSlot({ runs: [], approvals: [], revision: 0 }, 's1')).toEqual([])
    expect(approvalsForSlot({ revision: 0 } as unknown as KasRunsSnapshot, 's1')).toEqual([])
  })
})
