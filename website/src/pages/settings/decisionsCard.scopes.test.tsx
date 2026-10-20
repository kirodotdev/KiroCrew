/**
 * The Decisions (Jev) card's scope switches past the tool-argument one: the whole-transcript
 * scope on compaction.keep, the recalled-memory scope on memory.recall and the evidence
 * scope on nudge.wake, plus what a scope write does while it is in flight, when it is
 * refused and beside the main switch. Split from decisionsCard.test.tsx, which keeps the
 * main switch and the tool-argument switch; the fixtures both files use are in
 * src/test/decisionsCardTestUtils.tsx.
 *
 * The describes keep their decisionsCard.test.tsx order and install no hooks, so the
 * `saveDecisionsScope` spy and its calls carry from one describe to the next.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, cleanup, waitFor } from '@testing-library/react'

import { api, type DecisionsConsentData } from '../../api/client'
import {
  renderSection,
  decisionsSwitch,
  ENDPOINT,
  pointsOf,
  consentOf,
  pointRow,
  openPoint,
  SCOPE_SWITCH,
  toolArgsSwitch,
  stubGateway,
} from '../../test/decisionsCardTestUtils'
// DecisionsCard loads these two panes with React.lazy. decisionsCard.test.tsx's main-switch
// cases load them before any case there opens a scope panel; importing them here gives the
// cases below the same warm start, instead of a first case that loads a cold chunk inside
// a 1000 ms waitFor.
import './DecisionsPointPanel'
import './DecisionsProviderPicker'

/** The point that carries each scope, for a test that needs to reach its switch. */
const SCOPE_POINT: Record<string, string> = {
  tool_args: 'Risky tool-call notes',
  compaction: 'Which tool calls a compaction would keep (measured only)',
  memory_text: 'Which recalled memories reach the prompt',
  nudge_evidence: 'Quiet check-ins: wake or skip',
}

type ScopeName = 'tool_args' | 'compaction' | 'memory_text' | 'nudge_evidence'

/**
 * Open the panel that owns a scope and hand back its switch.
 *
 * Every scope switch lives on its own point's panel, and one panel renders at a
 * time, so a test cannot reach two scopes at once and must say which point it means.
 * That is the shape itself, not an accident of it: the switch is the OK for THAT
 * point, and drawing it beside the main switch is what made it ambiguous.
 */
async function openScope(scope: ScopeName) {
  await waitFor(() => {
    expect(pointRow(SCOPE_POINT[scope])).toBeInTheDocument()
  })
  openPoint(SCOPE_POINT[scope])
  // flake-ok: waits behind DecisionsCard's React.lazy DecisionsPointPanel, which this file imports up front so the chunk is warm; pre-existing wait, moved verbatim
  return await waitFor(() => screen.getByRole('switch', { name: SCOPE_SWITCH[scope] }))
}

/**
 * Open a scope's own panel and report whether its switch is there.
 *
 * The absence half of `openScope`, and it has to open the panel for the answer to
 * mean anything: one panel renders at a time, so "the switch is not on screen" is
 * true of every point that is not selected, whatever the card would draw for THIS
 * one. A test asserting absence without opening the panel passes on a card that
 * offers the switch with consent withheld.
 */
async function scopeSwitchOnPanel(scope: ScopeName) {
  await waitFor(() => {
    expect(pointRow(SCOPE_POINT[scope])).toBeInTheDocument()
  })
  openPoint(SCOPE_POINT[scope])
  // flake-ok: waits behind DecisionsCard's React.lazy DecisionsPointPanel, which this file imports up front so the chunk is warm; pre-existing wait, moved verbatim
  await waitFor(() => {
    expect(screen.getByText(/logged as/i)).toBeInTheDocument()
  })
  return screen.queryByRole('switch', { name: SCOPE_SWITCH[scope] })
}

/** The whole-transcript consent switch, on `compaction.keep`'s own panel. */
const compactionSwitch = () => screen.getByRole('switch', { name: SCOPE_SWITCH.compaction })

/** The recalled-memory consent switch, on `memory.recall`'s own panel. */
const memoryTextSwitch = () => screen.getByRole('switch', { name: SCOPE_SWITCH.memory_text })

