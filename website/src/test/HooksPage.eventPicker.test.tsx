/**
 * The new/edit-hook card's lifecycle-event picker, after the native `<select>`
 * was replaced by `SimpleSelect` (Radix Select).
 *
 * Two things the migration changed and this pins:
 *  - the control now HAS an accessible name (it had none as a native select),
 *    reusing the page's existing "Event" catalog key;
 *  - an event value the picker doesn't offer (a legacy or hand-edited hook)
 *    shows the stored value on the trigger. A native select silently rendered
 *    the FIRST option while state held the stale value, so saving an untouched
 *    form appeared to change the event.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { i18nT } from '../i18n/t'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const createHook = vi.fn().mockResolvedValue({})
const updateHook = vi.fn().mockResolvedValue({})
let hooksPayload: { hooks: unknown[] } = { hooks: [] }

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => hooksPayload)
      if (prop === 'createHook') return createHook
      if (prop === 'updateHook') return updateHook
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: false },
    labels: { hooksSection: 'Provider hooks' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Open the "New Hook" card and return its event picker trigger. */
async function openForm() {
  fireEvent.click(await screen.findByRole('button', { name: '+ New Hook' }))
  return screen.findByLabelText('Event')
}

beforeEach(() => {
  vi.clearAllMocks()
  hooksPayload = { hooks: [] }
})

