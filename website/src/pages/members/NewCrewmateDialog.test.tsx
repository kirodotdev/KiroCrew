import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../../components/NavigationLeaveGuard'
import { api } from '../../api/client'
import NewCrewmateDialog from './NewCrewmateDialog'
import { seededTraits } from '../../components/CrewAvatar'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
      workspaces: vi.fn(() => Promise.resolve({ workspaces: [{ name: 'default' }] })),
      availableModels: vi.fn(() => Promise.resolve({ models: [] })),
      members: vi.fn(() => Promise.resolve({ members: [] })),
      createKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true, name: 'Scout' })),
    },
  }
})

let mayLeave: () => boolean = () => true
function Probe() {
  mayLeave = useMayLeaveForNavigation()
  return null
}
const unloadPrevented = () => {
  const ev = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(ev)
  return ev.defaultPrevented
}
const props = { onClose: vi.fn(), onCreated: vi.fn(), existingNames: [] as string[] }
const typeName = (v: string) => fireEvent.change(screen.getByRole('textbox', { name: 'Name' }), { target: { value: v } })
/** The helper line `Field` renders under a control: its frame's last child. */
const fieldHint = (control: HTMLElement) => {
  const frame = control.closest('.flex.flex-col.gap-1\\.5')
  if (!frame?.lastElementChild || frame.lastElementChild.contains(control)) throw new Error('field has no hint')
  return frame.lastElementChild as HTMLElement
}