describe('the whole-transcript consent scope', () => {
  it('is not drawn on its own panel while the main switch is off', async () => {
    // With consent withheld nothing is sent at all, so a second egress control would
    // describe a state that cannot happen -- and the gateway refuses the write anyway.
    //
    // The panel is OPENED before asserting absence. Without that, the assertion is
    // true of any point that is not the selected one, so it would pass on a card that
    // offered the switch with consent withheld -- which is the defect it names.
    stubGateway({ enabled: false })
    renderSection()
    expect(await scopeSwitchOnPanel('compaction')).toBeNull()
    // The row itself is still there: a point a reader cannot use is what the card
    // exists to explain.
    expect(pointRow(SCOPE_POINT.compaction)).toBeInTheDocument()
  })

  it('is not drawn on its own panel while the address moved out from under consent', async () => {
    // The switch is recorded-on here and still must not be offered: nothing is being
    // sent, so granting a wider category would be a control over an egress that is not
    // happening, and the route answers 409 for it.
    stubGateway(
      consentOf(true, {
        endpoint: ENDPOINT,
        configured_endpoint: 'https://proxy.example/v1/systemone',
        permits: false,
        compaction: true,
      }),
    )
    renderSection()
    expect(await scopeSwitchOnPanel('compaction')).toBeNull()
  })

  it('draws OFF for a consent recorded before the scope existed', async () => {
    // The whole point of a third leaf: an owner who consented to sending, and even to
    // tool arguments, has not consented to a whole transcript. Asserted one panel at a
    // time, because each switch is the OK for its own point.
    stubGateway(consentOf(true, { tool_args: true }))
    renderSection()
    expect((await openScope('tool_args')).getAttribute('aria-checked')).toBe('true')
    expect((await openScope('compaction')).getAttribute('aria-checked')).toBe('false')
  })

  it('draws ON when the keystone recorded it', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    renderSection()
    expect((await openScope('compaction')).getAttribute('aria-checked')).toBe('true')
  })

  it('grants the scope through the consent route, naming only its own field', async () => {
    stubGateway({ enabled: true })
    const scope = vi
      .spyOn(api, 'saveDecisionsScope')
      .mockResolvedValue(consentOf(true, { compaction: true }))
    const save = vi.spyOn(api, 'saveDecisionsConsent')
    renderSection()
    const sw = await openScope('compaction')
    sw.click()
    await waitFor(() => {
      // ONE field, and no endpoint echo: every other scope is omitted, which is what
      // preserves it, and echoing an address is a review this switch is not.
      expect(scope).toHaveBeenCalledWith('compaction', true)
    })
    // And the switch that means consent itself is untouched.
    expect(save).not.toHaveBeenCalled()
  })

  it('revokes it with an explicit false rather than by omission', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    const scope = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
    renderSection()
    const sw = await openScope('compaction')
    expect(sw.getAttribute('aria-checked')).toBe('true')
    sw.click()
    await waitFor(() => {
      // An explicit false: omission PRESERVES a scope, so a revoke has to be written.
      expect(scope).toHaveBeenCalledWith('compaction', false)
    })
  })

  it('names the point on its own panel, as the decision log spells it', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    renderSection()
    await openScope('compaction')
    // The identifier a reader greps the decision log for, on the panel for the point
    // it belongs to.
    expect(screen.getByTitle('compaction.keep')).toBeInTheDocument()
    cleanup()
    // And a gateway that does not ship the point names it nowhere: the card holds a
    // label per id and no list of its own, so the row and the identifier both go.
    stubGateway(
      consentOf(true, {
        points: pointsOf(true).filter(row => row.id !== 'compaction.keep'),
      }),
    )
    renderSection()
    await waitFor(() => {
      expect(screen.getByTitle('skills.select')).toBeInTheDocument()
    })
    expect(screen.queryByTitle('compaction.keep')).toBeNull()
    expect(
      screen.queryByRole('tab', { name: /Which tool calls a compaction would keep/i }),
    ).toBeNull()
  })

  it('says the compaction itself is unchanged', async () => {
    // A switch that read as "better compaction" would be a promise this build does
    // not keep: nothing is applied, and the answer is a line on a notice.
    stubGateway({ enabled: true })
    renderSection()
    await openScope('compaction')
    const rendered = document.body.textContent ?? ''
    expect(rendered).toContain('It is a measurement')
    expect(rendered).toContain('Tool OUTPUT is never sent')
    expect(rendered).toContain('Passwords and keys are replaced')
  })
})

