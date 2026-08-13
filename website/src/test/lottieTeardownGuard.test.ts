/**
 * Guards for the lottie-web / happy-dom teardown interaction.
 *
 * lottie-web's prebuilt player bundles register a module-scoped
 * `setInterval(checkReady, 100)` purely by being imported. The interval belongs
 * to no AnimationItem, so no component cleanup can clear it; it self-clears only
 * on its first tick ~100ms after import, and only while `document` still exists.
 * A vitest worker that tears happy-dom down inside that window leaves the tick
 * to fire with no `document`, and the resulting
 * `ReferenceError: document is not defined` is counted by vitest as an unhandled
 * error -- the job goes red with 0 failing tests.
 *
 * `happyDOM.abort()` cannot close this: vitest's happy-dom environment does not
 * install happy-dom's task-managed timers on `globalThis` (the timer keys are
 * absent from its copy list), so such intervals are raw Node `Timeout`s that
 * happy-dom's async task manager never sees.
 *
 * Two layers defend against the failure, both wired in integration/setup.ts;
 * these tests pin each layer independently.
 *   1. `vi.mock` for both runtime specifiers, so the real bundle (and its
 *      import-time interval) never loads under test.
 *   2. A tracking wrapper on the global timer functions plus a sweep at file
 *      teardown, cancelling any still-pending Node timer before the
 *      environment goes away.
 *
 * NOTE: the last test arms teardown mode (late registrations get cancelled),
 * so it must stay LAST in this file.
 */
import { describe, it, expect, vi } from 'vitest'
import { setTimeout as sleep } from 'node:timers/promises'
import { clearLeakedTimers, beginTimerTeardown } from '../../integration/setup'

describe('lottie-web import-time timer neutralization', () => {
  it('serves the setup mock for BOTH runtime specifiers, so the real bundle never registers its interval', async () => {
    // Each specifier is a separate module with its own module-scoped interval,
    // so covering only one would leave the race open through the other.
    const full = (await import('lottie-web')).default
    const light = (await import('lottie-web/build/player/lottie_light')).default
    expect(vi.isMockFunction(full.loadAnimation)).toBe(true)
    expect(vi.isMockFunction(light.loadAnimation)).toBe(true)
  })

  it('sweeps a pending module-scoped interval before it can fire into a torn-down document', async () => {
    // Same shape as lottie's readyStateCheckInterval: an interval that
    // dereferences `document` on each tick. No await between registration and
    // the sweep, so the tick cannot run first -- the assertion is deterministic.
    const tick = vi.fn(() => document.readyState)
    setInterval(tick, 100)
    expect(clearLeakedTimers()).toBeGreaterThan(0)

    // Wait past the interval period on a raw Node timer imported directly from
    // node:timers/promises (independent of the wrapped globals). If the sweep
    // failed, the tick fires within 100ms and this catches it.
    await sleep(250)
    expect(tick).not.toHaveBeenCalled()
  })

  it('sweeps a pending setImmediate as well', async () => {
    const soon = vi.fn(() => document.readyState)
    setImmediate(soon)
    clearLeakedTimers()
    await sleep(20)
    expect(soon).not.toHaveBeenCalled()
  })

  it('clearTimeout/clearInterval through the wrappers stay functional', async () => {
    const late = vi.fn()
    const handle = setTimeout(late, 100)
    clearTimeout(handle)
    await sleep(150)
    expect(late).not.toHaveBeenCalled()
  })

  it('a fired one-shot self-evicts from the ledger (the Set tracks live timers, not total creations)', async () => {
    clearLeakedTimers() // drain anything earlier tests or React left pending
    const ran = vi.fn()
    setTimeout(ran, 1)
    await sleep(30)
    expect(ran).toHaveBeenCalledTimes(1)
    // The fired handle must be gone from the ledger; only timers created by
    // OTHER code between the drain and here could remain, and none were.
    expect(clearLeakedTimers()).toBe(0)
  })

  it('setup.ts wires the sweep into an afterAll (structural pin)', async () => {
    // The behavioral tests above exercise the sweep directly; this pins that it
    // is actually REGISTERED to run at file teardown. A refactor that keeps the
    // function but drops the hook re-opens the race silently -- the leak only
    // fires under specific worker/file-count timing.
    const { readFile } = await import('node:fs/promises')
    const { resolve } = await import('node:path')
    // Resolved from cwd (vitest runs with cwd at the website root, where
    // vite.config.ts lives). import.meta.url is NOT usable here: under this
    // suite's happy-dom environment it carries the environment's http URL,
    // not a file: URL, so fileURLToPath throws.
    const source = await readFile(resolve(process.cwd(), 'integration/setup.ts'), 'utf8')
    expect(source).toMatch(/afterAll\([\s\S]{0,120}?beginTimerTeardown\(/)
  })

  it('after teardown begins, a late registration is cancelled on the spot (MUST BE LAST)', async () => {
    beginTimerTeardown()
    const straggler = vi.fn(() => document.readyState)
    setTimeout(straggler, 1)
    setInterval(straggler, 10)
    await sleep(80)
    expect(straggler).not.toHaveBeenCalled()
    // The file's own afterAll calls beginTimerTeardown() again -- idempotent.
  })
})