describe('hooks page — lifecycle event picker', () => {
  it('labels the picker and shows the default event', async () => {
    renderPage()
    const trigger = await openForm()
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger).toHaveTextContent('UserPromptSubmit')
  })

  it('warns on a column NAME wearing the added: prefix, which shape alone cannot catch', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )
    const field = screen.getByPlaceholderText(/^Matcher \(optional\)$/)

    // A space cannot appear in a tag id at any case, and folding cannot rescue it -- so this
    // one really is dead, and a check on shape alone would wave it through.
    fireEvent.change(field, { target: { value: '*added:my lane;*' } })
    fireEvent.blur(field)
    expect(screen.getByTestId('lane-matcher-never-fires')).toBeInTheDocument()

    // Case is NOT such a character: the backend lowercases both sides, so this fires.
    fireEvent.change(field, { target: { value: '*added:Done;*' } })
    fireEvent.blur(field)
    expect(screen.queryByTestId('lane-matcher-never-fires')).toBeNull()

    // A real id, and a class that could match one, must NOT warn.
    fireEvent.change(field, { target: { value: '*added:9f2c1ab77e40;*' } })
    fireEvent.blur(field)
    expect(screen.queryByTestId('lane-matcher-never-fires')).toBeNull()
    fireEvent.change(field, { target: { value: '*added:[0-9a-f]*' } })
    fireEvent.blur(field)
    expect(screen.queryByTestId('lane-matcher-never-fires')).toBeNull()
  })

  it('warns politely while typing and assertively once the author leaves the field', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )
    const field = screen.getByPlaceholderText(/^Matcher \(optional\)$/)
    fireEvent.focus(field)

    // Saving closes the form, so the note must be READABLE while typing -- but an assertive
    // alert per keystroke would interrupt on every character toward a valid value.
    fireEvent.change(field, { target: { value: 'd' } })
    const note = screen.getByTestId('lane-matcher-never-fires')
    expect(note).toBeInTheDocument()
    expect(note.getAttribute('role')).toBeNull()
    expect(note.getAttribute('aria-live')).toBe('polite')

    // Leaving the field is the author saying the value is finished, so now it may interrupt.
    fireEvent.blur(field)
    const settled = screen.getByTestId('lane-matcher-never-fires')
    expect(settled.getAttribute('role')).toBe('alert')
    expect(settled.getAttribute('aria-live')).toBeNull()
  })

  it('does not warn outside glob mode, where a bare column name does match', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )
    const field = screen.getByPlaceholderText(/^Matcher \(optional\)$/)
    fireEvent.change(field, { target: { value: 'done' } })
    fireEvent.blur(field)

    // Glob is whole-string fnmatch, so a bare `done` genuinely cannot match.
    expect(screen.getByTestId('lane-matcher-never-fires')).toBeInTheDocument()

    // contains/regex DO match `added:done;` on the backend, so the warning must go quiet
    // rather than condemn a matcher that fires.
    const modeControl = screen.getByLabelText(i18nT('pages.hooksPage.matcher_mode'))
    for (const mode of ['contains', 'regex']) {
      fireEvent.click(modeControl)
      fireEvent.click(await screen.findByRole('option', { name: mode }))
      await waitFor(() => expect(modeControl).toHaveTextContent(mode))
      expect(screen.queryByTestId('lane-matcher-never-fires')).toBeNull()
    }

    // Back to glob and the warning returns, so the gate is the mode and not the matcher.
    fireEvent.click(modeControl)
    fireEvent.click(await screen.findByRole('option', { name: 'glob' }))
    await waitFor(() => expect(modeControl).toHaveTextContent('glob'))
    expect(screen.getByTestId('lane-matcher-never-fires')).toBeInTheDocument()
  })

  it('announces the never-fires warning and ties it to the matcher input', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )
    const field = screen.getByPlaceholderText(/^Matcher \(optional\)$/)
    fireEvent.change(field, { target: { value: 'Done' } })
    fireEvent.blur(field)

    // It appears mid-typing, so a screen reader never reaches it without being told.
    const warn = screen.getByTestId('lane-matcher-never-fires')
    expect(warn).toHaveAttribute('role', 'alert')
    expect(field.getAttribute('aria-describedby') || '').toContain(warn.id)
    expect(warn.id).not.toBe('')

    // A warning that only condemns leaves the author stuck; this one carries the way out.
    expect(warn).toHaveTextContent(/empty matcher/i)
    expect(warn).toHaveTextContent(/KIROCREW_HOOK_CONTEXT/)
  })

  it('tells a governance-denied test run which setting to change', () => {
    // The denial is shared base copy for all six events, so the repair path lives here
    // rather than in that string -- a denied author otherwise sees no next step at all.
    const repair = i18nT('pages.hooksPage.hook_test_governance_repair')
    expect(repair).toMatch(/capabilities\.script_hooks/)
    expect(repair).not.toBe('pages.hooksPage.hook_test_governance_repair')
  })

  it('does not send the author hunting for an id no screen shows', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )

    // Prescribing *added:<tag-id>;* while no surface reveals a tag id is a dead end. The
    // guidance must name the route that works today instead.
    expect(screen.getByTestId('lane-matcher-hint')).toHaveTextContent(/KIROCREW_HOOK_CONTEXT/)
  })

  it('does not prescribe a shape whose id the author cannot look up', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )

    // Review found the copyable `*added:<tag-id>;*` a dead end, because no surface reveals
    // a tag id -- so the guidance names the empty matcher and the context env var instead.
    const hint = screen.getByTestId('lane-matcher-hint')
    expect(hint).not.toHaveTextContent('<tag-id>')

    // The route to an ID belongs in the warning, which appears exactly when one is needed;
    // repeating either half here made a prose wall every lane-hook author reads on every visit.
    expect(hint).not.toHaveTextContent(/empty matcher/i)
    expect(hint).not.toHaveTextContent(/no screen shows/i)
    fireEvent.change(screen.getByPlaceholderText(/^Matcher \(optional\)$/), {
      target: { value: 'Done' },
    })
    fireEvent.blur(screen.getByPlaceholderText(/^Matcher \(optional\)$/))
    expect(screen.getByTestId('lane-matcher-never-fires')).toHaveTextContent(
      /no screen shows a tag id yet/i,
    )

    // Naming the automatic columns in the board's OWN words: "only tag edits fire it" was
    // accurate and unmappable, since a board user sees columns and never sees a tag.
    // Promoted out of the muted hint onto its own unmuted line, as the scope line was.
    const automatic = screen.getByTestId('lane-matcher-automatic')
    expect(automatic).toHaveTextContent(/automatic columns/i)
    expect(automatic).toHaveTextContent(/Working/)
    expect(automatic).toHaveTextContent(/Idle/)
    expect(automatic).toHaveTextContent(/never fire it/i)

    // A hook author binding irreversible close-out work must see that an app or automation
    // moving the card fires nothing -- on its OWN line, not last in a muted paragraph.
    const scope = screen.getByTestId('lane-matcher-scope')
    expect(scope).toHaveTextContent(/never one an app or an automation makes/i)
    expect(hint).not.toHaveTextContent(/never one an app or an automation makes/i)
  })

  it('does not style the recommended empty matcher as a problem', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )

    // Empty is the RECOMMENDED state -- fires on every column -- so warn styling here
    // trains the reader to ignore the one case that is actually broken.
    expect(screen.getByTestId('lane-matcher-hint').className).not.toContain('text-warn')
    expect(screen.queryByTestId('lane-matcher-never-fires')).toBeNull()

    // A bare column name IS the broken case, and only now does a warning belong.
    fireEvent.change(screen.getByPlaceholderText(/^Matcher \(optional\)$/), { target: { value: 'Done' } })
    fireEvent.blur(screen.getByPlaceholderText(/^Matcher \(optional\)$/))
    const warn = screen.getByTestId('lane-matcher-never-fires')
    expect(warn.className).toContain('text-warn')

    // The working shape is not the broken case.
    fireEvent.change(screen.getByPlaceholderText(/^Matcher \(optional\)$/), {
      target: { value: '*added:9f2c1ab77e40;*' },
    })
    fireEvent.blur(screen.getByPlaceholderText(/^Matcher \(optional\)$/))
    expect(screen.queryByTestId('lane-matcher-never-fires')).toBeNull()
  })

  it('warns that the lane matcher takes tag ids, not the column name', async () => {
    renderPage()
    const trigger = await openForm()

    // The generic "e.g. *deploy*" placeholder invites the column name, which saves a
    // hook the backend never fires -- so this event needs its own guidance.
    expect(screen.getByPlaceholderText(/e\.g\. \*deploy\*/)).toBeInTheDocument()
    expect(screen.queryByTestId('lane-matcher-hint')).toBeNull()

    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))

    await waitFor(() =>
      expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'),
    )
    expect(screen.getByPlaceholderText(/^Matcher \(optional\)$/)).toBeInTheDocument()

    // The instruction is neutral and always present; the ids-not-names sentence now lives
    // in the warning, which only a matcher that cannot match brings on screen.
    const hint = screen.getByTestId('lane-matcher-hint')
    expect(hint).toHaveTextContent(/leave empty to fire on any move between columns you created/i)
    fireEvent.change(screen.getByPlaceholderText(/^Matcher \(optional\)$/), { target: { value: 'Done' } })
    fireEvent.blur(screen.getByPlaceholderText(/^Matcher \(optional\)$/))
    expect(screen.getByTestId('lane-matcher-never-fires')).toHaveTextContent(/compares tag IDs/i)
    // A bare id is the state the hint's own instructions produce, so the warning has to name the
    // shape that works rather than only saying this one does not.
    expect(screen.getByTestId('lane-matcher-never-fires')).toHaveTextContent('*added:done;*')
    expect(screen.getByTestId('lane-matcher-hint')).toBeInTheDocument()
  })

  it('offers every lifecycle event and commits the pick', async () => {
    renderPage()
    const trigger = await openForm()

    // Radix Select: open, then click — a `change` on the trigger does nothing.
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(6))
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual([
      'AgentSpawn', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop',
      // Glossed at the point of choice: the wire value is immutable, so the option
      // carries the board's own word beside it.
      'SessionLaneChanged — board column',
    ])

    fireEvent.click(screen.getByRole('option', { name: 'PreToolUse' }))
    await waitFor(() => expect(screen.getByLabelText('Event')).toHaveTextContent('PreToolUse'))

    // The matcher placeholder switches to the tool-filter copy, proving the
    // pick reached the form state and not just the trigger's own label.
    expect(screen.getByPlaceholderText(/Matcher \(tool filter/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(createHook).toHaveBeenCalledTimes(1))
    expect(createHook.mock.calls[0][0]).toMatchObject({ event: 'PreToolUse' })
  })

  it('shows a stored event the picker no longer offers instead of the first option', async () => {
    hooksPayload = {
      hooks: [{
        id: 'h1', name: 'legacy', event: 'agentSpawn', matcher: '', command: 'true',
        timeout: 30, enabled: true, last_run: 0, last_status: '', run_count: 0,
      }],
    }
    renderPage()

    // Edit lives in the row's ⋯ overflow menu. The trigger is a Radix
    // DropdownMenuTrigger, which opens on keyboard activation (Enter) — a path
    // jsdom handles, unlike the PointerEvent-driven click Radix uses for mouse.
    fireEvent.keyDown(await screen.findByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    const trigger = await screen.findByLabelText('Event')
    expect(trigger).toHaveTextContent('agentSpawn')
    expect(trigger).not.toHaveTextContent('AgentSpawn')

    // Saving without touching the picker must not silently rewrite the event.
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateHook).toHaveBeenCalledTimes(1))
    expect(updateHook.mock.calls[0][1]).toMatchObject({ event: 'agentSpawn' })
  })

  it('warns inline on a dead lane matcher without holding the save', async () => {
    renderPage()
    const trigger = await openForm()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('option', { name: 'SessionLaneChanged — board column' }))

    // The most likely first attempt: a column name typed straight in, never blurred. Saving
    // closes the form, so the note has to be on screen BEFORE the press to be read at all.
    const matcher = screen.getByPlaceholderText(/^Matcher \(optional\)$/)
    fireEvent.focus(matcher)
    fireEvent.change(matcher, { target: { value: 'Done' } })
    expect(screen.getByTestId('lane-matcher-never-fires')).toBeInTheDocument()
    expect(createHook).not.toHaveBeenCalled()

    // The note informs; it does not gate. One press saves, so there is no armed second state
    // to explain and no confirm control to hunt for.
    expect(screen.queryByTestId('lane-matcher-confirm-save')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(createHook).toHaveBeenCalledTimes(1))
    expect(createHook.mock.calls[0][0]).toMatchObject({ matcher: 'Done' })
  })
})