describe('the whole-transcript scope reports its own in-flight and failed states', () => {
  /**
   * Both are about THIS PR's switch. A scope PUT carries `enabled: true` — the scope is
   * only meaningful while the seam is on — so a scope flip followed immediately by a
   * main switch OFF is two concurrent writes to one keystone, and the scope one
   * landing second restores consent the owner just revoked. And a refused scope write is
   * otherwise silent: the switch snaps back on the refetch with nothing saying why.
   *
   * The FREEZE is asserted for both scopes, because it is one hazard rather than one per
   * scope and the main switch's condition is this row's own. The tool-argument switch's
   * error surface is still not asserted here: that switch shipped before this one, and
   * #12492 is fixing the server side for every scope at once.
   */
  const heldSave = () => {
    let release: (v: DecisionsConsentData) => void = () => {}
    const promise = new Promise<DecisionsConsentData>(resolve => { release = resolve })
    return { promise, release: () => release(consentOf(true)) }
  }

  it('freezes the main switch while the scope write is in flight', async () => {
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    const sw = await openScope('compaction')
    // flake-ok: the main switch's gate reads only stubbed reads that resolve at once, and openScope has already waited on the later panel render; pre-existing read, moved verbatim
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    sw.click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    held.release()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })

  it('freezes the main switch while the TOOL-ARGUMENT scope write is in flight too', async () => {
    // Same hazard, other scope: a condition that named only one of the two covered part
    // of its own subject.
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    const sw = await openScope('tool_args')
    // flake-ok: the main switch's gate reads only stubbed reads that resolve at once, and openScope has already waited on the later panel render; pre-existing read, moved verbatim
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    sw.click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    held.release()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })

  it('says so when the scope write is refused, rather than reverting in silence', async () => {
    stubGateway(consentOf(true))
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    const sw = await openScope('compaction')
    sw.click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    // The stored value is what the switch shows: the refused write did not take.
    expect(sw.getAttribute('aria-checked')).toBe('false')
  })

  it('draws no error while the write is merely in flight', async () => {
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    const sw = await openScope('compaction')
    sw.click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    expect(screen.queryByTestId('decisions-save-error')).toBeNull()
    held.release()
  })
})

describe('the recalled-memory scope switch', () => {
  it('is not offered while the main switch is off', async () => {
    // Off, nothing is sent at all, so a second egress control would describe a
    // state that cannot happen.
    stubGateway({ enabled: false })
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch()).toBeInTheDocument()
    })
    expect(screen.queryByRole('switch', { name: /snippets of recalled memories/i })).toBeNull()
  })

  it('appears unchecked once consent is on, for a keystone that never recorded it', async () => {
    // The state every install consented before this scope existed is in: sending is
    // allowed, recalled-memory text is not, and the card must draw exactly that
    // rather than inferring the scope from the main switch.
    stubGateway({ enabled: true })
    renderSection()
    await openScope('memory_text')
    expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('reflects a recorded scope', async () => {
    stubGateway(consentOf(true, { memory_text: true }))
    renderSection()
    await openScope('memory_text')
    await waitFor(() => {
      expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('true')
    })
  })

  it('is not granted by the tool-argument scope', async () => {
    // Two independent decisions. A card that read one switch off the other would
    // show a consent the owner never gave.
    stubGateway(consentOf(true, { tool_args: true }))
    renderSection()
    await openScope('tool_args')
    await waitFor(() => {
      expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    // Each scope's switch is on its OWN point's panel, so the second answer is read
    // where it lives rather than beside the first.
    await openScope('memory_text')
    expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('grants the scope through the same consent route, naming only itself', async () => {
    stubGateway({ enabled: true })
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
      consentOf(true, { memory_text: true }),
    )
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      // ONLY its own scope: naming the other would let this click grant or erase a
      // scope the owner did not touch, which the route's omission rule exists for.
      expect(scopeSave).toHaveBeenCalledWith('memory_text', true)
    })
  })

  it('revokes it with an explicit false rather than by omission', async () => {
    stubGateway(consentOf(true, { memory_text: true }))
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
    renderSection()
    await openScope('memory_text')
    await waitFor(() => {
      expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('true')
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(scopeSave).toHaveBeenCalledWith('memory_text', false)
    })
  })

  it('states what the extra data is, and what the decision can do with it', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await openScope('memory_text')
    const body = document.body.textContent ?? ''
    expect(body).toContain('first 200 characters')
    expect(body).toContain('It can only remove them')
    expect(body).toContain('Passwords and keys are replaced')
  })

  it('defers recalled-memory snippets to their own switch in the egress note', async () => {
    // The note is what a reader consents to, and it is drawn whether or not the
    // scope switches are. It names what the main switch sends and says every other
    // category, recalled-memory snippets included, waits for a switch of its own.
    stubGateway({ enabled: false })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/sent over the internet to Jev/i)).toBeInTheDocument()
    })
    const note = screen.getByText(/sent over the internet to Jev/i).textContent ?? ''
    expect(note).toMatch(/your messages and your skills' names and descriptions/)
    expect(note).toMatch(/Every other kind of data has its own switch below and stays off/)
  })
})

