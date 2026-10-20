/**
 * dashboardTiming — the Dashboard-tab load stepper's timing contract
 * (issue #20952).
 *
 * The stepper emits one `console.debug` line per step shaped
 * `[dashboard-timing] <label> <name> +<dt>ms (t=<sinceStart>ms)`, where `+dt`
 * is the delta since the PREVIOUS step and `t=` is the cumulative time since
 * the load began. The two numbers are distinct once more than one step runs:
 * the per-step delta must not collapse onto the cumulative total.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { startDashboardLoadTiming } from './dashboardTiming'

describe('startDashboardLoadTiming', () => {
  let nowSpy: ReturnType<typeof vi.spyOn>
  let debugSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // Drive performance.now() deterministically: 0 at start, then 100, 250.
    const stamps = [0, 100, 250]
    let i = 0
    nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => stamps[Math.min(i++, stamps.length - 1)])
    debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
  })

  afterEach(() => {
    nowSpy.mockRestore()
    debugSpy.mockRestore()
  })

  it('measures the per-step delta from the previous step, not from load start', () => {
    const timing = startDashboardLoadTiming('slug')
    timing.step('step1-get') // now=100: dt from t0=0 -> +100ms (t=100ms)
    timing.step('step2-sandbox-post') // now=250: dt from 100 -> +150ms (t=250ms)

    const lines = debugSpy.mock.calls.map((c) => String(c[0]))
    expect(lines[0]).toContain('step1-get +100ms (t=100ms)')
    // The second step's +Nms is the gap since step 1 (150), NOT the cumulative
    // 250ms — this is the delta-vs-cumulative distinction the stepper must keep.
    expect(lines[1]).toContain('step2-sandbox-post +150ms (t=250ms)')
  })
})
