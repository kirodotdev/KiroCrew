import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

/** The inspector's contract is not what it SHOWS -- it is what it costs when it is
 *  off. It sits on the transcript's hottest paths (a per-frame correction loop, a
 *  per-render counter, every scroll burst), so "disabled" has to mean no element,
 *  no timer, and nothing retained -- not merely "cheap". Each test here fails if
 *  the gate is moved below any of those.
 *
 *  Loaded fresh per test: the module reads the flag ONCE at import and keeps its
 *  state in module scope, so a shared instance would let one test's toggle decide
 *  the next test's answer. */
const load = async () => await import('../dev/scrollInspector')

const overlay = () => document.querySelector('[data-scroll-inspector]')

describe('scroll inspector: disabled costs nothing', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    document.body.replaceChildren()
  })

  it('creates no element and arms no timer while off', async () => {
    const setInterval = vi.spyOn(window, 'setInterval')
    const insp = await load()
    expect(insp.inspectorOn()).toBe(false)

    insp.devLog('TAG', 'detail')
    insp.devWatchScroller(document.createElement('div'), 12)
    insp.devWatchMessages(100, 7000)

    expect(overlay()).toBeNull()
    expect(setInterval).not.toHaveBeenCalled()
  })

  it('retains nothing logged while off, so enabling later shows no backlog', async () => {
    const insp = await load()
    insp.devLog('BEFORE', 'x')
    insp.setInspectorEnabled(true)
    // Only the reading that arrives AFTER enabling may appear. A buffer that
    // filled while disabled would both cost memory and mislead: those lines
    // carry timestamps from a window nobody was watching.
    insp.devLog('AFTER', 'y')
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('AFTER')
    expect(text).not.toContain('BEFORE')
  })

  it('reads the persisted flag at load, so a reload keeps it on', async () => {
    localStorage.setItem('mc-scroll-inspector', '1')
    const insp = await load()
    expect(insp.inspectorOn()).toBe(true)
  })

  it('survives storage being unavailable instead of taking the app down', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('private mode')
    })
    const insp = await load()
    // An inspector nobody can turn on is the safe answer on a path the product
    // depends on; a throw here would break every chat that imports it.
    expect(insp.inspectorOn()).toBe(false)
    getItem.mockRestore()
  })
})

describe('scroll inspector: enabling and disabling', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    document.body.replaceChildren()
  })
  afterEach(() => {
    document.body.replaceChildren()
  })

  it('paints a line only once there is something to show', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    // Enabling alone must not hang an empty box over the page: a surface that
    // reports nothing should stay invisible.
    expect(overlay()).toBeNull()
    insp.devLog('RESTORE.OK', 'idx=4')
    expect(overlay()?.textContent).toContain('RESTORE.OK')
  })

  it('removes the element and clears the timer when switched off', async () => {
    const clearInterval = vi.spyOn(window, 'clearInterval')
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    insp.devWatchScroller(document.createElement('div'), 3)
    expect(overlay()).not.toBeNull()

    insp.setInspectorEnabled(false)
    expect(overlay()).toBeNull()
    expect(clearInterval).toHaveBeenCalled()
    expect(insp.inspectorOn()).toBe(false)
  })

  it('picks up the panel toggle event without a reload', async () => {
    const insp = await load()
    expect(insp.inspectorOn()).toBe(false)
    window.dispatchEvent(new CustomEvent('mc-scroll-inspector-changed', { detail: true }))
    expect(insp.inspectorOn()).toBe(true)
    window.dispatchEvent(new CustomEvent('mc-scroll-inspector-changed', { detail: false }))
    expect(insp.inspectorOn()).toBe(false)
  })

  it('follows another tab turning it off', async () => {
    localStorage.setItem('mc-scroll-inspector', '1')
    const insp = await load()
    expect(insp.inspectorOn()).toBe(true)
    localStorage.setItem('mc-scroll-inspector', '0')
    window.dispatchEvent(new StorageEvent('storage', { key: 'mc-scroll-inspector' }))
    expect(insp.inspectorOn()).toBe(false)
  })

  it('lets a tap through everywhere except the drag grip', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    const box = overlay() as HTMLElement
    // The box must never swallow a tap meant for the transcript it reports on;
    // the grip is the one place that opts back in so it can be dragged.
    expect(box.style.pointerEvents).toBe('none')
    const grip = box.firstElementChild as HTMLElement
    expect(grip.style.pointerEvents).toBe('auto')
  })

  it('restores a dragged position from storage', async () => {
    localStorage.setItem('mc-scroll-inspector-pos', JSON.stringify({ x: 120, y: 260 }))
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    const box = overlay() as HTMLElement
    expect(box.style.left).toBe('120px')
    expect(box.style.top).toBe('260px')
  })

  it('ignores a corrupt persisted position instead of vanishing off-screen', async () => {
    localStorage.setItem('mc-scroll-inspector-pos', '{not json')
    const insp = await load()
    insp.setInspectorEnabled(true)
    insp.devLog('X', 'y')
    const box = overlay() as HTMLElement
    expect(parseFloat(box.style.left)).toBeGreaterThanOrEqual(0)
    expect(parseFloat(box.style.top)).toBeGreaterThanOrEqual(0)
  })
})

