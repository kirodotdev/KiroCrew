/** Shared fixtures for the AutoNudgePopover suites: the slot, goal-draft
 *  storage readers, render harnesses, and a loop factory. */
import { useState } from 'react'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import SessionAutomationPopover from '../components/SessionAutomationPopover'
import { normalizeAutomationRecord, type AutomationRecord } from '../monitoring/automation'
import { GOAL_DRAFT_RECORD_PREFIX } from '../utils/goalDrafts'

export const SLOT = 'chat-1-100'

export const goalRecordKeys = () => Array.from({ length: localStorage.length }, (_, index) => (
  localStorage.key(index)
)).filter((key): key is string => key?.startsWith(GOAL_DRAFT_RECORD_PREFIX) === true)

export const goalStorageSnapshot = () => goalRecordKeys()
  .sort()
  .map(key => [key, localStorage.getItem(key)])

export const isGoalRecordKey = (key: string) => key.startsWith(GOAL_DRAFT_RECORD_PREFIX)

export function renderPopover(loop: AutoNudgeLoop | null, slotKey = SLOT) {
  // A FRESH client per render: the popover reads the shared `cron-jobs` key, and
  // a client reused across tests would serve one test's stubbed rows to the next.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover
        slotKey={slotKey}
        loop={loop}
        open={true}
        onOpenChange={() => {}}
        onChange={() => {}}
        onFired={() => {}}
      />
    </QueryClientProvider>,
  )
}

export function renderStatefulPopover(loop: AutoNudgeLoop, slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Harness() {
    const [currentLoop, setCurrentLoop] = useState<AutoNudgeLoop | null>(loop)
    const [open, setOpen] = useState(true)
    return (
      <>
        <output data-testid="popover-open">{String(open)}</output>
        <AutoNudgePopover
          slotKey={slotKey}
          loop={currentLoop}
          open={open}
          onOpenChange={setOpen}
          onChange={setCurrentLoop}
          onFired={() => {}}
        />
      </>
    )
  }
  return render(
    <QueryClientProvider client={qc}>
      <Harness />
    </QueryClientProvider>,
  )
}

/** The goal editor through the real dashboard bridge, which keys the editor on
 *  the loop: a Clear's hand-up of `null` unmounts it and mounts a fresh one.
 *  `remove-automation` is the server's `removed` frame, which the store applies
 *  the same way and which can arrive before the DELETE answers. */
export function renderBridgedPopover(loop: AutoNudgeLoop, slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Harness() {
    const [automation, setAutomation] = useState<AutomationRecord | null>(() => normalizeAutomationRecord(loop))
    const [open, setOpen] = useState(true)
    return (
      <>
        <output data-testid="popover-open">{String(open)}</output>
        <button type="button" data-testid="reopen-popover" onClick={() => setOpen(true)}>reopen</button>
        <button type="button" data-testid="remove-automation" onClick={() => setAutomation(null)}>removed</button>
        <SessionAutomationPopover
          slotKey={slotKey}
          automation={automation}
          open={open}
          onOpenChange={setOpen}
          onChange={setAutomation}
        />
      </>
    )
  }
  return render(
    <QueryClientProvider client={qc}>
      <Harness />
    </QueryClientProvider>,
  )
}

export const makeLoop = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'active loop goal',
  idle_secs: 90, max_cycles: 3, cycle_count: 1, active: true, last_fire_ts: 0,
  next_due_ts: 0, ...over,
})
