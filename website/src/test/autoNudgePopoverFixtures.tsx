/** Shared fixtures for the AutoNudgePopover suites: the slot, a fake gateway
 *  for the goal-draft routes, render harnesses, and a loop factory. */
import { useState } from 'react'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { vi } from 'vitest'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import SessionAutomationPopover from '../components/SessionAutomationPopover'
import { normalizeAutomationRecord, type AutomationRecord } from '../monitoring/automation'

export const SLOT = 'chat-1-100'

export type WireDraft = { message: string, idle_secs: number, max_cycles: number }
export type WireRecord = { draft: WireDraft | null, updated_at: number }

export const wireDraft = (message: string, idle_secs = 60, max_cycles = 0): WireDraft => ({ message, idle_secs, max_cycles })

type Answer = { ok: boolean, status: number, json: () => Promise<unknown> }
const answer = (status: number, body: unknown): Answer => ({ ok: status < 300, status, json: () => Promise.resolve(body) })

/** The gateway's goal-draft rules, held in `row`: a PUT on the stored stamp
 *  lands with a newer stamp, a PUT holding the stored content answers the record
 *  unchanged, and any other PUT is the 409 conflict. A loop create naming an
 *  older version, over different stored text, is refused with the same 409.
 *  `intercept` may answer one request itself; return `undefined` to fall
 *  through to these rules. */
export interface FakeGateway {
  row: WireRecord
  puts: Array<{ base_updated_at: number, draft: WireDraft | null }>
  creates: Array<Record<string, unknown>>
  deletes: string[]
  intercept?: (url: string, init: RequestInit | undefined, gateway: FakeGateway) => Promise<unknown> | undefined
  /** Apply one PUT body to `row` the way the store does. */
  apply: (body: { base_updated_at: number, draft: WireDraft | null }) => Answer
}

const sameWire = (left: WireDraft | null, right: WireDraft | null) => (
  left === null || right === null ? left === right : JSON.stringify(left) === JSON.stringify(right)
)

export function stubGoalGateway(row: WireRecord = { draft: null, updated_at: 0 }): FakeGateway {
  let clock = 1_000
  const gateway: FakeGateway = {
    row,
    puts: [],
    creates: [],
    deletes: [],
    apply: body => {
      if (body.base_updated_at === gateway.row.updated_at) {
        clock = Math.max(clock + 1, gateway.row.updated_at + 1)
        gateway.row = { draft: body.draft, updated_at: clock }
        return answer(200, gateway.row)
      }
      if (sameWire(body.draft, gateway.row.draft) && gateway.row.updated_at > 0) return answer(200, gateway.row)
      return answer(409, { code: 'goal_draft_conflict', error: 'goal draft changed since it was loaded', current: gateway.row })
    },
  }
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    const path = String(url)
    const intercepted = gateway.intercept?.(path, init, gateway)
    if (intercepted) return intercepted
    if (path.startsWith('/api/crons')) return Promise.resolve(answer(200, { jobs: [] }))
    if (path.startsWith('/api/autonudge/draft/slot/')) {
      if (init?.method !== 'PUT') return Promise.resolve(answer(200, gateway.row))
      const body = JSON.parse(String(init.body)) as { base_updated_at: number, draft: WireDraft | null }
      gateway.puts.push(body)
      return Promise.resolve(gateway.apply(body))
    }
    if (init?.method === 'DELETE') {
      gateway.deletes.push(path)
      return Promise.resolve(answer(204, {}))
    }
    if (path === '/api/autonudge' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      gateway.creates.push(body)
      const version = body.draft_updated_at
      const shown = { message: body.message, idle_secs: body.idle_secs, max_cycles: body.max_cycles } as WireDraft
      if (typeof version === 'number' && gateway.row.updated_at > version && !sameWire(gateway.row.draft, shown)) {
        return Promise.resolve(answer(409, { code: 'goal_draft_conflict', error: 'goal draft changed since it was loaded', current: gateway.row }))
      }
      return Promise.resolve(answer(200, { ok: true, loop: makeLoop({ id: 'l-new', message: String(body.message), next_due_ts: 0, cycle_count: 0 }) }))
    }
    return Promise.resolve(answer(200, { ok: true, loop: null }))
  }) as unknown as typeof fetch)
  return gateway
}

/** A promise the test settles by hand, for holding one request open. */
export function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(settle => { resolve = settle })
  return { promise, resolve }
}

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

export function renderStatefulPopover(loop: AutoNudgeLoop | null, slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Harness() {
    const [currentLoop, setCurrentLoop] = useState<AutoNudgeLoop | null>(loop)
    const [open, setOpen] = useState(true)
    return (
      <>
        <output data-testid="popover-open">{String(open)}</output>
        <button type="button" data-testid="reopen-popover" onClick={() => setOpen(true)}>reopen</button>
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

export const goalBox = () => (
  document.querySelector('textarea[placeholder^="Describe what you want"]') as HTMLTextAreaElement
)