/** The reader's complaint, as a number: movement the APP performed, per landing.
 *
 *  Not the change in scrollTop across a landing -- that conflates the finger with
 *  the machine. On the device a landing showed the position moving 11px while the
 *  app had written only 1px; the other 10px were the user's own scrolling, and a
 *  before/after reading would have reported it as ten times the real defect. */
/** The live-stats block renders only for a WATCHED scroller -- without one the
 *  overlay shows just the event log, which is how a first version of these tests
 *  reported the instrument broken when it was the test that was incomplete. */
const TICK_MS = 250

const watchAScroller = (insp: Awaited<ReturnType<typeof load>>) => {
  const el = document.createElement('div')
  Object.defineProperty(el, 'scrollHeight', { value: 41644, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: 595, configurable: true })
  el.scrollTop = 1481
  insp.devWatchScroller(el, 66)
  return el
}

describe('scroll inspector: programmatic movement per landing', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
    localStorage.clear()
    document.body.replaceChildren()
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.replaceChildren()
  })

  it('leads with the environment, and MEASURES the anchoring rather than asking', async () => {
    // The first line exists because every wrong conclusion in this area came from
    // reasoning about a platform instead of reading it. A capability split keyed to
    // `'overflowAnchor' in style` shipped and inverted on the one platform it was
    // for: WebKit has landed the property, so it answers yes on a device whose
    // anchoring does not hold a virtualized list.
    //
    // So the line must carry BOTH -- what the browser claims (`sa`) and what it
    // actually did when content was inserted above a parked scroll position
    // (`hold`) -- because their disagreement is the whole finding. A line that
    // reported only the property would re-tell the same lie on a bigger font.
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    // First LINE, not first character: the drag grip shares the host and carries no
    // newline of its own, so the readings start on the line the env text opens.
    expect(text.split('\n')[0]).toMatch(/env sa=/)
    expect(text.indexOf('env ')).toBeLessThan(text.indexOf('to-end'))
    expect(text).toMatch(/\bsa=(yes|no)\b/)
    expect(text).toMatch(/\bhold=/)
    // The discriminator, and the reason this assertion names a value instead of a
    // shape. This harness IS the failure case in miniature: jsdom exposes the CSS
    // property but performs no anchoring, so `sa=yes hold=no` -- the same
    // disagreement the device produces. A probe that merely copied the property
    // would report `hold=yes` here and pass every structural check, which is how
    // the withdrawn capability split got shipped in the first place.
    expect(text).toMatch(/\bhold=no\b/)
    // HARNESS GAP, recorded rather than papered over: this cannot tell a probe that
    // measured no movement from one that never inserted anything, because jsdom
    // answers `no` either way. Removing the insertion leaves every test here green.
    // Closing it needs an engine that actually anchors, i.e. a real browser -- so
    // the positive half of this probe has only ever been read off a device.
    // The bundle, so a hot-swapped dist can be told apart on the screen -- readings
    // were taken against the wrong build more than once with no way to notice.
    expect(text).toMatch(/\bb=/)
  })

  it('reads the movement out of the write log the virtualizer already emits', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    // Exactly the shape useVirtualChat logs: `<who> <from>-><to>`.
    insp.devLog('WRITE', 'abovefold 100->500')
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('moved 400px')
    expect(text).toContain('abovefold')
  })

  it('sums opposite writes as two jolts, because a net figure would call them zero', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devLog('WRITE', 'abovefold 100->500')
    insp.devLog('WRITE', 'forcepin 500->100')
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    // 400 out and 400 back is the WORST thing the reader can experience, and the
    // one a signed total reports as perfect. This assertion is the whole point.
    expect(text).toContain('moved 800px')
    expect(text).not.toContain('moved 0px')
    expect(text).toContain('2 write(s)')
  })

  it('starts a fresh count at each landing, so the figure is per page not per session', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devLog('WRITE', 'abovefold 0->900')
    // The LANDING is the reset point, not the fetch starting: with several
    // landings per scroll those are different moments, and resetting on the
    // earlier one made the figure span two landings.
    insp.devWatchMessages(100, 900)
    insp.devWatchMessages(200, 900)
    insp.devLog('WRITE', 'abovefold 900->910')
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('moved 10px')
    expect(text).not.toContain('moved 910px')
  })

  it('keeps the worst single write, which is what a lurch looks like', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    // The lurch is in the MIDDLE, deliberately. With it first, a field that only
    // ever keeps the FIRST write still reads correctly; with it last, one that
    // keeps only the LAST does. Both of those mutations survived an earlier
    // version of this test, and only a middle peak reddens both.
    insp.devLog('WRITE', 'abovefold 0->2')
    insp.devLog('WRITE', 'abovefold 2->9411')
    insp.devLog('WRITE', 'abovefold 9411->9413')
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('worst=9409px')
    expect(text).not.toContain('worst=2px')
  })

  it('ignores a write that moved nothing, so a no-op does not inflate the count', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devLog('WRITE', 'abovefold 3615->3615')
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('moved')
  })
})


