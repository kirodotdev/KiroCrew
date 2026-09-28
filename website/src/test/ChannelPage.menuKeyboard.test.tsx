import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

const mockChannel = {
  id: 'ch1',
  topic: 'Test Channel',
  members: {
    a1: { id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1' },
  },
  messages: [],
}

/**
 * The listen-mode dropdown in the agents panel declares role="menu", which
 * promises the WAI-ARIA menu keyboard contract (arrows move focus between the
 * rows and wrap, Home/End jump to the boundaries, Tab is contained while the
 * menu is open). None of that existed here: the rows were reachable only by
 * mouse — the defect class #6231 fixed on its five inventoried surfaces via
 * the shared useMenuKeyboard hook; this surface was outside that inventory
 * (#6269).
 */
describe('ChannelPage — listen-mode menu keyboard contract', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(mockChannel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
    vi.mocked(api).channelUpdateAgent = vi.fn().mockResolvedValue({ ok: true })
  })

  const menuEl = () => document.querySelector('[role="menu"]') as HTMLElement | null
  /** The three listen-mode rows, in document order (all / mention / silent). */
  const rowsOf = () => Array.from(menuEl()!.querySelectorAll<HTMLButtonElement>('button'))

  /**
   * Open the agents sidebar, then the listen-mode menu of the single agent
   * row. Returns the menu trigger, grabbed BEFORE opening the menu so it is
   * unambiguous.
   */
  async function openListenMenu() {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    // The trigger renders the current listen-mode badge ("mention" for this
    // fixture) inside the agent row.
    const trigger = (await screen.findByText('mention', { selector: 'span' })).closest('button') as HTMLButtonElement
    fireEvent.click(trigger)
    await waitFor(() => expect(menuEl()).toBeTruthy())
    return trigger
  }

  it('moves focus into the menu when it opens', async () => {
    await openListenMenu()
    // role="menu" tells assistive tech that focus is managed inside the menu,
    // so opening must land the user there rather than leaving focus on the
    // trigger with the menu an unreachable island.
    expect(menuEl()!.contains(document.activeElement)).toBe(true)
    expect(rowsOf()[0]).toHaveFocus()
  })

  it('walks the rows with ArrowDown and wraps past the last one', async () => {
    await openListenMenu()
    const rows = rowsOf()
    expect(rows).toHaveLength(3) // all / mention / silent
    rows[0].focus()
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    expect(rows[1]).toHaveFocus()
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    expect(rows[0]).toHaveFocus() // wraps, rather than dead-ending
  })

  it('walks the rows with ArrowUp and wraps past the first one', async () => {
    await openListenMenu()
    const rows = rowsOf()
    rows[0].focus()
    fireEvent.keyDown(document, { key: 'ArrowUp' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document, { key: 'ArrowUp' })
    expect(rows[1]).toHaveFocus()
  })

  it('jumps to the boundary rows with Home and End', async () => {
    await openListenMenu()
    const rows = rowsOf()
    rows[1].focus()
    fireEvent.keyDown(document, { key: 'End' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Home' })
    expect(rows[0]).toHaveFocus()
  })

  it('contains Tab and Shift-Tab inside the open menu', async () => {
    // #2533: a Tab out of a still-open menu drops a keyboard user behind it
    // with no obvious way back.
    await openListenMenu()
    const rows = rowsOf()
    rows[2].focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(rows[0]).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(rows[2]).toHaveFocus()
  })

  it('closes on Escape and hands focus back to the trigger', async () => {
    // "Escape closes" is a pin on existing behaviour; the focus restore is the
    // new part. Focus now ENTERS the menu on open, so closing without a
    // restore would orphan focus on <body> and lose the user's place.
    const trigger = await openListenMenu()
    expect(trigger).not.toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(menuEl()).toBeNull()
    expect(trigger).toHaveFocus()
  })

  it('hands focus back to the trigger after picking a row', async () => {
    const trigger = await openListenMenu()
    const rows = rowsOf()
    // Pick "all" (row 0); the pick still reports upstream.
    fireEvent.click(rows[0])
    await waitFor(() => expect(vi.mocked(api).channelUpdateAgent).toHaveBeenCalledWith('ch1', 'a1', { listen: 'all' }))
    expect(menuEl()).toBeNull()
    // The focused row is unmounted by the close, so without a restore focus
    // falls to <body> and the next Tab restarts from the top of the document.
    expect(trigger).toHaveFocus()
  })

  it('marks the rows as menuitemradio with the current mode checked', async () => {
    // The current mode was conveyed only by colour; aria-checked is the only
    // thing assistive tech can perceive — assert the programmatic state.
    await openListenMenu()
    const radios = Array.from(menuEl()!.querySelectorAll('[role="menuitemradio"]'))
    expect(radios).toHaveLength(3)
    expect(radios.map(r => r.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false'])
  })

  it('names the menu so the radio group has a referent', async () => {
    // Focus entry lands an AT user inside the menu with no surrounding row
    // context, so "menu, 3 items" of bare mode words ("all"/"mention"/
    // "silent") needs a group name saying what they control.
    await openListenMenu()
    expect(menuEl()!.getAttribute('aria-label')).toBe('Listen mode')
  })

  it('dismisses on a mousedown in the composer without stealing focus', async () => {
    // The host's outside-mousedown dismissal is what makes "menu open while
    // an outside editor holds the caret" unreachable (the hook's document
    // listener assumes at most one open menu and defers to outside editable
    // targets — that guard is pinned in the hook's own test). Assert the
    // reachable invariant: interacting with the composer closes the menu, and
    // outside-pointer dismissal does NOT restore focus to the trigger (the
    // browser routes focus per the click target).
    const trigger = await openListenMenu()
    const composer = document.querySelector('textarea') as HTMLTextAreaElement
    expect(composer).toBeTruthy()
    fireEvent.mouseDown(composer)
    expect(menuEl()).toBeNull()
    expect(trigger).not.toHaveFocus()
  })
})
