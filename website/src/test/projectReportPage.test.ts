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
  extra: {
    written_at?: Record<string, string>
    agentic?: string[]
    seq?: number
    locale?: string
    last_user_at?: string
  } = {},
) {
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields,
    agentic: extra.agentic ?? ['ci', 'for_you', 'verdict'],
    seq: extra.seq ?? 412,
    stale: false,
    missing: [],
    written_at: extra.written_at ?? {},
    // The host sets this only when the reader has a non-English UI locale; an absent
    // key is the English default, which is what every existing case above relies on.
    ...(extra.locale === undefined ? {} : { locale: extra.locale }),
    ...(extra.last_user_at === undefined ? {} : { last_user_at: extra.last_user_at }),
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
function refresh(
  fields: Record<string, unknown>,
  extra: { written_at?: Record<string, string>; last_user_at?: string } = {},
) {
  const api = (window as unknown as { kirocrew: Record<string, unknown> }).kirocrew
  api.fields = fields
  if (extra.written_at) api.written_at = extra.written_at
  if (extra.last_user_at !== undefined) api.last_user_at = extra.last_user_at
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

    it('marks the line as the mate\'s own', () => {
      // A reader deciding whether to act on a judgment is entitled to know it is one.
      mount(live, { written_at: { verdict: iso(1) } })
      expect(text('.pr-vd-meta')).toContain('mate wrote this')
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

  describe('what needs you counts open items only', () => {
    // A worker's last word outlives the conductor's close: an item whose worker asked
    // and which the conductor then accepted, rejected or abandoned still reads
    // `question` or `blocked`. Counted by status alone, every such item stayed under
    // Needs you and the number only grew. The fold's own `needs_you` counts open items
    // only, and the page must too: it raises the fold's count to the rows it draws, so
    // a page drawing the closed rows would put them back in the number.
    const closed = ['accepted', 'rejected', 'abandoned'].flatMap((state) =>
      ['question', 'blocked'].map((status) =>
        task({ item_id: `${state}-${status}`, title: `${state} ${status}`, state, status }),
      ),
    )
    const fields = () => ({
      items: [
        board(
          [
            task({ item_id: 'q', title: 'open question', status: 'question', summary: 'which?' }),
            task({ item_id: 'b', title: 'open blocked', status: 'blocked', summary: 'no key' }),
            task({ item_id: 'p', title: 'open progress', status: 'progress' }),
            ...closed,
          ],
          // What the fold reports for this board: the two open items that wait.
          { needs_you: 2, open: 3, accepted: 2 },
        ),
      ],
      omitted: 0,
      series: [],
      for_you: [],
      last_entry_at: iso(3),
    })

    it('draws a card for an open question and an open blocker, and nothing closed', () => {
      mount(fields())
      const titles = all('.pr-todo-i .pr-todo-x > b').map((n) => n.textContent)
      expect(titles.sort()).toEqual(['open blocked', 'open question'])
    })

    it('counts the same two in the header and on the KPI card', () => {
      mount(fields())
      expect(text('#pr-sentence')).toContain('2 need you')
      const card = all('.pr-kpi').find(
        (n) => n.querySelector('.pr-kpi-label')?.textContent === 'Needs you',
      )
      expect(own(card?.querySelector('.pr-kpi-v'))).toBe('2')
    })
  })

  describe('a mate with no workstream', () => {
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

  // ------------------------- cards asked before the person wrote in the chat fold
  describe('cards asked before the person wrote in the chat', () => {
    // A reply typed in the chat, an option clicked there and a steer into the running
    // turn never pass through this page. The host says when the person last spoke in
    // the chat (`last_user_at`), so cards the crewmate wrote BEFORE that fold under
    // Asked before you wrote in chat, not asking the reader, until it writes the list
    // again. The page cannot tell whether that message answered a card.
    const WROTE = iso(10)
    const cards = [
      { text: 'Fix the CLI bubble?', ask: 'decide', options: ['Fix it', 'Leave it'], workstream: '' },
      { text: 'Fix skill_search?', ask: 'decide', options: ['Fix it', 'Leave it'], workstream: '' },
    ]
    // One worker question beside the cards, so the KPI card is drawn and a count that
    // dropped only the folded cards is told apart from one that dropped everything.
    const page = (over: Record<string, unknown> = {}) => ({
      items: [
        board(
          [task({ item_id: 'q1', title: 'which base?', status: 'question', last_report_at: iso(2) })],
          { needs_you: 1 },
        ),
      ],
      omitted: 0,
      series: [],
      for_you: cards,
      last_entry_at: iso(3),
      ...over,
    })
    const openCards = () => all('.pr-todo-i > .pr-todo-x > b').map((n) => n.textContent?.trim())
    const folds = () => all('.pr-fold > summary').map((n) => n.textContent?.trim())
    const sentLines = () => all('.pr-todo-sent').map((n) => n.textContent?.trim() ?? '')
    const needsKpi = () => {
      const card = all('.pr-kpi').find((k) => k.querySelector('.pr-kpi-label')?.textContent === 'Needs you')
      return own(card?.querySelector('.pr-kpi-v'))
    }
    const needsSub = () => {
      const card = all('.pr-kpi').find((k) => k.querySelector('.pr-kpi-label')?.textContent === 'Needs you')
      return card?.querySelector('.pr-kpi-sub')?.textContent?.trim() ?? ''
    }
    const replied = { written_at: { for_you: WROTE }, last_user_at: iso(5) }

    it('folds the cards written before the person wrote in chat under their own heading, saying why', () => {
      mount(page(), replied)
      expect(openCards()).toEqual(['which base?'])
      // Picked nothing on the page, so they are not under Answered.
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
      const lines = sentLines()
      expect(lines).toHaveLength(2)
      for (const line of lines) expect(line).toContain('\u2192 You wrote in chat after this was asked')
    })

    it('says in the header how many were asked before the person wrote in chat', () => {
      mount(page(), replied)
      expect(text('#pr-sentence')).toContain('1 needs you, 2 asked before you wrote in chat')
      // Nothing is added when no card is folded by a message in the chat.
      mount(page(), { written_at: { for_you: WROTE } })
      expect(text('#pr-sentence')).not.toContain('asked before you wrote in chat')
    })

    it('says on the KPI card how many were asked before the person wrote in chat when nothing else needs the reader', () => {
      const quiet = page({ items: [board([task({ item_id: 'p1', title: 'build it', status: 'progress' })])] })
      mount(quiet, replied)
      expect(needsKpi()).toBe('0')
      expect(needsSub()).toBe('2 asked before you wrote in chat')
      mount(quiet, { written_at: { for_you: WROTE } })
      expect(needsSub()).toBe('2 of them raised by the mate')
    })

    it('takes them out of every Needs-you count, and only them', () => {
      mount(page(), { written_at: { for_you: WROTE } })
      expect(needsKpi()).toBe('3')
      expect(text('#pr-sentence')).toContain('3 need you')
      mount(page(), replied)
      expect(needsKpi()).toBe('1')
      expect(text('#pr-sentence')).toContain('1 needs you')
      // None of the one left was raised by the crewmate, and nothing is "not shown".
      expect(text('.pr-kpis')).not.toContain('raised by the mate')
      expect(text('#pr-view')).not.toContain('more not shown')
    })

    it('still names the worker asks a truncated list could not show', () => {
      // The fold counts three asks on the board and the page holds one of them. The two
      // cards asked before the person wrote in chat are drawn but not counted, so they
      // cannot stand in for the two asks the list dropped.
      const truncated = page({
        items: [
          board(
            [task({ item_id: 'q1', title: 'which base?', status: 'question', last_report_at: iso(2) })],
            { needs_you: 3, tasks_omitted: 2 },
          ),
        ],
      })
      mount(truncated, replied)
      expect(needsKpi()).toBe('3')
      expect(text('#pr-view')).toContain('2 more not shown (list truncated)')
    })

    it('keeps asking when the mate wrote the list after the reply', () => {
      mount(page(), { written_at: { for_you: iso(5) }, last_user_at: iso(10) })
      expect(openCards()).toEqual(['which base?', 'Fix the CLI bubble?', 'Fix skill_search?'])
      expect(folds()).toEqual([])
    })

    it('keeps asking when the reply and the list share a second', () => {
      // The host sends both stamps at whole seconds, so a list written later in the
      // same second as the reply reads as EQUAL to it, which does not fold the cards.
      mount(page(), { written_at: { for_you: '2026-10-04T17:02:00+00:00' }, last_user_at: '2026-10-04T17:02:00+00:00' })
      expect(openCards()).toHaveLength(3)
      expect(folds()).toEqual([])
    })

    it('keeps asking when either stamp cannot be read', () => {
      for (const extra of [
        { written_at: { for_you: WROTE }, last_user_at: 'not a time' },
        { written_at: { for_you: WROTE }, last_user_at: '' },
        { written_at: { for_you: WROTE } },
        { written_at: {}, last_user_at: iso(5) },
        { written_at: { for_you: 'not a time' }, last_user_at: iso(5) },
      ]) {
        mount(page(), extra)
        expect(openCards(), JSON.stringify(extra)).toHaveLength(3)
        expect(text('#pr-sentence'), JSON.stringify(extra)).toContain('3 need you')
      }
    })

    it('asks again once the mate writes the list after the reply', () => {
      mount(page(), replied)
      expect(openCards()).toHaveLength(1)
      refresh(page(), { written_at: { for_you: iso(1) } })
      expect(openCards()).toHaveLength(3)
      expect(text('#pr-sentence')).toContain('3 need you')
    })

    it('unfolds a card on Show card until the next reply folds it again', () => {
      mount(page(), replied)
      const buttons = all('.pr-todo-sent button').map((b) => b.textContent)
      expect(buttons).toEqual(['Show card', 'Show card'])
      ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
      expect(openCards()).toEqual(['which base?', 'Fix the CLI bubble?'])
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 1'])
      expect(text('#pr-sentence')).toContain('2 need you, 1 asked before you wrote in chat')
      // A refill with the same reply keeps the card the reader unfolded.
      refresh(page())
      expect(openCards()).toEqual(['which base?', 'Fix the CLI bubble?'])
      // A newer reply folds it again.
      refresh(page(), { last_user_at: iso(4) })
      expect(openCards()).toEqual(['which base?'])
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
    })

    it('moves focus to the card Show card brings back, flashed and still saying the person wrote in chat after it', () => {
      mount(page(), replied)
      ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
      const active = document.activeElement as HTMLElement
      expect(active.classList.contains('pr-todo-i')).toBe(true)
      expect(active.querySelector('.pr-todo-x > b')?.textContent).toBe('Fix the CLI bubble?')
      expect(active.classList.contains('pr-flash')).toBe(true)
      expect(active.tabIndex).toBe(-1)
      expect(active.textContent).toContain('You wrote in chat after this was asked')
      // Only the card brought back is marked, and the next render marks nothing.
      expect(all('.pr-todo-i.pr-flash')).toHaveLength(1)
      refresh(page())
      expect(all('.pr-todo-i.pr-flash')).toHaveLength(0)
    })

    it('brings a card back on Show card without moving it, scrolling instantly under reduced motion', () => {
      // The shared setup installs a writable matchMedia stub that never matches.
      const realMatchMedia = window.matchMedia
      // A folded row and a card report different tops, so a slide from one to the
      // other would have an offset to animate.
      const rect = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
        const top = this.classList.contains('pr-todo-sent') ? 400 : this.classList.contains('pr-todo-i') ? 100 : 0
        return { top, bottom: top + 40, left: 0, right: 200, width: 200, height: 40, x: 0, y: top, toJSON: () => ({}) } as DOMRect
      })
      const scroll = vi.spyOn(Element.prototype, 'scrollIntoView')
      try {
        for (const reduce of [true, false]) {
          window.matchMedia = ((query: string) => ({
            matches: reduce && query === '(prefers-reduced-motion: reduce)',
            media: query,
            onchange: null,
            addEventListener: () => {},
            removeEventListener: () => {},
            addListener: () => {},
            removeListener: () => {},
            dispatchEvent: () => false,
          })) as unknown as typeof window.matchMedia
          scroll.mockClear()
          mount(page(), replied)
          ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
          const active = document.activeElement as HTMLElement
          expect(active.classList.contains('pr-todo-i'), String(reduce)).toBe(true)
          expect(active.classList.contains('pr-flash'), String(reduce)).toBe(true)
          const style = active.getAttribute('style') ?? ''
          expect(style, String(reduce)).not.toContain('transform')
          expect(style, String(reduce)).not.toContain('transition')
          expect(scroll).toHaveBeenCalledTimes(1)
          expect(scroll).toHaveBeenCalledWith({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' })
        }
      } finally {
        window.matchMedia = realMatchMedia
        rect.mockRestore()
        scroll.mockRestore()
      }
    })

    it('folds a card brought back with Show card again on Hide card, focus on the fold heading', () => {
      mount(page(), replied)
      ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
      // Only the card brought back carries Hide card.
      const withHide = all('.pr-todo-i').filter((c) => c.querySelector('.pr-todo-again button'))
      expect(withHide.map((c) => c.querySelector('.pr-todo-x > b')?.textContent)).toEqual(['Fix the CLI bubble?'])
      const hide = all('.pr-todo-again button')
      expect(hide.map((b) => b.textContent)).toEqual(['Hide card'])
      expect((hide[0] as HTMLButtonElement).type).toBe('button')
      ;(hide[0] as HTMLButtonElement).click()
      expect(openCards()).toEqual(['which base?'])
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
      expect(text('#pr-sentence')).toContain('1 needs you, 2 asked before you wrote in chat')
      expect(all('.pr-todo-again')).toHaveLength(0)
      // The clicked button is gone, so focus is on the heading of the fold the card went into.
      const active = document.activeElement as HTMLElement
      expect(active.tagName).toBe('SUMMARY')
      expect(active.textContent).toBe('Asked before you wrote in chat \u00b7 2')
      expect(active.parentElement?.querySelectorAll('.pr-todo-sent')).toHaveLength(2)
      // A refill with the same reply keeps it folded.
      refresh(page())
      expect(openCards()).toEqual(['which base?'])
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
    })

    it('unfolds a card picked on the page and then sent, with one Undo', () => {
      mount(page(), { written_at: { for_you: WROTE } })
      const fix = all('.pr-todo-opt').find((b) => b.textContent === 'Fix it') as HTMLButtonElement
      fix.click()
      vi.advanceTimersByTime(200)
      // The person sent what the button put in the chat box: that is a reply too.
      refresh(page(), { last_user_at: iso(5) })
      expect(sentLines()[0]).toContain('\u2192 Fix it')
      // A card picked on the page keeps Undo under Answered; the one folded by the reply
      // alone goes under Asked before you wrote in chat, with Show card.
      expect(folds()).toEqual(['Answered \u00b7 1', 'Asked before you wrote in chat \u00b7 1'])
      expect(sentLines()[1]).toContain('\u2192 You wrote in chat after this was asked')
      expect(all('.pr-todo-sent button').map((b) => b.textContent)).toEqual(['Undo', 'Show card'])
      ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
      expect(openCards()).toContain('Fix the CLI bubble?')
    })

    it('folds every card on an unrelated reply the mate has not answered, each still on the page', () => {
      // The person writes about something else and the crewmate has not written the
      // list again yet: every card folds, and each stays drawn with its own text and its
      // own Show card button, so none of them leaves the page.
      mount(page(), replied)
      expect(openCards()).toEqual(['which base?'])
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
      const rows = all('.pr-todo-sent')
      expect(rows.map((r) => r.querySelector('b')?.textContent)).toEqual(['Fix the CLI bubble?', 'Fix skill_search?'])
      expect(rows.map((r) => r.querySelector('button')?.textContent)).toEqual(['Show card', 'Show card'])
      // The crewmate's next write shows them asking.
      refresh(page(), { written_at: { for_you: iso(1) } })
      expect(openCards()).toEqual(['which base?', 'Fix the CLI bubble?', 'Fix skill_search?'])
      expect(folds()).toEqual([])
    })

    it('says nothing is left to pick only when every folded card was picked on the page', () => {
      const NOTHING = 'Nothing left to pick.'
      // Every card folded by the chat reply alone: the crewmate may still ask again,
      // so the desk does not say nothing is left.
      mount({ items: [], for_you: cards }, replied)
      expect(openCards()).toEqual([])
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
      expect(text('#pr-view')).not.toContain(NOTHING)
      // Every card picked on the page, and none folded by the chat: the line still shows.
      mount({ items: [], for_you: cards }, { written_at: { for_you: WROTE } })
      for (let n = 0; n < 2; n++) {
        const fix = all('.pr-todo-opt').find((b) => b.textContent === 'Fix it') as HTMLButtonElement
        fix.click()
        vi.advanceTimersByTime(200)
      }
      expect(openCards()).toEqual([])
      expect(folds()).toEqual(['Answered \u00b7 2'])
      expect(text('#pr-view')).toContain(NOTHING)
      // One card picked on the page and the other folded by a later message in the
      // chat: nothing is open, but the folded one may still be asked again.
      mount({ items: [], for_you: cards }, { written_at: { for_you: WROTE } })
      ;(all('.pr-todo-opt').find((b) => b.textContent === 'Fix it') as HTMLButtonElement).click()
      vi.advanceTimersByTime(200)
      refresh({ items: [], for_you: cards }, { last_user_at: iso(5) })
      expect(openCards()).toEqual([])
      expect(folds()).toEqual(['Answered \u00b7 1', 'Asked before you wrote in chat \u00b7 1'])
      expect(text('#pr-view')).not.toContain(NOTHING)
    })

    it('keeps a card asked before the person wrote in chat reachable on a page with no board', () => {
      mount({ items: [], for_you: cards }, replied)
      expect(folds()).toEqual(['Asked before you wrote in chat \u00b7 2'])
      expect(all('.pr-todo-sent button')).toHaveLength(2)
      expect(text('#pr-sentence')).toContain('0 need you, 2 asked before you wrote in chat')
      expect(text('#pr-view')).not.toContain('Nothing needs you, and nothing has been spent')
    })

    it('words the line in the mate\u2019s language, else in the reader\u2019s', () => {
      mount(
        page({
          verdict: {
            words: { replied: 'Saw your reply', reopen: 'Bring back', in_chat: 'With the mate', hide: 'Put away' },
          },
        }),
        replied,
      )
      expect(sentLines()[0]).toContain('\u2192 Saw your reply')
      expect(text('.pr-todo-sent button')).toBe('Bring back')
      expect(folds()).toEqual(['With the mate \u00b7 2'])
      ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
      expect(all('.pr-todo-again button').map((b) => b.textContent)).toEqual(['Put away'])
      mount(page(), { ...replied, locale: 'zh-CN' })
      // The zh-CN table defaults for word_replied, word_in_chat, word_reopen, word_hide
      // and the header's in_chat_suffix.
      expect(sentLines()[0]).toContain(
        '\u8fd9\u6761\u63d0\u51fa\u540e\uff0c\u4f60\u5728\u804a\u5929\u91cc\u53d1\u8fc7\u6d88\u606f',
      )
      expect(folds()).toEqual(['\u5728\u4f60\u53d1\u804a\u5929\u6d88\u606f\u4e4b\u524d\u63d0\u51fa \u00b7 2'])
      expect(text('.pr-todo-sent button')).toBe('\u663e\u793a\u5361\u7247')
      expect(text('#pr-sentence')).toContain(
        '\uff0c2 \u4e2a\u5728\u4f60\u53d1\u804a\u5929\u6d88\u606f\u4e4b\u524d\u63d0\u51fa',
      )
      ;(all('.pr-todo-sent button')[0] as HTMLButtonElement).click()
      expect(all('.pr-todo-again button').map((b) => b.textContent)).toEqual(['\u6536\u8d77\u5361\u7247'])
    })
  })

  // ------------------------------------------------ the desk words follow the locale
  describe('the desk words default from the locale, overridden by the verdict', () => {
    // The desk's labels have per-locale defaults in the page's own I18N table, and a
    // crewmate's `verdict.words` still overrides any of them, in every locale. The
    // "Write my own" button (`word_own`) is on every needs-you card, so it is the one
    // word read in all three readings here. zh-CN expectations are \u escapes so the
    // file stays ASCII.
    const deskFields = (over: Record<string, unknown> = {}) => ({
      items: [],
      for_you: [{ text: 'Approve the staging rollout', workstream: '' }],
      ...over,
    })
    const ownButton = () => text('.pr-todo-opt.pr-own')

    it('renders a desk default word in zh-CN', () => {
      // \u81ea\u5df1\u5199 is the zh-CN table default for word_own ("Write my own").
      mount(deskFields(), { locale: 'zh-CN' })
      expect(ownButton()).toBe('\u81ea\u5df1\u5199')
    })

    it('lets a verdict.words override beat the zh-CN table default', () => {
      // \u6211\u6765\u5199 is the crewmate's own wording; it wins over the table even
      // under zh-CN, which is the rule that must hold in every locale.
      mount(deskFields({ verdict: { words: { own: '\u6211\u6765\u5199' } } }), {
        locale: 'zh-CN',
      })
      expect(ownButton()).toBe('\u6211\u6765\u5199')
      expect(ownButton()).not.toBe('\u81ea\u5df1\u5199')
    })

    it('renders the English default word when the locale is en', () => {
      mount(deskFields(), { locale: 'en' })
      expect(ownButton()).toBe('Write my own')
    })
  })

  // ------------------------------------------------- what the crewmate published
  describe('the published block', () => {
    const PANEL = JSON.parse(
      readFileSync(
        resolve(__dirname, '../../../test/fixtures/dashboard_templates/sample_panel.json'),
        'utf-8',
      ),
    ).folds.panel.value
    const published = PANEL.data as Record<string, unknown>
    const card = () => document.querySelector('#pr-published-host .pr-pub')

    it('draws every published key as text, each shape its own way', () => {
      mount({ items: [], published })
      const node = card()
      expect(node, 'no published block was drawn').not.toBeNull()
      expect(text('#pr-published-host .pr-card-t')).toBe('Release watch')
      // A short scalar is a tile.
      const tiles = all('#pr-published-host .pr-pub-tile').map((t) => t.textContent)
      expect(tiles).toEqual(['phaserollout', 'open rulings2', 'healthyyes'])
      // A long string is a paragraph.
      expect(text('#pr-published-host .pr-pub-prose')).toContain('canary to settle')
      // An array of objects is a table, columns unioned across rows.
      const heads = all('#pr-published-host table.pr-pub-t th').map((t) => t.textContent)
      expect(heads).toEqual(['name', 'state', 'hosts'])
      expect(all('#pr-published-host table.pr-pub-t td').map((t) => t.textContent)).toContain('canary')
      // An array of scalars is a list.
      expect(all('#pr-published-host ul.pr-pub-l li').map((t) => t.textContent)).toEqual([
        'canary alarm under review',
        'one host failed its health check',
      ])
      // A nested object is key/value rows.
      expect(all('#pr-published-host dl.pr-pub-kv dt').map((t) => t.textContent)).toEqual([
        'rollout',
        'approval',
      ])
      // Published order is kept: the section headings follow the keys.
      expect(all('#pr-published-host .pr-pub-k').map((t) => t.textContent)).toEqual([
        'summary',
        'regions',
        'blockers',
        'roles',
      ])
    })

    it('says the mate wrote it', () => {
      mount({ items: [], published })
      expect(text('#pr-published-host .pr-agent')).toBe('mate wrote this')
      expect(text('[data-dashboard-field="published"]')).toBe('a board')
    })

    it('draws markup in a value as text, never as an element', () => {
      const hostile = '<img src=x onerror="window.__pwned=1"><b>bold</b>'
      mount({ items: [], published: { note: hostile, rows: [{ cell: hostile }], list: [hostile] } })
      expect(document.querySelector('#pr-published-host img')).toBeNull()
      expect(document.querySelector('#pr-published-host b')).toBeNull()
      expect(card()?.textContent).toContain('<img src=x')
    })

    it('is hidden when the mate never published', () => {
      for (const value of [undefined, {}, [], 'text']) {
        mount({ items: [], published: value })
        expect(card(), `a block was drawn for ${JSON.stringify(value)}`).toBeNull()
        expect(document.getElementById('pr-published-host')?.childNodes.length).toBe(0)
      }
    })

    it('leaves when a refresh drops the publish', () => {
      mount({ items: [], published })
      expect(card()).not.toBeNull()
      refresh({ items: [], published: {} })
      expect(card()).toBeNull()
    })

    it('says yes and no in the reader\'s language', () => {
      mount({ items: [], published: { healthy: true, done: false } }, { locale: 'zh-CN' })
      const tiles = all('#pr-published-host .pr-pub-tile dd').map((t) => t.textContent)
      expect(tiles).toEqual(['\u662f', '\u5426'])
    })

    it('leaves the footer clause out when nothing was published', () => {
      mount({ items: [board([task()])] })
      expect((document.getElementById('pr-note-pub') as HTMLElement).style.display).toBe('none')
      refresh({ items: [board([task()])], published })
      expect((document.getElementById('pr-note-pub') as HTMLElement).style.display).toBe('')
    })

    it('caps a long table and says so', () => {
      const rows = Array.from({ length: 150 }, (_, i) => ({ n: i }))
      mount({ items: [], published: { rows } })
      expect(all('#pr-published-host table.pr-pub-t tr')).toHaveLength(101)
      expect(text('#pr-published-host .pr-pub-note')).toBe('showing 100 of 150')
    })

    it('says when it drops columns', () => {
      const row = Object.fromEntries(Array.from({ length: 25 }, (_, i) => [`k${i}`, i]))
      mount({ items: [], published: { rows: [row] } })
      expect(all('#pr-published-host table.pr-pub-t th')).toHaveLength(20)
      expect(text('#pr-published-host .pr-pub-note')).toBe('columns capped at 20')
    })

    it('says once that only a publish is shown, with no zero counters above it', () => {
      const HINT = 'Below is what this mate last published.'
      mount({ items: [], published })
      expect(document.body.textContent).toContain(HINT)
      expect(document.body.textContent).not.toContain('No workstream yet.')
      expect((document.getElementById('pr-pills') as HTMLElement).style.display).toBe('none')
      expect((document.getElementById('pr-sentence') as HTMLElement).style.display).toBe('none')
      // With no publish the page is the ordinary empty page, counters included.
      mount({ items: [] })
      expect(document.body.textContent).not.toContain(HINT)
      expect((document.getElementById('pr-pills') as HTMLElement).style.display).toBe('')
    })

    it('brings the counters back once tracked work arrives', () => {
      mount({ items: [], published })
      refresh({ items: [board([task()])], published })
      expect((document.getElementById('pr-pills') as HTMLElement).style.display).toBe('')
      expect((document.getElementById('pr-sentence') as HTMLElement).style.display).toBe('')
    })
  })
})
