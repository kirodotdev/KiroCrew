import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import MessageErrorBoundary, { isStaleDomError } from '../components/MessageErrorBoundary'

/**
 * A stale-DOM reconciliation failure must not latch a SETTLED message into the
 * fallback.
 *
 * React throws
 *   NotFoundError: Failed to execute 'insertBefore' on 'Node': The node before
 *   which the new node is to be inserted is not a child of this node.
 * from `commitPlacement` when the DOM a fiber points at is no longer shaped the
 * way the fiber says. The markdown is fine — it renders correctly from a fresh
 * mount — so the boundary remounts the subtree ONCE rather than showing a
 * fallback that, for a message the user merely opened, survives until reload
 * (`componentDidUpdate` only clears on a CONTENT change, which a settled message
 * never gets).
 *
 * The retry is driven through the boundary's own error path with a component that
 * throws during render. A real commit-phase fault cannot be staged here: React 18
 * leaves the root in an unusable state after one ("Should not already be
 * working."), which breaks every later assertion in the file rather than testing
 * anything. What matters for the fix is the DECISION (which errors earn a
 * remount, and that it is spent only once), and that is exercised faithfully.
 */
const INSERT_BEFORE =
  "Failed to execute 'insertBefore' on 'Node': The node before which the new node is to be inserted is not a child of this node."

function staleDomError() {
  const e = new Error(INSERT_BEFORE)
  e.name = 'NotFoundError'
  return e
}

/** Throws on its first `failures` renders, then renders `label`. Counts every
 *  invocation so a retry is observable. */
function Flaky({ id, failures, label, error }: { id: string; failures: number; label: string; error: () => Error }) {
  const seen = (Flaky.mounts[id] = (Flaky.mounts[id] ?? 0) + 1)
  if (seen <= failures) throw error()
  return <span>{label}</span>
}
Flaky.mounts = {} as Record<string, number>

afterEach(() => {
  Flaky.mounts = {}
  vi.restoreAllMocks()
})

const quiet = () => vi.spyOn(console, 'error').mockImplementation(() => {})

describe('isStaleDomError', () => {
  it('recognizes the insertBefore and removeChild reconciliation failures', () => {
    expect(isStaleDomError(staleDomError())).toBe(true)
    expect(isStaleDomError(new Error("Failed to execute 'removeChild' on 'Node': The node to be removed is not a child of this node."))).toBe(true)
  })

  it('does not claim ordinary render errors', () => {
    expect(isStaleDomError(new TypeError('x is not a function'))).toBe(false)
    expect(isStaleDomError(new Error('Minified React error #290'))).toBe(false)
  })
})

describe('MessageErrorBoundary retry decision', () => {
  /** Exercises the lifecycle contract directly. React's DEV build re-runs a
   *  failed render, so a component that throws once recovers on its own and a
   *  rendered test cannot tell the fix apart from that replay. The decision — who
   *  earns a remount, and that it is spent once — is what the fix adds, so it is
   *  asserted where it is made. */
  const boundary = (rawContent: string) => {
    const inst = new MessageErrorBoundary({ children: null, rawContent })
    type Patch = Partial<{ error: Error | null; showRaw: boolean; attempt: number; retriedFor: string | null }>
    const updates: Patch[] = []
    // `setState` takes a patch OR an updater; resolve an updater against the live
    // state, so what is captured is what React would apply.
    inst.setState = ((u: Patch | ((s: typeof inst.state) => Patch)) => {
      updates.push(typeof u === 'function' ? u(inst.state) : u)
    }) as unknown as typeof inst.setState
    return { inst, updates }
  }

  it('schedules a remount for a stale-DOM error', () => {
    quiet()
    const { inst, updates } = boundary('settled')
    inst.componentDidCatch(staleDomError())
    expect(updates).toHaveLength(1)
    expect(updates[0]).toMatchObject({ error: null, attempt: 1, retriedFor: 'settled' })
  })

  it('schedules nothing for an ordinary render error', () => {
    quiet()
    const { inst, updates } = boundary('settled')
    inst.componentDidCatch(new TypeError('x is not a function'))
    expect(updates).toHaveLength(0)
  })

  it('does not schedule a second remount for the same content', () => {
    quiet()
    const { inst, updates } = boundary('settled')
    inst.state = { ...inst.state, retriedFor: 'settled' }
    inst.componentDidCatch(staleDomError())
    expect(updates).toHaveLength(0)
  })
})

describe('MessageErrorBoundary stale-DOM recovery', () => {
  it('shows the fallback for an ordinary render error', () => {
    quiet()
    render(
      <MessageErrorBoundary rawContent="settled">
        <Flaky id="a" failures={99} label="settled" error={() => new TypeError('boom')} />
      </MessageErrorBoundary>,
    )
    expect(screen.getByText(/failed to render/i)).toBeTruthy()
  })

  it('gives up after a single remount when the stale-DOM error repeats', () => {
    quiet()
    render(
      <MessageErrorBoundary rawContent="settled">
        <Flaky id="b" failures={99} label="settled" error={staleDomError} />
      </MessageErrorBoundary>,
    )
    // Bounded: the fallback appears rather than the boundary remounting forever.
    expect(screen.getByText(/failed to render/i)).toBeTruthy()
  })

  it('spends a fresh retry on new content while streaming', () => {
    quiet()
    const view = render(
      <MessageErrorBoundary rawContent="tick one">
        <Flaky id="c" failures={99} label="tick one" error={staleDomError} />
      </MessageErrorBoundary>,
    )
    expect(screen.getByText(/failed to render/i)).toBeTruthy()

    view.rerender(
      <MessageErrorBoundary rawContent="tick two">
        <Flaky id="d" failures={0} label="tick two" error={staleDomError} />
      </MessageErrorBoundary>,
    )
    expect(screen.getByText('tick two')).toBeTruthy()
    expect(screen.queryByText(/failed to render/i)).toBeNull()
  })

  it('keeps the raw-content hand-off available on the fallback', () => {
    quiet()
    render(
      <MessageErrorBoundary rawContent="# raw markdown">
        <Flaky id="e" failures={99} label="x" error={() => new TypeError('boom')} />
      </MessageErrorBoundary>,
    )
    expect(screen.getByText(/view raw/i)).toBeTruthy()
  })
})