describe('a scope write and the main switch cannot interleave', () => {
  // The hole this closes: flip a scope, immediately turn the seam OFF, and the two
  // PUTs race. The disable lands first, the scope write lands second carrying
  // `enabled: true`, and consent is re-committed by a click that was about a scope —
  // the owner turned egress off and it came back on.

  it('says NOTHING about enabled on a scope write', async () => {
    // Not even the value the card holds. That value comes from a read, and a concurrent
    // revoking PUT makes it stale inside the window — writing it back would re-commit a
    // consent the owner had just withdrawn, from a click that was about a scope. Saying
    // nothing is the only shape that cannot, and the gateway preserves the recorded flag.
    stubGateway({ enabled: true })
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
      consentOf(true, { memory_text: true }),
    )
    const consentSave = vi.spyOn(api, 'saveDecisionsConsent')
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(scopeSave).toHaveBeenCalledWith('memory_text', true)
    })
    // The scope path must not reach the consent writer at all: that is the function
    // whose body carries `enabled`.
    expect(consentSave).not.toHaveBeenCalled()
  })

  it('freezes the MAIN switch while a scope write is in flight', async () => {
    // The other half: the interleave cannot be started, not merely made harmless.
    stubGateway({ enabled: true })
    let release = () => {}
    vi.spyOn(api, 'saveDecisionsScope').mockImplementation(
      () => new Promise(resolve => { release = () => resolve(consentOf(true, { memory_text: true })) }),
    )
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    release()
  })

  it('refuses the NEXT scope write while one scope write is in flight', async () => {
    // Two outstanding writes against one keystone resolve in an order the clicks did
    // not choose, and the route records the whole file under one lock. The invariant is
    // about the WRITE, not about two switches being on screen together: this card draws
    // one point's panel at a time, so the second scope is reached by opening its own
    // panel while the first write is still outstanding, and its switch must refuse.
    stubGateway(consentOf(true, { tool_args: true }))
    let release = () => {}
    vi.spyOn(api, 'saveDecisionsScope').mockImplementation(
      () => new Promise(resolve => { release = () => resolve(consentOf(true, { tool_args: true })) }),
    )
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    const other = await openScope('tool_args')
    await waitFor(() => {
      expect(other.getAttribute('aria-disabled')).toBe('true')
    })
    release()
  })

  it('frees every switch again once the scope write settles', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true, { memory_text: true }))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })
})