describe('NewCrewmateDialog', () => {
  beforeEach(() => {
    props.onClose = vi.fn()
    props.onCreated = vi.fn()
    vi.mocked(api.createKirocrewAgent).mockClear()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('standalone: still a modal dialog', async () => {
    renderWithProviders(<NewCrewmateDialog open {...props} />)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toContainElement(screen.getByTestId('crewmate-create-form'))
    expect(screen.queryByTestId('crewmate-create-embedded')).toBeNull()
  })

  it('embedded: the same complete form in a labelled page region, no dialog, no Escape dismissal', async () => {
    const { container } = renderWithProviders(<NewCrewmateDialog open embedded {...props} />)
    const region = await screen.findByRole('region', { name: 'New crewmate' })
    expect(container.contains(region)).toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(region).toContainElement(screen.getByTestId('crewmate-create-form'))
    expect(region).toContainElement(screen.getByTestId('crewmate-create-submit'))
    // The expert fields are still there behind Advanced settings.
    fireEvent.click(screen.getByTestId('crewmate-create-advanced-toggle'))
    expect(screen.getByTestId('crewmate-create-advanced')).toBeInTheDocument()
    const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    document.dispatchEvent(esc)
    expect(props.onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('embedded: renders nothing while closed and submits the same create', async () => {
    const { rerender } = renderWithProviders(<NewCrewmateDialog open={false} embedded {...props} />)
    expect(screen.queryByTestId('crewmate-create-embedded')).toBeNull()
    rerender(<NewCrewmateDialog open embedded {...props} />)
    await screen.findByTestId('crewmate-create-embedded')
    typeName('Scout')
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    // Unasked, the create owes no first greeting.
    const body = vi.mocked(api.createKirocrewAgent).mock.calls[0][0] as Record<string, unknown>
    expect(body).toMatchObject({ name: 'Scout', kiro_agent: 'kirocrew' })
    expect(body).not.toHaveProperty('first_greeting')
    await waitFor(() => expect(props.onCreated).toHaveBeenCalledWith({ name: 'Scout', job: '' }), { timeout: 4000 })
  })

  it('Advanced settings offers the built-in agent first, labelled as the default', async () => {
    renderWithProviders(<NewCrewmateDialog open embedded startExpanded {...props} />)
    const select = await screen.findByRole('combobox', { name: 'Built from' })
    // The label stays short enough to show "(default)" on a phone; what the
    // default is goes in the field's helper text instead.
    expect(select.textContent?.trim()).toBe('kirocrew (default)')
    expect(fieldHint(select)).toHaveTextContent(/kirocrew/)
  })

  it('the Model helper text is a sentence of its own, not a lowercase fragment', async () => {
    renderWithProviders(<NewCrewmateDialog open embedded startExpanded {...props} />)
    const select = await screen.findByRole('combobox', { name: 'Edit default model' })
    const hint = fieldHint(select).textContent ?? ''
    expect(hint).toMatch(/^\p{Lu}/u)
    expect(hint).toMatch(/\.$/)
  })

  describe('one card with Advanced settings inline', () => {
    const card = (extra: Record<string, unknown> = {}) =>
      renderWithProviders(<NewCrewmateDialog open embedded firstGreeting {...props} {...extra} />)
    const toggle = () => screen.getByTestId('crewmate-create-advanced-toggle')

    it('folded by default: only the name, the avatar, the hint and the footer, and the template is never named', async () => {
      card()
      const region = await screen.findByRole('region', { name: 'New crewmate' })
      expect(screen.getByRole('textbox', { name: 'Name' })).toBeInTheDocument()
      expect(screen.getByTestId('crewmate-create-look')).toBeInTheDocument()
      // The look is the hero: the tile centred with its caption under it,
      // and no visible small-caps labels over it or over the name.
      const identity = screen.getByTestId('crewmate-create-identity')
      expect(Array.from(identity.children)).toEqual([
        screen.getByTestId('crewmate-create-look'),
        screen.getByTestId('crewmate-create-look-caption'),
      ])
      expect(identity).toHaveClass('flex-col', 'items-center')
      expect(screen.getByTestId('crewmate-create-look-caption')).toHaveTextContent('Tap the picture for another look')
      expect(screen.getByTestId('crewmate-create-look-caption')).toHaveClass('text-center')
      expect(identity).not.toContainElement(screen.getByRole('textbox', { name: 'Name' }))
      expect(within(region).queryByText('Avatar')).toBeNull()
      expect(within(region).queryByText('Name')).toBeNull()
      // The tile is the one re-roll control, named by the old button's string.
      expect(screen.getAllByRole('button', { name: 'Try another look' })).toEqual([screen.getByTestId('crewmate-create-look')])
      // The hint is the shared tooltip, not the native one.
      expect(screen.getByTestId('crewmate-create-look')).not.toHaveAttribute('title')
      expect(screen.getByTestId('crewmate-create-quick-hint')).toBeInTheDocument()
      expect(screen.getAllByRole('textbox')).toHaveLength(1)
      expect(screen.queryByRole('combobox')).toBeNull()
      expect(screen.queryByTestId('crewmate-create-advanced')).toBeNull()
      expect(toggle()).toHaveAttribute('aria-expanded', 'false')
      expect(toggle()).toHaveTextContent('Advanced settings')
      expect(region).not.toHaveTextContent(/kirocrew/i)
    })

    it('unfolding shows Built from = kirocrew and every setting in the same card, under the same heading', async () => {
      card()
      const region = await screen.findByRole('region', { name: 'New crewmate' })
      fireEvent.click(toggle())
      expect(toggle()).toHaveAttribute('aria-expanded', 'true')
      const advanced = screen.getByTestId('crewmate-create-advanced')
      expect(toggle()).toHaveAttribute('aria-controls', advanced.id)
      expect(region).toContainElement(advanced)
      expect(await screen.findByRole('combobox', { name: 'Built from' })).toHaveTextContent('kirocrew')
      expect(screen.getByRole('textbox', { name: 'What it looks after' })).toBeInTheDocument()
      expect(advanced).toContainElement(screen.getByRole('combobox', { name: 'Built from' }))
      expect(within(advanced).getByLabelText('Workspace')).toBeInTheDocument()
      expect(within(advanced).getByLabelText('Edit default model')).toBeInTheDocument()
      // One disclosure, not a second one nested inside it, and no way "back".
      expect(screen.getAllByTestId('crewmate-create-advanced-toggle')).toHaveLength(1)
      expect(screen.queryByRole('button', { name: 'Back' })).toBeNull()
      expect(screen.getByRole('region', { name: 'New crewmate' })).toBe(region)
    })

    it('values set under Advanced settings survive folding and unfolding, and are sent', async () => {
      card()
      await screen.findByTestId('crewmate-create-embedded')
      typeName('Scout')
      fireEvent.click(toggle())
      fireEvent.change(screen.getByRole('textbox', { name: 'What it looks after' }), { target: { value: 'Watch PRs' } })
      fireEvent.click(toggle())
      await waitFor(() => expect(screen.queryByTestId('crewmate-create-advanced')).toBeNull())
      fireEvent.click(toggle())
      expect(screen.getByRole('textbox', { name: 'What it looks after' })).toHaveValue('Watch PRs')
      expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Scout')
      // Folded again: still sent.
      fireEvent.click(toggle())
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      expect(vi.mocked(api.createKirocrewAgent).mock.calls[0][0]).toMatchObject({
        name: 'Scout',
        kiro_agent: 'kirocrew',
        description: 'Watch PRs',
        first_greeting: true,
        avatar: { kind: 'ghost', traits: seededTraits('Scout') },
      })
    })

    it('Create from the folded card builds from kirocrew, pinning the shown look and asking for the first greeting', async () => {
      card()
      await screen.findByTestId('crewmate-create-embedded')
      typeName('Scout')
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      const body = vi.mocked(api.createKirocrewAgent).mock.calls[0][0] as Record<string, unknown>
      expect(body).toMatchObject({
        name: 'Scout',
        kiro_agent: 'kirocrew',
        workspace: 'default',
        description: '',
        first_greeting: true,
        avatar: { kind: 'ghost', traits: seededTraits('Scout') },
      })
      expect(body).not.toHaveProperty('model')
    })

    it('a re-rolled look is the one sent: the tile and its corner badge each draw another', async () => {
      card()
      await screen.findByTestId('crewmate-create-embedded')
      typeName('Scout')
      const badge = screen.getByTestId('crewmate-create-look-reroll')
      expect(screen.getByTestId('crewmate-create-look')).toContainElement(badge)
      fireEvent.click(screen.getByTestId('crewmate-create-look'))
      fireEvent.click(badge)
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      const body = vi.mocked(api.createKirocrewAgent).mock.calls[0][0] as { avatar: { kind: string; traits: ReturnType<typeof seededTraits> } }
      expect(body.avatar.kind).toBe('ghost')
      // Some later roll of the same name, never the name's own look.
      const rolls = Array.from({ length: 64 }, (_, i) => seededTraits(`Scout#${i + 1}`))
      expect(rolls).toContainEqual(body.avatar.traits)
      expect(body.avatar.traits).not.toEqual(seededTraits('Scout'))
    })

    it('every re-roll changes the tile colour, even where the next draw would repeat it', async () => {
      // A name whose first re-draw keeps its tile colour: a press that only
      // advanced one draw would show the same colour again.
      const name = Array.from({ length: 400 }, (_, i) => `Mate ${i}`)
        .find((n) => seededTraits(n).tile === seededTraits(`${n}#1`).tile)
      expect(name).toBeDefined()
      card()
      await screen.findByTestId('crewmate-create-embedded')
      typeName(name!)
      fireEvent.click(screen.getByTestId('crewmate-create-look'))
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      const body = vi.mocked(api.createKirocrewAgent).mock.calls[0][0] as { avatar: { traits: ReturnType<typeof seededTraits> } }
      expect(body.avatar.traits.tile).not.toBe(seededTraits(name!).tile)
    })

    it('the re-roll badge shows the shared tooltip on hover, and the tile keeps its name', async () => {
      card()
      await screen.findByTestId('crewmate-create-embedded')
      const tile = screen.getByTestId('crewmate-create-look')
      expect(tile).toHaveAccessibleName('Try another look')
      fireEvent.mouseEnter(screen.getByTestId('crewmate-create-look-reroll'))
      expect(await screen.findByRole('tooltip')).toHaveTextContent('Try another look')
      fireEvent.mouseLeave(screen.getByTestId('crewmate-create-look-reroll'))
      await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
    })

    it('startExpanded opens with Advanced settings already unfolded, on every open', async () => {
      const { rerender } = card({ startExpanded: true })
      await screen.findByRole('combobox', { name: 'Built from' })
      expect(toggle()).toHaveAttribute('aria-expanded', 'true')
      fireEvent.click(toggle())
      rerender(<NewCrewmateDialog open={false} embedded firstGreeting startExpanded {...props} />)
      rerender(<NewCrewmateDialog open embedded firstGreeting startExpanded {...props} />)
      expect(await screen.findByRole('combobox', { name: 'Built from' })).toBeInTheDocument()
    })

    it('reads top to bottom: look, caption, name, hint, Create, then the Advanced settings toggle last', async () => {
      card()
      const region = await screen.findByRole('region', { name: 'New crewmate' })
      // Document order, read off one walk of the card.
      const all = Array.from(region.querySelectorAll('*'))
      const at = (el: Element) => all.indexOf(el)
      const order = [
        screen.getByRole('heading', { name: 'New crewmate' }),
        screen.getByTestId('crewmate-create-look'),
        screen.getByTestId('crewmate-create-look-caption'),
        screen.getByRole('textbox', { name: 'Name' }),
        screen.getByTestId('crewmate-create-quick-hint'),
        screen.getByTestId('crewmate-create-submit'),
        screen.getByTestId('crewmate-create-secondary'),
      ]
      for (const el of order) expect(at(el)).toBeGreaterThan(-1)
      for (let i = 1; i < order.length; i++) expect(at(order[i])).toBeGreaterThan(at(order[i - 1]))
      // Create sits right after the hint, and the folded card ends with the
      // secondary line: its toggle is the last control, and no second Create.
      expect(screen.getByTestId('crewmate-create-submit').previousElementSibling).toBe(screen.getByTestId('crewmate-create-quick-hint'))
      expect(within(region).getAllByRole('button').at(-1)).toBe(toggle())
      expect(screen.queryByTestId('crewmate-create-submit-end')).toBeNull()
      expect(screen.getByTestId('crewmate-create-submit')).toHaveClass('w-full')
    })

    it('the secondary line under Create holds only the centred Advanced settings toggle, chevron before its label', async () => {
      card()
      const line = await screen.findByTestId('crewmate-create-secondary')
      expect(line).toHaveClass('justify-center')
      expect(within(line).getAllByRole('button')).toEqual([toggle()])
      expect(within(line).queryByRole('button', { name: 'Cancel' })).toBeNull()
      expect(toggle().firstElementChild).toBe(screen.getByTestId('crewmate-create-advanced-chevron'))
      expect(toggle().lastElementChild).toHaveTextContent('Advanced settings')
    })

    it('the way out is an X beside the heading, outside the form', async () => {
      card()
      const header = await screen.findByTestId('crewmate-create-header')
      const close = screen.getByRole('button', { name: 'Cancel' })
      expect(close).toBe(screen.getByTestId('crewmate-create-close'))
      expect(Array.from(header.children)).toEqual([screen.getByRole('heading', { name: 'New crewmate' }), close])
      expect(header).toHaveClass('justify-between')
      expect(close).toHaveAttribute('type', 'button')
      expect(screen.getByTestId('crewmate-create-form')).not.toContainElement(close)
      fireEvent.click(close)
      expect(props.onClose).toHaveBeenCalledTimes(1)
    })

    it('the modal door keeps only the modal\'s own X: no Cancel in the form', async () => {
      renderWithProviders(<NewCrewmateDialog open firstGreeting {...props} />)
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByRole('button', { name: 'Close' })).toBeInTheDocument()
      expect(within(dialog).queryByRole('button', { name: 'Cancel' })).toBeNull()
      expect(screen.queryByTestId('crewmate-create-close')).toBeNull()
    })

    it('every control is a 44px touch target on a phone', async () => {
      card()
      await screen.findByTestId('crewmate-create-embedded')
      for (const el of [toggle(), screen.getByTestId('crewmate-create-submit')]) {
        expect(el).toHaveClass('min-h-11')
      }
      expect(screen.getByTestId('crewmate-create-close')).toHaveClass('size-11', 'sm:size-8')
    })

    it('Advanced settings unfolds directly under its toggle and ends with a second Create', async () => {
      card()
      const region = await screen.findByRole('region', { name: 'New crewmate' })
      fireEvent.click(toggle())
      const advanced = screen.getByTestId('crewmate-create-advanced')
      const all = Array.from(region.querySelectorAll('*'))
      const at = (el: Element) => all.indexOf(el)
      expect(at(advanced)).toBeGreaterThan(at(screen.getByTestId('crewmate-create-secondary')))
      expect(at(screen.getByTestId('crewmate-create-submit'))).toBeLessThan(at(screen.getByTestId('crewmate-create-secondary')))
      expect(advanced.previousElementSibling).toBe(screen.getByTestId('crewmate-create-secondary'))
      const end = screen.getByTestId('crewmate-create-submit-end')
      expect(advanced).toContainElement(end)
      expect(within(region).getAllByRole('button').at(-1)).toBe(end)
      expect(end).toHaveClass('w-full', 'min-h-11')
      expect(end).toHaveTextContent('Create crewmate')
    })

    it('the closing Create submits the same create, once per press', async () => {
      vi.mocked(api.createKirocrewAgent).mockImplementationOnce(() => new Promise(() => {}))
      card()
      await screen.findByTestId('crewmate-create-embedded')
      typeName('Scout')
      fireEvent.click(toggle())
      fireEvent.click(screen.getByTestId('crewmate-create-submit-end'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      expect(vi.mocked(api.createKirocrewAgent).mock.calls[0][0]).toMatchObject({ name: 'Scout', kiro_agent: 'kirocrew' })
      // In flight: both Create buttons are locked, so no second POST leaves.
      await waitFor(() => expect(screen.getByTestId('crewmate-create-submit-end')).toBeDisabled())
      expect(screen.getByTestId('crewmate-create-submit')).toBeDisabled()
      fireEvent.click(screen.getByTestId('crewmate-create-submit-end'))
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    })

    it('Cancel, the toggle and the look are locked with the form while a create is in flight', async () => {
      vi.mocked(api.createKirocrewAgent).mockImplementationOnce(() => new Promise(() => {}))
      card()
      await screen.findByTestId('crewmate-create-embedded')
      typeName('Scout')
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(toggle()).toBeDisabled())
      expect(screen.getByTestId('crewmate-create-look')).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
      expect(screen.getByTestId('crewmate-create-submit')).toBeDisabled()
    })

    it('the hint is shown only where a first greeting is asked for', async () => {
      renderWithProviders(<NewCrewmateDialog open embedded {...props} />)
      await screen.findByTestId('crewmate-create-embedded')
      expect(screen.queryByTestId('crewmate-create-quick-hint')).toBeNull()
    })

    it('a written job turns the hint from "will ask" into "will confirm this job"', async () => {
      card()
      const hint = await screen.findByTestId('crewmate-create-quick-hint')
      expect(hint).toHaveTextContent("your crewmate will ask what you'd like it to do")
      fireEvent.click(toggle())
      fireEvent.change(screen.getByRole('textbox', { name: 'What it looks after' }), { target: { value: 'Watch PRs' } })
      expect(hint).toHaveTextContent('your crewmate will confirm this job with you')
      fireEvent.change(screen.getByRole('textbox', { name: 'What it looks after' }), { target: { value: '  ' } })
      expect(hint).toHaveTextContent("your crewmate will ask what you'd like it to do")
    })
  })

  it.each([false, true])('a typed draft asks before leaving and before unload (embedded=%s); a clean form does not', async (embedded) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <Probe />
        <NewCrewmateDialog open embedded={embedded} {...props} />
      </NavigationLeaveGuardProvider>,
    )
    await screen.findByTestId('crewmate-create-form')
    expect(mayLeave()).toBe(true)
    expect(unloadPrevented()).toBe(false)
    typeName('Scout')
    expect(unloadPrevented()).toBe(true)
    expect(mayLeave()).toBe(false)
    expect(confirm).toHaveBeenCalledTimes(1)
  })
})

describe('NewCrewmateDialog opened from a proposal', () => {
  beforeEach(() => {
    props.onClose = vi.fn()
    props.onCreated = vi.fn()
    vi.mocked(api.createKirocrewAgent).mockClear()
  })
  afterEach(() => { vi.restoreAllMocks() })
  const goalField = () => screen.queryByRole('textbox', { name: 'What it looks after' }) as HTMLTextAreaElement | null

  it('fills the name, and the goal with Advanced settings open so the goal is in view', async () => {
    renderWithProviders(<NewCrewmateDialog open embedded initialDraft={{ name: 'Scout', goal: 'Watch CI' }} {...props} />)
    await screen.findByTestId('crewmate-create-form')
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Scout')
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(goalField()).toHaveValue('Watch CI')
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.createKirocrewAgent).mock.calls[0][0]).toMatchObject({ name: 'Scout', description: 'Watch CI' })
  })

  it('a proposal with no goal keeps Advanced settings folded', async () => {
    renderWithProviders(<NewCrewmateDialog open embedded initialDraft={{ name: 'Scout', goal: '  ' }} {...props} />)
    await screen.findByTestId('crewmate-create-form')
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Scout')
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'false')
  })

  it('an untouched proposal leaves without asking and reports itself unedited; an edit is the user\'s draft', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const states: { edited: boolean; busy: boolean }[] = []
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <Probe />
        <NewCrewmateDialog open embedded initialDraft={{ name: 'Scout', goal: 'Watch CI' }} onDraftStateChange={(st) => states.push(st)} {...props} />
      </NavigationLeaveGuardProvider>,
    )
    await screen.findByTestId('crewmate-create-form')
    expect(mayLeave()).toBe(true)
    expect(unloadPrevented()).toBe(false)
    expect(states.at(-1)).toEqual({ edited: false, busy: false })
    fireEvent.change(goalField()!, { target: { value: 'Watch CI and the release' } })
    await waitFor(() => expect(states.at(-1)).toEqual({ edited: true, busy: false }))
    expect(unloadPrevented()).toBe(true)
    expect(mayLeave()).toBe(false)
    expect(confirm).toHaveBeenCalledTimes(1)
  })

  it('an untouched proposal whose New workspace form holds input reports itself edited, and unedited once that form is closed', async () => {
    const states: { edited: boolean; busy: boolean }[] = []
    renderWithProviders(
      <NewCrewmateDialog open embedded initialDraft={{ name: 'Scout', goal: 'Watch CI' }} onDraftStateChange={(st) => states.push(st)} {...props} />,
    )
    await screen.findByTestId('crewmate-create-form')
    const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
    fireEvent.keyDown(workspace, { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    const wsName = await screen.findByPlaceholderText('e.g. oncall')
    expect(states.at(-1)).toEqual({ edited: false, busy: false })
    // The card's own fields are as proposed; the typed workspace name is the user's.
    fireEvent.change(wsName, { target: { value: 'staging' } })
    await waitFor(() => expect(states.at(-1)).toEqual({ edited: true, busy: false }))
    const cancels = screen.getAllByRole('button', { name: 'Cancel' })
    fireEvent.click(cancels[cancels.length - 1])
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    await waitFor(() => expect(states.at(-1)).toEqual({ edited: false, busy: false }))
  })

  it('a new proposal object re-fills an open card', async () => {
    const { rerender } = renderWithProviders(<NewCrewmateDialog open embedded initialDraft={{ name: 'Scout', goal: '' }} {...props} />)
    await screen.findByTestId('crewmate-create-form')
    typeName('Edited')
    rerender(<NewCrewmateDialog open embedded initialDraft={{ name: 'Radar', goal: 'Triage issues' }} {...props} />)
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Radar'))
    expect(goalField()).toHaveValue('Triage issues')
  })

  it('only the guided door carries the guide anchor on its Create', async () => {
    const { unmount } = renderWithProviders(<NewCrewmateDialog open embedded {...props} />)
    await screen.findByTestId('crewmate-create-form')
    expect(screen.getByTestId('crewmate-create-submit')).not.toHaveAttribute('data-guide-anchor')
    unmount()
    renderWithProviders(<NewCrewmateDialog open embedded guided {...props} />)
    await screen.findByTestId('crewmate-create-form')
    expect(screen.getByTestId('crewmate-create-submit')).toHaveAttribute('data-guide-anchor', 'crewmate.create')
    expect(document.querySelectorAll('[data-guide-anchor]')).toHaveLength(1)
  })
})
