// @vitest-environment jsdom
/**
 * The `project-report` page's three HCI readings, driven rather than read.
 *
 * The page is a template the PYTHON package ships -- an inert body plus its own
 * script, which the gateway mints into a sandboxed frame. Its logic therefore has no
 * home in the backend suite, where no JS engine is reachable: the backend file beside
 * it pins what a regex can pin and says so. What is checked HERE is the behaviour --
 * the page's real script, in a real DOM, against the field bag the host hands it.
 *
 * Reading across to the package tree is this repo's own practice in the other
 * direction: `test_dashboard_frame.py` reads `widgetSrcdoc.ts` and
 * `CrewDynamicDashboard.tsx` to keep one policy in one place. Same reason both ways.
 *
 * Each case sets `window.kirocrew` the way the frame does, inserts the markup, and
 * runs the script. The markup goes in through `DOMParser`, whose parsed scripts stay
 * inert, so the page's script is lifted out and called -- which is also what makes a
 * case able to re-run it with a new bag.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const TEMPLATE = resolve(
  __dirname,
  '../../../src/kiro_crew/dashboard_templates/builtin/project-report/template.html',
)

const RAW = readFileSync(TEMPLATE, 'utf-8')
/** The page in two halves: what goes in the body, and what runs over it. */
const SCRIPT = (RAW.match(/<script\b[^>]*>([\s\S]*?)<\/script\b[^>]*>/i) || [])[1]
const MARKUP = RAW.replace(/<script\b[^>]*>[\s\S]*?<\/script\b[^>]*>/gi, '')

/** The instant every case reads its elapsed figures against. */
const NOW = Date.parse('2026-10-04T17:12:00Z')
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()

/** One task row of the `workstreams` fold, with only what a case cares about set. */
function task(over: Record<string, unknown> = {}) {
  return {
    item_id: 'it_1',
    title: 'the fold',
    state: 'open',
    status: 'progress',
    summary: '',
    verdict: null,
    pr: null,
    round: 1,
    spender: 'w1',
    credits: 1,
    credits_reported: true,
    duration_ms: 60_000,
    last_report_at: '',
    decision: '',
    events: [] as Array<Record<string, unknown>>,
    events_seen: 0,
    ...over,
  }
}

/** One board, with the rows a case hands it. */
function board(tasks: Array<Record<string, unknown>>, over: Record<string, unknown> = {}) {
  return {
    id: 'b1',
    goal: 'ship the report',
    round: 1,
    parent: null,
    total: tasks.length,
    accepted: 0,
    open: tasks.length,
    needs_you: 0,
    credits: 1,
    credits_reported: true,
    last_activity_at: iso(3),
    tasks,
    tasks_omitted: 0,
    series: [],
    ...over,
  }
}

/**
 * Mount the page with *fields*, and run its script.
 *
 * `agentic` defaults to the three the shipped manifest declares, because that is what
 * the host sends for this template and a case that forgot it would be testing a page
 * the gateway never serves.
 */
function mount(
  fields: Record<string, unknown>,
  extra: { written_at?: Record<string, string>; agentic?: string[]; seq?: number } = {},
) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields,
    agentic: extra.agentic ?? ['ci', 'for_you', 'verdict'],
    seq: extra.seq ?? 412,
    stale: false,
    missing: [],
    written_at: extra.written_at ?? {},
  }
  document.body.replaceChildren(
    ...new DOMParser().parseFromString(MARKUP, 'text/html').body.childNodes,
  )
  new Function(SCRIPT as string)()
}

/**
 * A REFRESH, which is not a second `mount`.
 *
 * `mount` runs the script in a fresh `new Function`, so every piece of module state
 * the page keeps -- which rows are expanded, which drawer is open, what each row
 * looked like last time -- starts empty. That is a page LOAD. What the host actually
 * does is replace the field bag and post a message, which the page's existing script
 * hears and re-renders from. Only this second thing can show that a refresh leaves
 * an expanded row expanded, because only this second thing has the state to lose.
 */
function refresh(fields: Record<string, unknown>, extra: { written_at?: Record<string, string> } = {}) {
  const api = (window as unknown as { kirocrew: Record<string, unknown> }).kirocrew
  api.fields = fields
  if (extra.written_at) api.written_at = extra.written_at
  window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew:fields' } }))
}

const text = (sel: string) => document.querySelector(sel)?.textContent?.trim() ?? ''
const all = (sel: string) => Array.from(document.querySelectorAll(sel))
/** The text a row shows ITSELF, without its nested marks concatenated onto it. */
const own = (node: Element | null | undefined) =>
  Array.from(node?.childNodes ?? [])
    .filter((n) => n.nodeType === 3)
    .map((n) => n.textContent ?? '')
    .join('')
    .trim()

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