describe('a refused scope write says so', () => {
  // The switch snaps back to the recorded value on the refetch, which on its own looks
  // like the click never landed. Each scope gets its OWN notice: two failures rendering
  // in one place would leave a reader unable to tell which write was refused.

  it('surfaces a refused tool-argument write, naming that switch', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('tool_args')
    toolArgsSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    expect(screen.getByTestId('decisions-save-error').textContent).toContain('Also send tool-call arguments')
  })

  it('surfaces a refused recalled-memory write, naming that switch', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    expect(screen.getByTestId('decisions-save-error').textContent).toContain('Also send snippets of recalled memories')
  })

  it('draws neither notice while both writes are healthy', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await openScope('memory_text')
    expect(screen.queryByTestId('decisions-save-error')).toBeNull()
  })

  it('lists memory.recall among what Jev decides, once its scope is granted', async () => {
    // The list answers "what does Jev decide". It named the skill choice and the
    // compaction measurement and omitted the one point that changes what a TOOL
    // RETURNS, which understates the feature exactly where the reader is deciding
    // whether to grant it.
    stubGateway(consentOf(true, { memory_text: true }))
    renderSection()
    await openScope('memory_text')
    const text = document.body.textContent ?? ''
    expect(text).toContain('Which recalled memories reach the prompt')
    expect(text).toContain('memory.recall')
  })

  it('reports memory.recall as needing your OK while its scope is not granted', async () => {
    // Without the scope the point is inert, and the card must not tell a reader the
    // decision happens. In a list that names every point the GATEWAY projects, that is
    // the row's STATUS rather than its absence: hiding the row would also hide the
    // switch that grants it, leaving a reader no way to reach the thing they are being
    // asked about.
    stubGateway(consentOf(true, { memory_text: false }))
    renderSection()
    const row = await waitFor(() => pointRow('Which recalled memories reach the prompt'))
    expect(row.textContent).toContain('Needs your OK')
    expect(row.textContent).not.toContain('Switched on')
  })

  it('defers every further category to its own switch without naming a count', async () => {
    // The note once said "Two further categories" while three scopes existed: a
    // count in the copy goes stale the moment a scope lands. The note now names no
    // number and no list, and points at the per-scope switches, which are the source
    // of truth; each scope's switch names its own category.
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/sent over the internet to Jev/i)).toBeInTheDocument()
    })
    const note = screen.getByText(/sent over the internet to Jev/i).textContent ?? ''
    expect(note).toMatch(/own switch below/)
    expect(note).not.toMatch(/\b(one|two|three|four|five) further/i)
    // The promise is "each category has its own switch", so every point that declares
    // a scope is pinned by name, and each one's panel must draw that switch. A scope
    // added, dropped or left without its switch fails here.
    const scoped = pointsOf(true)
      .filter(row => row.needs_scope !== null)
      .map(row => row.needs_scope)
      .sort()
    expect(scoped).toEqual(['compaction', 'memory_text', 'nudge_evidence', 'tool_args'])
    for (const scope of scoped as ScopeName[]) {
      expect(await openScope(scope)).toBeInTheDocument()
    }
  })

  it('says WHICH switch could not be saved', async () => {
    // "Could not save this switch" over three switches names none of them, and the
    // widest scope's notice does not even sit beside its own switch. Each notice
    // interpolates the label of the switch it belongs to.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    const notice = screen.getByTestId('decisions-save-error').textContent ?? ''
    expect(notice).toContain('Also send snippets of recalled memories')
    expect(notice).not.toContain('this switch')
  })

  it('names the compaction switch on its own displaced notice', async () => {
    // This is the notice the naming exists for: it renders at the foot of the card,
    // away from the switch it reports on, so without the label a reader cannot tell
    // which of the three writes was refused.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('compaction')
    compactionSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    const notice = screen.getByTestId('decisions-save-error').textContent ?? ''
    expect(notice).toContain('Also send the conversation and tool-call inputs')
    expect(notice).not.toContain('this switch')
  })

  it('names only the scope that failed', async () => {
    // One rejected write must not light the other scope's notice.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('nope'))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    // One notice, and it names the scope that failed and not the one beside it.
    expect(screen.getByTestId('decisions-save-error').textContent).toContain('Also send snippets of recalled memories')
    expect(screen.getByTestId('decisions-save-error').textContent).not.toContain('Also send tool-call arguments')
  })
})


describe("the wake judge's point on the card", () => {
  /* QA armed a loop against a gateway reporting `needs_scope=nudge_evidence
   * status=off` and saw a panel with no switch at all, so the scope could not be
   * granted from the dashboard and the feature was unreachable end to end. The
   * switch is drawn generically from the row's own `needs_scope`, so what these
   * assert is that every piece that lookup needs is registered: the scope's label,
   * its description, its granted position, and the point's plain-words name.
   */

  it('draws the nudge_evidence switch on the nudge.wake panel', async () => {
    stubGateway(consentOf(true))
    renderSection()
    const toggle = await openScope('nudge_evidence')
    expect(toggle).toBeInTheDocument()
    // Consent is on but this scope is withheld, which is the state an install that
    // consented before the scope existed is in.
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })

  it('shows the switch already on when the scope is granted', async () => {
    stubGateway(consentOf(true, { nudge_evidence: true }))
    renderSection()
    const toggle = await openScope('nudge_evidence')
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  })

  it('names the point in plain words rather than as a raw id', async () => {
    stubGateway(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(pointRow(SCOPE_POINT.nudge_evidence)).toBeInTheDocument()
    })
    // The fallback is `POINT_NAME[id] ?? id`, so an unregistered point renders its
    // internal identifier to every visitor, every visit.
    expect(screen.queryByRole('tab', { name: /^nudge\.wake$/ })).not.toBeInTheDocument()
  })

  it('withholds the switch entirely while Decisions consent is off', async () => {
    stubGateway(consentOf(false))
    renderSection()
    expect(await scopeSwitchOnPanel('nudge_evidence')).toBeNull()
  })
})