/** A reprice above the reader is the one displacement nobody compensates. The
 *  spacer moves for two reasons and only one is a defect, so the instrument has
 *  to separate them or it reports every scroll as a bug. */
describe('scroll inspector: reprice above the reader', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
    localStorage.clear()
    document.body.replaceChildren()
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.replaceChildren()
  })

  it('counts a spacer change at a STILL window, which is a reprice', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devSpacer(7488, 40)
    insp.devSpacer(109, 40)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('repriced 7379px')
  })

  it('ignores a spacer change caused by the window MOVING, which is the reader', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    // The reader scrolled: start moved, so the spacer difference is expected.
    insp.devSpacer(7488, 40)
    insp.devSpacer(109, 31)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('repriced')
  })

  it('keeps the worst single reprice, with the peak in the middle', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devSpacer(1000, 40)
    insp.devSpacer(1002, 40)
    insp.devSpacer(8500, 40)
    insp.devSpacer(8502, 40)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('worst=7498px')
    expect(text).not.toContain('worst=2px')
  })

  it('starts fresh at each landing so the figure is per page', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devSpacer(1000, 40)
    insp.devSpacer(9000, 40)
    insp.devWatchMessages(100, 900)
    insp.devWatchMessages(200, 900)
    insp.devSpacer(9000, 40)
    insp.devSpacer(9007, 40)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('repriced 7px')
    expect(text).not.toContain('repriced 8007px')
  })
})


/** Movement no logged write explains. The instrument's whole value is telling
 *  three things apart, so each is pinned: our own write, the reader's finger,
 *  and a jump from somewhere else. */
describe('scroll inspector: unowned scroll movement', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
    localStorage.clear()
    document.body.replaceChildren()
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.replaceChildren()
  })

  it('reports a kilopixel jump that no write accounts for', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devScrollTop(14432)
    insp.devScrollTop(1361)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').toContain('UNOWNED 13071px')
  })

  it('stays silent for our OWN write, matched by its target', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devScrollTop(1361)
    insp.devLog('WRITE', 'resize 1361->14563')
    insp.devScrollTop(14563)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('UNOWNED')
  })

  it('COUNTS a write the engine truncated, which is the movement being hunted', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devScrollTop(14432)
    // We asked for 14563 but the engine clamped to 1361: the position did NOT
    // land on our target, so it is not ours to excuse.
    insp.devLog('WRITE', 'resize 14432->14563')
    insp.devScrollTop(1361)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').toContain('UNOWNED')
  })

  it('stays silent for ordinary scrolling, however fast', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    // Momentum-sized steps on a ~600px viewport, well under the threshold.
    for (const t of [1000, 1400, 1900, 2500, 3200]) insp.devScrollTop(t)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('UNOWNED')
  })

  it('calls a landing ON the range limit a CLAMP, not a jump', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devScrollTop(14432, 60000)
    // Content became shorter: the limit is now 1361 and the engine pulls the
    // position down to exactly it. Landing ON the limit is the signature.
    insp.devScrollTop(1361, 1361)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('clamp=1')
    expect(text).not.toContain('jump')
  })

  it('calls a landing away from the limit a JUMP, which needs the opposite fix', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devScrollTop(14432, 60000)
    insp.devScrollTop(1361, 60000)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('jump')
    expect(text).not.toContain('clamp')
  })

  it('starts fresh at each landing', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devScrollTop(20000)
    insp.devScrollTop(1000)
    insp.devWatchMessages(100, 900)
    insp.devWatchMessages(200, 900)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('UNOWNED')
  })
})