describe('project-report: the page the gateway ships', () => {
  it('carries a script and a body, so the halves above are real', () => {
    // A guard on the harness itself: if the template stopped carrying a script, every
    // case below would mount an empty page and pass by drawing nothing.
    expect(SCRIPT, 'the template carries no script').toBeTruthy()
    expect(MARKUP).toContain('data-dashboard-field="items"')
  })

  // ----------------------------------------------------------------- change 1
  describe('the verdict line', () => {
    const live = {
      items: [board([task()])],
      omitted: 0,
      series: [],
      last_entry_at: iso(3),
      for_you: [],
      verdict: {
        state: 'needs_you',
        headline: 'PR #14982 is not green. 88 checks pass, 2 fail.',
        blocker: 'First Principles reads the RFC from main. A maintainer clears it.',
      },
    }

    it('draws the state, the headline and the one thing in the way', () => {
      mount(live, { written_at: { verdict: iso(1) } })
      expect(text('.pr-vd-state')).toBe('Needs you')
      expect(text('.pr-vd-head')).toContain('88 checks pass, 2 fail')
      expect(text('.pr-vd-block')).toContain('A maintainer clears it')
    })

    it('marks the line as the crewmate\'s own', () => {
      // A reader deciding whether to act on a judgment is entitled to know it is one.
      mount(live, { written_at: { verdict: iso(1) } })
      expect(text('.pr-vd-meta')).toContain('crewmate wrote this')
    })

    it.each([
      ['on_track', 'On track'],
      ['needs_you', 'Needs you'],
      ['blocked', 'Blocked'],
      ['no_word', 'No word yet'],
    ])('draws %s as %s', (state, label) => {
      mount({ ...live, verdict: { state, headline: 'x' } }, { written_at: { verdict: iso(1) } })
      expect(text('.pr-vd-state')).toBe(label)
    })

    it('falls back to no word yet when the fold advanced after the verdict', () => {
      // THE WHOLE POINT. A lead that died mid-run leaves a green verdict over a board
      // that has since moved, and shown as current it is a stale lie.
      mount(
        { ...live, last_entry_at: iso(3) },
        { written_at: { verdict: iso(40) } },
      )
      expect(text('.pr-vd-state')).toBe('No word yet')
      expect(text('.pr-vd-head')).toContain('has not posted since the log last advanced')
    })

    it('still names the lead\'s last word as a past reading', () => {
      // Downgraded, not hidden: "needs you, 2 fail" an hour ago is information, and a
      // page that silently drops it has thrown away the only word anyone wrote.
      mount({ ...live, last_entry_at: iso(3) }, { written_at: { verdict: iso(40) } })
      expect(text('.pr-vd-block')).toContain('88 checks pass, 2 fail')
      expect(text('.pr-vd-block')).toContain('past reading')
    })

    it('keeps the verdict when the fold has not advanced since it was written', () => {
      mount({ ...live, last_entry_at: iso(40) }, { written_at: { verdict: iso(3) } })
      expect(text('.pr-vd-state')).toBe('Needs you')
    })

    it('keeps the verdict when the host sent no write time', () => {
      // One unreadable stamp is a reason to leave the lead's word standing, not a
      // reason to silence it: silencing on missing data makes the fallback fire on
      // every host that has not been upgraded yet.
      mount(live, {})
      expect(text('.pr-vd-state')).toBe('Needs you')
    })

    it('draws nothing at all for a value that is not a verdict', () => {
      for (const bad of [null, 'needs_you', [], {}, { state: 'mystery' }]) {
        mount({ ...live, verdict: bad })
        expect(document.querySelector('.pr-vd'), String(bad)).toBeNull()
      }
    })
  })

  // ----------------------------------------------------------------- change 2
  describe('the red-lane strip and the PR chip', () => {
    const lanes = [
      { name: 'First Principles Review', state: 'fail', owner: 'maintainer', why: 'reads main' },
      { name: 'GPT 6.1 Review', state: 'fail', owner: 'yours', why: 'no redaction' },
      { name: 'Opus 5.5 Review', state: 'did_not_run', owner: 'yours' },
      { name: 'Backend tests', state: 'pass', owner: 'yours' },
    ]
    const withCi = {
      items: [board([task({ pr: '14982' })])],
      omitted: 0,
      series: [],
      last_entry_at: iso(3),
      for_you: [],
      ci: {
        pr: '14982',
        head: 'f44a4e56',
        checks: { it_1: { pass: 88, fail: 2, running: 0 } },
        lanes,
      },
    }

    it('lists only the lanes a reader has to do something about', () => {
      // There are ninety green lanes on a real board. A strip that listed them is a
      // strip nobody reads, which is the same as not having one.
      mount(withCi)
      const names = all('.pr-lane-n').map((n) => n.textContent)
      expect(names).toEqual([
        'First Principles Review',
        'GPT 6.1 Review',
        'Opus 5.5 Review',
      ])
      expect(names).not.toContain('Backend tests')
    })

    it('counts the reds and the did-not-runs separately in its header', () => {
      mount(withCi)
      expect(text('.pr-lanes-h')).toContain('2 red')
      expect(text('.pr-lanes-h')).toContain('1 did not run')
    })

    it('tags each red lane with who can clear it', () => {
      // `yours` is a lane the reader can act on and `maintainer` is one they can only
      // wait for. A strip that does not say which spends attention on a row that
      // cannot move.
      mount(withCi)
      const rows = all('.pr-lane').map((r) => r.textContent ?? '')
      expect(rows[0]).toContain('maintainer')
      expect(rows[1]).toContain('yours')
    })

    it('tags a lane that never started with the act it needs, not an owner', () => {
      // A lane that failed to launch is nobody's finding. It needs re-running, and
      // calling it someone's fault sends the reader to read a log that does not exist.
      mount(withCi)
      const never = all('.pr-lane').find((r) => (r.textContent ?? '').includes('Opus'))
      expect(never?.textContent).toContain('rerun it')
      expect(never?.textContent).toContain('never started')
    })

    it('tells a lane that never started apart from a failing one', () => {
      // The distinction this change exists for, asserted on the MARK rather than the
      // colour: the page is read on a light canvas and in a screenshot, where a hue
      // alone is not a word.
      mount(withCi)
      const marks = all('.pr-lane-m').map((m) => m.textContent)
      expect(marks).toEqual(['\u2717', '\u2717', '\u25cb'])
    })

    it('puts the failing count on the PR chip itself', () => {
      mount(withCi)
      const chip = all('.pr-chip').find((c) => (c.textContent ?? '').includes('PR 14982'))
      expect(chip?.textContent).toContain('\u2717 2')
      expect(chip?.className).toContain('pr-chip-fail')
    })

    it.each([
      [{ pass: 88, fail: 0, running: 0 }, '\u2713 88', 'pr-chip-pass'],
      [{ pass: 40, fail: 0, running: 13 }, '\u21bb 13', 'pr-chip-idle'],
      [{ pass: 86, fail: 3, running: 9 }, '\u2717 3', 'pr-chip-fail'],
    ])('reads %o as %s', (checks, mark, cls) => {
      // A failure outranks a run in flight: a board with both is red, and showing the
      // spinner would tell a reader to wait for news that has already arrived.
      mount({ ...withCi, ci: { ...withCi.ci, checks: { it_1: checks } } })
      const chip = all('.pr-chip').find((c) => (c.textContent ?? '').includes('PR 14982'))
      expect(chip?.textContent).toContain(mark)
      expect(chip?.className).toContain(cls)
    })

    it('leaves the chip bare when no board names that task', () => {
      // An absent build is not a passing one. A chip claiming green for a task the
      // worker never reported on would be the page inventing a reading.
      mount({ ...withCi, ci: { ...withCi.ci, checks: {} } })
      const chip = all('.pr-chip').find((c) => (c.textContent ?? '').includes('PR 14982'))
      expect(chip?.textContent?.trim()).toBe('PR 14982')
    })

    it('draws no strip when every lane is green', () => {
      mount({ ...withCi, ci: { ...withCi.ci, lanes: [lanes[3]] } })
      expect(document.querySelector('.pr-lanes')).toBeNull()
    })

    it('draws no strip for a value that is not a board', () => {
      for (const bad of [null, 'red', [], { lanes: 'no' }]) {
        mount({ ...withCi, ci: bad })
        expect(document.querySelector('.pr-lanes'), String(bad)).toBeNull()
      }
    })
  })

  // ----------------------------------------------------------------- change 3
  describe('idle time and the stale band', () => {
    const base = {
      omitted: 0,
      series: [],
      for_you: [],
      last_entry_at: iso(3),
    }
    const quietOf = () =>
      all('.pr-chip')
        .map((c) => c.textContent ?? '')
        .filter((t) => t.startsWith('quiet'))

    it('shows how long each in-flight row has been quiet', () => {
      mount({ ...base, items: [board([task({ last_report_at: iso(31) })])] })
      expect(quietOf()).toContain('quiet 31m')
    })

    it.each([
      [2, 'quiet 2m'],
      [31, 'quiet 31m'],
      [100, 'quiet 1h40m'],
    ])('reads %i minutes as %s', (mins, label) => {
      mount({ ...base, items: [board([task({ last_report_at: iso(mins) })])] })
      expect(quietOf()).toContain(label)
    })

    it('goes amber only past the threshold', () => {
      // A long silence is something to CHECK, not something known to be broken, so
      // the mark is amber and only once it is worth a reader's attention.
      //
      // Asserted as "which chips are amber", not "how many". One task is drawn in
      // more than one place -- the tree row, and again on the row that says what to
      // do about it -- and each place marks its own chip, which is right: a reader
      // scrolled to one of them must see the silence there. A count would make this
      // case fail the next time a section draws the same task, while saying nothing
      // about the threshold it is named for.
      mount({ ...base, items: [board([task({ last_report_at: iso(2) })])] })
      expect(all('.pr-chip-quiet'), 'a 2-minute silence was marked').toHaveLength(0)
      expect(quietOf(), 'the chip itself is drawn either way').toContain('quiet 2m')

      mount({ ...base, items: [board([task({ last_report_at: iso(31) })])] })
      const amber = all('.pr-chip-quiet')
      expect(amber.length, 'a 31-minute silence went unmarked').toBeGreaterThan(0)
      // EVERY amber chip is the long silence, which is the half a bare count drops:
      // a page that marked all its chips amber would satisfy "at least one".
      for (const chip of amber) {
        expect(chip.textContent).toBe('quiet 31m')
      }
    })

    it('says nothing about a task no worker has reported on', () => {
      // Absence of silence, not a long one: `quiet 0m` on a task nobody has picked up
      // reads as a worker that just spoke.
      mount({ ...base, items: [board([task({ last_report_at: '' })])] })
      expect(quietOf()).toHaveLength(0)
    })

    it('says nothing about a row that has finished', () => {
      // An accepted task stopped reporting because it was done. `quiet 3h` on it reads
      // as a problem where there is none.
      mount({
        ...base,
        items: [board([task({ state: 'accepted', last_report_at: iso(180) })])],
      })
      expect(quietOf()).toHaveLength(0)
    })

    it('says nothing when the stamp is in the future', () => {
      // The reader's clock can sit ahead of the gateway's, and `quiet -5m` is worse
      // than no chip at all.
      mount({ ...base, items: [board([task({ last_report_at: iso(-30) })])] })
      expect(quietOf()).toHaveLength(0)
    })

    it('raises a band when the fold itself has stopped advancing', () => {
      // The difference between "working" and "dead", which a page of intact numbers
      // cannot otherwise show.
      mount({ ...base, last_entry_at: iso(100), items: [board([task()])] })
      expect(text('.pr-stale')).toContain('No new entry for 1h40m')
      expect(text('.pr-stale')).toContain('last-known reading')
    })

    it('names the fold seq in the band, so the reading can be placed', () => {
      mount({ ...base, last_entry_at: iso(100), items: [board([task()])] }, { seq: 412 })
      expect(text('.pr-stale')).toContain('fold seq 412')
    })

    it('raises no band while the fold is advancing', () => {
      mount({ ...base, last_entry_at: iso(3), items: [board([task()])] })
      expect(document.querySelector('.pr-stale')).toBeNull()
    })

    it('raises no band when the fold has applied nothing at all', () => {
      // An empty fold is a page with no numbers, not a page of old ones, and the
      // empty state already says so in its own words.
      mount({ ...base, last_entry_at: '', items: [] })
      expect(document.querySelector('.pr-stale')).toBeNull()
    })
  })

  // The three bands are above the numbers they qualify, which is the only order that
  // works: a reader who learns the page is dead should learn it before reading a
  // number off it.
  it('puts the stale band, the verdict and the lanes above the view', () => {
    mount({
      items: [board([task({ pr: '1', last_report_at: iso(5) })])],
      omitted: 0,
      series: [],
      for_you: [],
      last_entry_at: iso(100),
      verdict: { state: 'blocked', headline: 'nothing has moved' },
      ci: { pr: '1', lanes: [{ name: 'L', state: 'fail', owner: 'yours' }], checks: {} },
    })
    const order = all('.pr-stale, .pr-vd, .pr-lanes, #pr-view').map((n) =>
      n.id === 'pr-view' ? 'view' : n.className,
    )
    expect(order).toEqual(['pr-stale', 'pr-vd', 'pr-lanes', 'view'])
  })
  // ----------------------------------------------------------------- change 4
  describe('the task drawer', () => {
    const base = { omitted: 0, series: [], for_you: [], last_entry_at: iso(3) }
    const open = (over: Record<string, unknown> = {}, fields: Record<string, unknown> = {}) => {
      mount({ ...base, items: [board([task(over)])], ...fields })
      const row = document.querySelector('.pr-n-task > .pr-n-h') as HTMLElement
      expect(row, 'no task row to click').toBeTruthy()
      row.click()
    }

    it('opens when a task row is clicked, titled with the task and its workstream', () => {
      open({ title: 'the fold' })
      expect(text('.pr-dr-t')).toBe('the fold')
      expect(text('.pr-dr-s')).toContain('ship the report')
    })

    it('shows the summary, the ruling and the verdict', () => {
      open({
        summary: 'pushed 495c102, waiting on CI',
        decision: 'rebase onto main and re-run',
        verdict: 'pass',
      })
      const body = text('.pr-dr-b')
      expect(body).toContain('pushed 495c102')
      expect(body).toContain('rebase onto main')
      expect(body).toContain('pass')
    })

    it.each([
      ['What the worker last said', 'has not reported on this task yet'],
      ['What the conductor decided', 'No ruling on this task'],
      ['Verdict', 'Not adjudicated'],
      ['Result / handoff', 'No output yet'],
    ])('%s says what would be here when it is empty', (label, sentence) => {
      // AN EMPTY BLOCK IS A SENTENCE, never blank space: a reader meeting a gap
      // cannot tell a task that produced nothing from a page that failed to load.
      open()
      const sec = all('.pr-dr-sec').find((s) => (s.textContent ?? '').startsWith(label))
      expect(sec, label).toBeTruthy()
      expect(sec?.textContent).toContain(sentence)
      expect(sec?.querySelector('.pr-dr-none'), label).toBeTruthy()
    })

    it('names the result when the task produced one', () => {
      open({ pr: '14982', state: 'accepted' })
      const sec = all('.pr-dr-sec').find((s) =>
        (s.textContent ?? '').startsWith('Result / handoff'))
      expect(sec?.textContent).toContain('14982')
      expect(sec?.querySelector('.pr-dr-none')).toBeNull()
    })

    it('lists the task\'s own events, newest first', () => {
      open({
        events_seen: 3,
        events: [
          { at: iso(2), kind: 'report', status: 'progress', text: 'pushed 495c102' },
          { at: iso(60), kind: 'decide', status: null, text: 'rebase it' },
          { at: iso(180), kind: 'create', status: null, text: 'created' },
        ],
      })
      expect(all('.pr-dr-ev .pr-dr-e-t').map(own))
        .toEqual(['pushed 495c102', 'rebase it', 'created'])
    })

    it('says how many events there were when it is carrying fewer', () => {
      // THE DISTINCTION THE FOLD'S COUNTER EXISTS FOR. The fold spends one bounded
      // ring across every task, so a task's older lines are dropped -- and a drawer
      // showing an empty list would say that task did nothing.
      open({
        events_seen: 34,
        events: [{ at: iso(2), kind: 'report', status: null, text: 'the newest line' }],
      })
      const sec = all('.pr-dr-sec').find((s) => (s.textContent ?? '').includes('Events'))
      expect(sec?.querySelector('.pr-dr-k')?.textContent).toContain('1 of 34')
    })

    it('tells a task that did nothing from one whose lines were dropped', () => {
      open({ events_seen: 0, events: [] })
      expect(text('.pr-dr-b')).toContain('No events recorded against this task yet')
      open({ events_seen: 14, events: [] })
      expect(text('.pr-dr-b')).toContain('no longer carrying their text')
    })

    it('shows only this task\'s red lanes, and says so when there are none', () => {
      open(
        { pr: '14982' },
        {
          ci: {
            pr: '14982',
            checks: {},
            lanes: [
              { name: 'First Principles Review', state: 'fail', owner: 'maintainer' },
              { name: 'Backend tests', state: 'pass', owner: 'yours' },
            ],
          },
        },
      )
      const sec = all('.pr-dr-sec').find((s) => (s.textContent ?? '').startsWith('Lanes'))
      expect(sec?.textContent).toContain('First Principles Review')
      expect(sec?.textContent).not.toContain('Backend tests')

      open({ pr: '99' }, { ci: { pr: '14982', checks: {}, lanes: [] } })
      const none = all('.pr-dr-sec').find((s) => (s.textContent ?? '').startsWith('Lanes'))
      expect(none?.textContent).toContain('No lane board for this task')
    })

    it('closes on its own control, on the backdrop and on Escape', () => {
      for (const shut of [
        () => (document.querySelector('.pr-dr-x') as HTMLElement).click(),
        () => (document.querySelector('.pr-dr-back') as HTMLElement).click(),
        () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })),
      ]) {
        open({ title: 'the fold' })
        expect(document.querySelector('.pr-dr')).toBeTruthy()
        shut()
        expect(document.querySelector('.pr-dr')).toBeNull()
      }
    })

    it('draws no drawer until a row is clicked', () => {
      mount({ ...base, items: [board([task()])] })
      expect(document.querySelector('.pr-dr')).toBeNull()
    })
  })

  // ----------------------------------------------------------------- change 5
  describe('the per-epic pipeline', () => {
    const rows = [
      task({ item_id: 'a', title: 'with a worker', state: 'open', spender: 'w1' }),
      task({ item_id: 'b', title: 'ruled', state: 'open', verdict: 'fail', spender: 'w2' }),
      task({ item_id: 'c', title: 'taken', state: 'accepted', verdict: 'pass', spender: 'w3' }),
      task({ item_id: 'd', title: 'also taken', state: 'accepted', verdict: 'pass', spender: 'w3' }),
    ]
    // The pipeline is a ONE-WORKSTREAM view, so a case has to select the pill.
    const one = () => {
      mount({
        items: [board(rows)],
        omitted: 0,
        series: [],
        for_you: [],
        last_entry_at: iso(3),
      })
      const pill = all('.pr-pill').find((b) => (b.textContent ?? '').includes('ship the report'))
      expect(pill, 'no workstream pill to select').toBeTruthy()
      ;(pill as HTMLElement).click()
    }

    it('is drawn for one workstream and not for all of them at once', () => {
      // Across every workstream the three stages would mix boards whose gates are
      // different conductors', and the bar would count a number nobody owns.
      mount({
        items: [board(rows)],
        omitted: 0,
        series: [],
        for_you: [],
        last_entry_at: iso(3),
      })
      expect(document.querySelector('.pr-pipe')).toBeNull()
      one()
      expect(document.querySelector('.pr-pipe')).toBeTruthy()
    })

    it('counts the accepted against the total', () => {
      one()
      expect(text('.pr-pipe-h')).toContain('2 / 4 accepted')
    })

    it('puts each task in the furthest stage it reached, and never in two', () => {
      // A PARTITION. Overlapping sets read as a double count: an accepted task has
      // a verdict too, so it would appear twice and the rows would add to more than
      // the board holds.
      one()
      const counts = all('.pr-pipe-stage').map((s) => (s.textContent ?? '').trim())
      expect(counts[0]).toContain('With a worker · 1')
      expect(counts[1]).toContain('Ruled, not yet accepted · 1')
      expect(counts[2]).toContain('Accepted · 2')
      const titles = all('.pr-pipe-t').map((n) => n.textContent)
      expect(titles).toHaveLength(4)
      expect(new Set(titles).size).toBe(4)
    })

    it('names the gate between two stages rather than leaving an arrow', () => {
      one()
      const gates = all('.pr-pipe-gate').map((g) => g.textContent)
      expect(gates[0]).toContain('when the worker reports done')
      expect(gates[1]).toContain('when the lead takes the verdict')
    })

    it('names a worker by its per-render alias, never a session key', () => {
      // The conductor ledger's rule: no reader but the conductor sees a key. The
      // alias keeps the one property a reader needs -- two rows run by one worker
      // carry one token.
      one()
      const who = all('.pr-pipe-w').map((n) => n.textContent ?? '')
      expect(who[0]).toBe('worker w1')
      expect(who.some((w) => w.includes('chat-'))).toBe(false)
    })

    it('says a task has no worker rather than inventing one', () => {
      mount({
        items: [board([task({ title: 'unbound', spender: null })])],
        omitted: 0,
        series: [],
        for_you: [],
        last_entry_at: iso(3),
      })
      ;(all('.pr-pill').find((b) =>
        (b.textContent ?? '').includes('ship the report')) as HTMLElement).click()
      expect(text('.pr-pipe-w')).toBe('no worker bound')
    })
  })

  // ------------------------------------------------- live state, flash, and hold
  describe('the live indicator', () => {
    const base = { items: [board([task()])], omitted: 0, series: [], for_you: [] }

    it('says Live and names when the page last read', () => {
      mount({ ...base, last_entry_at: iso(2) })
      expect(document.querySelector('.pr-live')?.getAttribute('data-live')).toBe('true')
      expect(text('.pr-live')).toContain('Live')
      expect(text('.pr-live')).toMatch(/Updated \d\d:\d\d/)
    })

    it('reads one clock, so a committed screenshot is reproducible', () => {
      // The page must not mix `Date.now()` with `new Date()`. Both answer the same
      // thing in a browser and part company in the evidence harness, which pins
      // `Date.now` to its fixture's instant -- so a page mixing them renders a
      // different "Updated" on every shoot and the committed images differ byte for
      // byte. Asserted on the SOURCE because a fake timer here patches both.
      // COMMENTS STRIPPED FIRST. The line above this one quotes the forbidden form
      // in order to explain it, and a scan counting that would report the page's own
      // documentation as the offender.
      const code = (SCRIPT as string)
        .split('\n')
        .filter((line) => !/^\s*(\/\/|\/\/:)/.test(line))
        .join('\n')
      const bare = code.match(/new Date\(\s*\)/g) || []
      expect(bare, 'a bare `new Date()` reads the real clock past the pin').toEqual([])
      // THE CONTROL. Without it an empty result also means the pattern is wrong, and
      // a scan that matches nothing anywhere passes this case forever.
      expect(code.match(/new Date\(Date\.now\(\)\)/g) || [], 'the pattern matches nothing')
        .toHaveLength(1)
    })

    it('stops claiming Live once nothing is arriving', () => {
      // Read from the same stamp the stale band is raised from, so the dot and the
      // band cannot disagree about whether the page is being fed.
      mount({ ...base, last_entry_at: iso(100) })
      expect(document.querySelector('.pr-live')?.getAttribute('data-live')).toBe('false')
      expect(text('.pr-live')).toContain('No new entry')
      expect(document.querySelector('.pr-stale')).toBeTruthy()
    })
  })

  describe('the host writes the same elements this page does', () => {
    // FOUND ON THE REAL GATEWAY, not in a render. The host fills every
    // `data-dashboard-field` element from the raw value, stringifying an array as
    // JSON, and defers that first fill to DOMContentLoaded -- which lands AFTER this
    // page's inline script. So the accounting line, whose spans carry the field names
    // the contract requires, ended up showing a wall of raw JSON.
    //
    // The evidence renderer runs the script after the fill, the one order in which the
    // page was already right, so 34 committed screenshots could not have caught this.
    it('repaints its own line after the host fills it late', () => {
      mount({
        items: [{ id: 'w1', goal: 'G', open: 1, total: 1, tasks: [] }],
        for_you: [],
      })
      const span = () => document.querySelector('[data-dashboard-field="items"]')
      expect(span()?.textContent).toBe('1 workstream')
      // THE REAL ORDER, modelled rather than approximated. The event is dispatched at
      // `document` and BUBBLES, so document-level listeners run before window-level
      // ones -- and the host's is on `window`. The first version of this fix listened
      // on `document`, ran before the host's fill, and was clobbered by it; the first
      // version of this TEST dispatched a non-bubbling event at `document` and passed
      // anyway. Both had to be corrected together.
      document.dispatchEvent(new Event('DOMContentLoaded', { bubbles: true }))
      expect(span()?.textContent, 'the page left the raw JSON standing').toBe('1 workstream')
    })

    it('repaints after a host fill that is itself on window', () => {
      // The ordering, with a stand-in host registered BEFORE the page exactly as the
      // real bootstrap is. Without this case the page could go back to listening on
      // `document` and still pass the case above.
      const clobber = () => {
        const n = document.querySelector('[data-dashboard-field="items"]')
        if (n) n.textContent = JSON.stringify([{ id: 'w1' }])
      }
      window.addEventListener('DOMContentLoaded', clobber)
      try {
        mount({ items: [{ id: 'w1', goal: 'G', open: 1, total: 1, tasks: [] }] })
        document.dispatchEvent(new Event('DOMContentLoaded', { bubbles: true }))
        expect(
          document.querySelector('[data-dashboard-field="items"]')?.textContent,
          'the host fill won, so the page is listening on the wrong target',
        ).toBe('1 workstream')
      } finally {
        window.removeEventListener('DOMContentLoaded', clobber)
      }
    })

    it('repaints on the host refill beacon too', () => {
      mount({ items: [{ id: 'w1', goal: 'G', open: 1, total: 1, tasks: [] }] })
      const span = () => document.querySelector('[data-dashboard-field="items"]')
      span()!.textContent = 'clobbered'
      window.dispatchEvent(new Event('kirocrew-dashboard'))
      expect(span()?.textContent).toBe('1 workstream')
    })

    it('agrees in number with the counts it states', () => {
      // "1 workstreams, 1 hours with spend" was on the live page of every crewmate
      // with one board, which is every new one. The noun has to live with the number.
      mount({
        items: [{ id: 'w1', goal: 'G', open: 1, total: 1, tasks: [] }],
        series: [{ hour: '2026-10-04T17:00:00Z', credits: 1.1, accepted: 0 }],
      })
      const note = text('#pr-note')
      expect(note).toContain('1 workstream,')
      expect(note).toContain('1 hour with spend')
      expect(note).not.toContain('1 workstreams')
      expect(note).not.toContain('1 hours')
    })

    it('keeps the noun when a slot has no value', () => {
      // "..., —, last entry 34m ago" -- a dash governing nothing -- was on the live
      // page in the global view, where there is no single board to read lanes from.
      mount({ items: [{ id: 'w1', goal: 'G', open: 1, total: 1, tasks: [] }] })
      expect(text('#pr-note')).toContain('\u2014 red lanes')
      expect(text('#pr-note'), 'a dash governing nothing').not.toMatch(/\u2014,/)
    })

    it('still pluralises a real plural', () => {
      mount({
        items: [
          { id: 'w1', goal: 'A', open: 1, total: 1, tasks: [] },
          { id: 'w2', goal: 'B', open: 1, total: 1, tasks: [] },
        ],
        series: [
          { hour: '2026-10-04T16:00:00Z', credits: 1, accepted: 0 },
          { hour: '2026-10-04T17:00:00Z', credits: 2, accepted: 0 },
        ],
      })
      expect(text('#pr-note')).toContain('2 workstreams,')
      expect(text('#pr-note')).toContain('2 hours with spend')
    })

    it('reads a colour variable the themes actually define', () => {
      // `--fg` was invented and is defined by neither theme, so it took the dark
      // fallback and the cost line was near-invisible on light. Asserted on the
      // source: jsdom resolves no cascade, so only the declaration can be checked.
      // CSS AND JS COMMENTS STRIPPED FIRST: the declaration's own comment names the
      // forbidden variable in order to explain it, and a bare scan reports the fix's
      // documentation as the defect.
      const styled = ((SCRIPT as string) + MARKUP)
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '')
      expect(styled, 'an undefined variable falls back to one theme').not.toContain('--fg')
      // The control, so an over-eager strip cannot make this pass by emptying it.
      expect(styled).toContain('var(--text')
    })
  })

  describe('elapsed time as a phrase', () => {
    // Caught on the demo pod: the header read "entry just now ago". `fmtSince` returns
    // a bare length for the chips, where the column supplies the sense, and four other
    // callers each appended " ago" themselves. Under a minute that is ungrammatical,
    // and it is on screen the moment a fresh crewmate writes anything.
    it('says just now without a suffix', () => {
      mount({ items: [], last_entry_at: '2026-10-04T17:11:30Z' })
      expect(text('.pr-live')).toContain('just now')
      expect(text('.pr-live'), 'reads "just now ago"').not.toContain('just now ago')
    })

    it('still says ago for a real length', () => {
      // The control: without it, deleting the suffix everywhere would pass the case
      // above while making every other reading tenseless.
      mount({ items: [], last_entry_at: '2026-10-04T16:00:00Z' })
      expect(text('.pr-live')).toContain('1h12m ago')
    })

    it('keeps the quiet chip a bare length', () => {
      // The chip is NOT a sentence: its own word supplies the sense, so "quiet 2m" is
      // right and "quiet 2m ago" would be wrong.
      mount({
        items: [{
          id: 'w1', goal: 'G', tasks: [
            { id: 't1', title: 'A task', last_report_at: '2026-10-04T17:10:00Z' },
          ],
        }],
      })
      const chip = all('.pr-chip').map((n) => n.textContent?.trim()).find((t) => t?.startsWith('quiet'))
      expect(chip).toBe('quiet 2m')
    })
  })

  describe('cost, demoted to one line', () => {
    const board = {
      items: [
        {
          id: 'w1', goal: 'Ship the thing', open: 2, accepted: 1, total: 3,
          credits: 25.4, credits_reported: true, tasks: [],
        },
      ],
      series: [
        { hour: '2026-10-04T16:00:00Z', credits: 3.1, accepted: 1 },
        { hour: '2026-10-04T17:00:00Z', credits: 2.52, accepted: 0 },
      ],
    }

    it('states the spend and the last hour on one line', () => {
      mount(board)
      // The separator is preceded by a NON-BREAKING space (\u00a0) so "· last hour
      // 2.52" cannot wrap onto a line of its own, which would make a one-line summary
      // two lines at a narrow width. Spelled out because the two strings look
      // identical in a diff and the failure message is then unreadable.
      expect(text('.pr-cost-l')).toBe('spent 25.4 credits\u00a0· last hour 2.52')
      expect(text('.pr-cost-l')).not.toContain('credits ·')
    })

    it('hides the chart until asked', () => {
      mount(board)
      expect(all('.pr-hero'), 'the chart is open before anyone asked').toHaveLength(0)
      expect(text('.pr-cost-b')).toBe('show breakdown')
    })

    it('expands the existing chart, which is not deleted', () => {
      mount(board)
      ;(document.querySelector('.pr-cost-b') as HTMLButtonElement).click()
      expect(all('.pr-hero'), 'the chart did not come back').toHaveLength(1)
      expect(text('.pr-hero')).toContain('Credits per hour')
      expect(text('.pr-cost-b')).toBe('hide breakdown')
    })

    it('says so when a board reported no cost, instead of a tidy total', () => {
      // `credits_reported` is the whole point: a board that reported nothing is not a
      // board that cost nothing, and a total that leaves one out while reading like a
      // full total is the one way this line could lie.
      mount({
        items: [
          { id: 'w1', goal: 'A', credits: 25.4, credits_reported: true, tasks: [] },
          { id: 'w2', goal: 'B', tasks: [] },
        ],
        series: board.series,
      })
      expect(text('.pr-cost-l')).toContain('1 reported no cost')
      expect(text('.pr-cost-l')).not.toContain('last hour')
    })

    it('keeps the breakdown open across a refresh', () => {
      // The same rule as an expanded tree row and an open drawer. A reader who opened
      // the breakdown to watch an hour land must not have it shut by the next read.
      mount(board)
      ;(document.querySelector('.pr-cost-b') as HTMLButtonElement).click()
      expect(all('.pr-hero')).toHaveLength(1)
      refresh({
        ...board,
        series: [...board.series, { hour: '2026-10-04T18:00:00Z', credits: 1.4, accepted: 2 }],
      })
      expect(all('.pr-hero'), 'the refresh collapsed it').toHaveLength(1)
      expect(text('.pr-cost-b')).toBe('hide breakdown')
    })

    it('does not state the spend twice on the KPI row', () => {
      mount(board)
      const labels = all('.pr-kpis .pr-kpi-k').map((n) => n.textContent?.trim())
      expect(labels, 'cost is back to being said in two places').not.toContain('Spent')
    })

    it('is reachable by keyboard, being the only way to the charts', () => {
      mount(board)
      const btn = document.querySelector('.pr-cost-b') as HTMLButtonElement
      expect(btn.tagName, 'a div cannot be focused or pressed').toBe('BUTTON')
      expect(btn.getAttribute('aria-expanded')).toBe('false')
      btn.click()
      expect(
        (document.querySelector('.pr-cost-b') as HTMLButtonElement).getAttribute('aria-expanded'),
      ).toBe('true')
    })
  })

  describe('a crewmate with no workstream', () => {
    // THE BUG the lead found on the pod: the header counted "1 needs you" while the
    // body under it said "No workstream yet. Nothing needs you". Two numbers from one
    // page disagreeing, and the one a reader can ACT on was the wrong one.
    //
    // The cause is that one branch spoke for three different records. `items` is the
    // conductor's board, but `for_you` is written straight by the crewmate and the
    // hourly `series` is the crew log's own rollup -- so "no board" says nothing about
    // whether anything needs a reader or anything was spent. This is also the ONLY
    // state a fresh demo pod is in: a crewmate that answers one turn has written
    // `for_you` and spent credits, and has no board at all.
    it('still shows what needs you when there is no board', () => {
      mount({
        items: [],
        for_you: [{ text: 'Approve the staging rollout', workstream: '' }],
      })
      const rows = all('.pr-todo-i')
      expect(rows, 'the needs-you row was dropped with the board').toHaveLength(1)
      expect(text('#pr-view')).toContain('Approve the staging rollout')
      expect(text('#pr-view'), 'the body contradicts the header').not.toContain(
        'Nothing needs you',
      )
      // And the two numbers agree, which is the actual defect.
      expect(text('#pr-sentence')).toContain('1 needs you')
    })

    it('still shows the spend when there is no board', () => {
      mount({
        items: [],
        series: [{ hour: '2026-10-04T17:00:00Z', credits: 4.5, accepted: 0 }],
      })
      // Asserts the NUMBER IS THERE, not that a wrong sentence is absent. The first
      // version of this case checked only that "nothing has been spent" had gone --
      // and that sentence stays gone when the spend is dropped too, so the mutation
      // harness walked straight through it.
      expect(text('.pr-cost-l'), 'the spend was dropped with the board').toContain('4.5')
      expect(text('#pr-view')).not.toContain('nothing has been spent')
    })

    it('says the board is empty without speaking for the rest', () => {
      // The welcome card is RIGHT when everything really is empty, and it is the first
      // thing a new crewmate sees, so it keeps its sentence.
      mount({ items: [] })
      expect(text('#pr-view')).toContain('No workstream yet')
      expect(text('#pr-view')).toContain('Nothing needs you')
      expect(all('.pr-todo-i')).toHaveLength(0)
    })

    it('tells a reader the board is still empty while something needs them', () => {
      // Not just the needs-you card on its own: a reader looking at a page with no
      // tree needs to know the board is empty rather than failing to draw.
      mount({
        items: [],
        for_you: [{ text: 'Approve the staging rollout', workstream: '' }],
      })
      expect(text('#pr-view')).toContain('No workstream yet')
    })
  })

  describe('what a refresh must not disturb', () => {
    const page = (over: Record<string, unknown> = {}) => ({
      items: [board([task({ item_id: 'a', title: 'the fold', summary: 'first', ...over })])],
      omitted: 0,
      series: [],
      for_you: [],
      last_entry_at: iso(3),
    })

    it('flashes a row whose own values moved, and not on first paint', () => {
      // A page that flashed every row the moment it opened would be telling the
      // reader that everything just changed.
      mount(page())
      expect(all('.pr-flash')).toHaveLength(0)
      refresh(page({ summary: 'second' }))
      expect(all('.pr-flash')).toHaveLength(1)
    })

    it('does not flash a row that only re-rendered', () => {
      mount(page())
      refresh(page())
      expect(all('.pr-flash')).toHaveLength(0)
    })

    it('keeps an expanded row expanded across a refresh', () => {
      mount({
        items: [board([task({ item_id: 'a' }), task({ item_id: 'b', title: 'second' })])],
        omitted: 0,
        series: [],
        for_you: [],
        last_entry_at: iso(3),
      })
      const story = all('.pr-n-story > .pr-n-h')[0] as HTMLElement
      expect(story, 'no story row').toBeTruthy()
      const wrap = story.parentElement as HTMLElement
      const before = wrap.getAttribute('data-open')
      story.click()
      const toggled = (document.querySelector('.pr-n-story') as HTMLElement)
        .getAttribute('data-open')
      expect(toggled).not.toBe(before)
      refresh({
        items: [board([task({ item_id: 'a', summary: 'moved' }),
          task({ item_id: 'b', title: 'second' })])],
        omitted: 0,
        series: [],
        for_you: [],
        last_entry_at: iso(3),
      })
      expect((document.querySelector('.pr-n-story') as HTMLElement)
        .getAttribute('data-open')).toBe(toggled)
    })

    it('keeps the drawer open across a refresh, showing the NEW values', () => {
      // The page's whole point is that it advances, so a drawer that shut whenever
      // the fold moved would be unusable.
      mount(page())
      ;(document.querySelector('.pr-n-task > .pr-n-h') as HTMLElement).click()
      expect(text('.pr-dr-b')).toContain('first')
      refresh(page({ summary: 'second' }))
      expect(document.querySelector('.pr-dr'), 'the refresh closed the drawer').toBeTruthy()
      expect(text('.pr-dr-b')).toContain('second')
      expect(text('.pr-dr-b')).not.toContain('first')
    })

    it('closes the drawer when the fold drops the task it named', () => {
      // Closed rather than left on a stale copy: a drawer showing an item the page
      // no longer lists is the one thing worse than no drawer.
      mount(page())
      ;(document.querySelector('.pr-n-task > .pr-n-h') as HTMLElement).click()
      expect(document.querySelector('.pr-dr')).toBeTruthy()
      refresh({
        items: [board([task({ item_id: 'z', title: 'a different task' })])],
        omitted: 0,
        series: [],
        for_you: [],
        last_entry_at: iso(3),
      })
      expect(document.querySelector('.pr-dr')).toBeNull()
    })
  })
})