/** The landing window -- the one the other two counters exclude by design. */
describe('scroll inspector: top spacer across a landing', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
    localStorage.clear()
    document.body.replaceChildren()
  })
  afterEach(() => {
    vi.useRealTimers()
    document.body.replaceChildren()
  })

  it('shows the spacer collapsing even though the window START moved', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devWatchMessages(201, 10350)
    insp.devSpacer(12000, 23)
    // The landing: 100 messages arrive and START is re-based, which is exactly
    // the case `repriced` skips.
    insp.devWatchMessages(301, 10350)
    insp.devSpacer(3845, 123)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('spacer 12000->3845')
    expect(text).toContain('-8155px')
  })

  it('keeps every live line inside the width the device actually shows', async () => {
    // Measured, not guessed. On a 440px-wide phone the overlay clipped
    // `RESIDUAL 0px of 8616px owed (worst 0px in 1 corr si` at ~50 characters, and
    // the words it cut were `since load` -- the scope label whose whole job is to
    // stop a misreading. A number that does not fit is not reported.
    //
    // The box also deliberately does not wrap (see ensureHost), so an over-long line
    // is silently truncated rather than folded: nothing on screen says it happened.
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devWatchMessages(200, 2807)
    insp.devSpacer(0, 0)
    insp.devWatchMessages(300, 2807)
    insp.devSpacer(7037, 0)
    insp.devLog('WRITE', 'resize 0->8616')
    insp.devLog('CORR', 'd=8616 owed=8616 res=0 painted=0')
    vi.advanceTimersByTime(TICK_MS)
    const liveText =
      document.querySelector('[data-scroll-inspector-live]')?.textContent ?? ''
    // Proven non-empty FIRST. A selector that stops matching makes this an assertion
    // over zero lines, which passes forever and reports nothing -- the emptiest kind
    // of green.
    expect(liveText).toContain('RESIDUAL')
    const tooWide = liveText.split('\n').filter((l) => l.length > 50)
    expect(tooWide).toEqual([])
  })

  it('reports the RESIDUAL from the corrector, not from the spacer', async () => {
    // Rewritten against a device frame that caught the old source lying. The
    // spacer-derived version printed 1579px on a landing the corrector reported as
    // `res=0`, and the corrector was right: `spacer 0->6781` while `owed=8360`,
    // because once the window reaches the start prepended rows MOUNT above the
    // anchor rather than growing the spacer, so the spacer undercounts by exactly
    // the mounted growth and the subtraction inherits it as fake residual. Two
    // residuals that disagree is worse than one, so the honest one wins the line.
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devWatchMessages(100, 2807)
    insp.devSpacer(0, 0)
    insp.devWatchMessages(200, 2807)
    insp.devSpacer(6781, 0)
    insp.devLog('WRITE', 'resize 0->8360')
    insp.devLog('CORR', 'd=8360 owed=8360 res=0 painted=0')
    // The device emitted a SECOND correction on the same landing, asking for
    // nothing: `CORR d=0 owed=0 res=0`. It must not count as a landing -- counting
    // it dilutes the run and makes a real residual look rarer than it is.
    insp.devLog('CORR', 'd=0 owed=0 res=0 painted=0')
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('RESIDUAL 0px of 8360px owed')
    expect(text).toContain('in 1 corr since load')
    // The scope has to be ON the line. `moved` directly above it resets at each
    // landing while these run figures do not, and a device reading showed the
    // mismatch reading as "two corrections on this landing" when one of them
    // predated it. Two adjacent numbers on different windows is how an earlier
    // residual became unreadable.
    expect(text).toContain('since load')
    // The spacer keeps only the job it can do -- describing the spacer.
    expect(text).toContain('spacer 0->6781')
    // And the raw figure is still shown, because it is what the writes did.
    expect(text).toContain('moved 8360px')
    // The number the old source would have produced must not appear anywhere.
    expect(text).not.toContain('1579')
  })

  it('keeps the WORST residual, with the peak in the middle', async () => {
    // A single bad landing inside a run of good ones is the entire complaint, and
    // a last-only reading hides it behind the next landing. Peak in the MIDDLE on
    // purpose: peak-last survives a "keeps only the last" bug and peak-first
    // survives "keeps only the first", so neither placement can prove a max.
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devLog('CORR', 'd=1000 owed=999 res=1 painted=0')
    insp.devLog('CORR', 'd=1000 owed=600 res=400 painted=1')
    insp.devLog('CORR', 'd=1000 owed=998 res=2 painted=0')
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('RESIDUAL 2px of 998px owed')
    expect(text).toContain('worst 400px')
    expect(text).toContain('in 3 corr since load')
  })

  it('opens its own window for a CORRECTION that arrives with no landing', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    // No message count change at all -- a restore, a regroup or a turn-end
    // rebuild reaches the correction without one. The spacer as it stands now is
    // the baseline.
    insp.devSpacer(4000, 12)
    insp.devLog('WRITE', 'resize 1613->11199')
    insp.devSpacer(13600, 12)
    vi.advanceTimersByTime(TICK_MS)
    const text = overlay()?.textContent ?? ''
    expect(text).toContain('spacer 4000->13600')
  })

  it('is armed by the anchor correction only, not by every writer', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devSpacer(4000, 12)
    // The above-fold reprice is a RESIDUAL corrector, a few px at a time, and it
    // fires while the reader scrolls. Letting it open the window would restart the
    // measurement mid-flight and hide the correction it was opened to measure.
    insp.devLog('WRITE', 'abovefold 1613->1609')
    insp.devSpacer(13600, 12)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('spacer 4000->13600')
  })

  it('does not arm on ordinary scrolling, only on the count rising', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devWatchMessages(301, 10350)
    insp.devSpacer(12000, 23)
    // Same count, window moves as the reader scrolls: not a landing.
    insp.devWatchMessages(301, 10350)
    insp.devSpacer(3845, 12)
    vi.advanceTimersByTime(TICK_MS)
    expect(overlay()?.textContent ?? '').not.toContain('spacer 12000->')
  })

  it('closes the window after a few renders so scrolling cannot drift into it', async () => {
    const insp = await load()
    insp.setInspectorEnabled(true)
    watchAScroller(insp)
    insp.devWatchMessages(201, 10350)
    insp.devSpacer(12000, 23)
    insp.devWatchMessages(301, 10350)
    for (const [px, st] of [[3845, 123], [3900, 124], [3950, 125], [4000, 126], [4050, 127], [4100, 128], [99999, 200]] as [number, number][]) {
      insp.devSpacer(px, st)
    }
    vi.advanceTimersByTime(TICK_MS)
    // The 7th sample is past the cap, so the runaway value never lands.
    expect(overlay()?.textContent ?? '').not.toContain('99999')
  })
})

describe('scroll inspector: reading helpers', () => {  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
  })

  it('keyShape shows the prefix, which is the part that identifies the vocabulary', async () => {
    const { keyShape } = await load()
    // The persisted anchor is a stable row id (`a-` prefix); the per-render key
    // is not. Printing only the tail hid exactly that difference and cost a
    // round of wrong diagnosis.
    expect(keyShape('a-abc123def456')).toBe('a-\u2026def456')
    expect(keyShape('turn-abc123def456')).toBe('tu\u2026def456')
  })

  it('shortId keeps two sessions distinguishable and tolerates absence', async () => {
    const { shortId } = await load()
    expect(shortId('chat-17-1788561823590')).toBe('3590')
    expect(shortId('abc')).toBe('abc')
    expect(shortId(null)).toBe('-')
    expect(shortId(undefined)).toBe('-')
  })
})
