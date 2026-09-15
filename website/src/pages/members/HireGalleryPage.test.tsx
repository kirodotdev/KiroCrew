import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'

/* The hire gallery (design step 6): one listing for every template a crewmate
 * can be hired from, scenario chips, a detail layer, a Hire that always asks
 * for a name before anything is created, and a secondary action that follows
 * the card's crewmate count (none / Chat with <name> / Your crewmates (N)). */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberTemplates: vi.fn(),
      hireMember: vi.fn(),
      members: vi.fn(() => Promise.resolve({ members: [{ name: 'Scribe', display_name: 'Minutes taker', role: 'Scribe' }, { name: 'Nia', display_name: 'Nia' }, { name: 'Nia-2', display_name: 'Nia' }] })),
    },
  }
})

import { api, type HireTemplateCard } from '../../api/client'
import HireGalleryPage, { categoriesPresent, filterCards, hireName } from './HireGalleryPage'

function card(over: Partial<HireTemplateCard>): HireTemplateCard {
  return {
    id: 'local:reviewer',
    origin: 'local',
    source: { kind: 'local', agent: 'reviewer' },
    role: 'Reviewer',
    duty: 'Reviews pull requests.',
    description: '',
    tags: [],
    category: 'other',
    starter_prompts: [],
    avatar: null,
    publisher: '',
    version: '',
    agent: 'reviewer',
    capabilities: [],
    hired_as: [],
    hireable: true,
    unavailable_code: '',
    unavailable_reason: '',
    ...over,
  }
}

const APP_CARD = card({
  id: 'app:oncall-pack/agents/triage.json',
  origin: 'app',
  source: { kind: 'store', app: 'oncall-pack', agent: 'agents/triage.json' },
  role: 'Oncall Triage Engineer',
  duty: 'Triages every page, correlates it with deploys.',
  description: 'Owns a paging queue end to end. Never rolls back without an ack.',
  tags: ['Incident triage', 'Deploy correlation', 'Rollback plans', 'Fourth tag'],
  category: 'ops',
  starter_prompts: [{ text: 'What paged overnight?' }, { text: 'Draft a rollback plan.', attachment: 'incident.md' }],
  avatar: { kind: 'ghost', traits: { eyes: 'visor' } },
  publisher: 'Oncall pack',
  version: '1.2.0',
  agent: 'triage',
  capabilities: [{ kind: 'mcp', name: 'pagerduty' }, { kind: 'skill', name: 'deployment-fixer' }],
})
const BUILTIN_CARD = card({
  id: 'builtin:pipeline-conductor',
  origin: 'builtin',
  source: { kind: 'local', agent: 'pipeline-conductor' },
  role: 'Pipeline Conductor',
  duty: 'Runs one issue-to-PR pipeline as a supervised fleet.',
  category: 'engineering',
  publisher: 'Release Desk',
})
const HIRED_CARD = card({ id: 'local:scribe', source: { kind: 'local', agent: 'scribe' }, role: 'Scribe', hired_as: [{ id: 'Scribe', display_name: 'Minutes taker' }] })
/** Two crewmates from one card, carrying the SAME label: the picker must tell them apart. */
const TWICE_CARD = card({
  id: 'local:reviewer',
  source: { kind: 'local', agent: 'reviewer' },
  role: 'Reviewer',
  category: 'engineering',
  hired_as: [{ id: 'Nia', display_name: 'Nia' }, { id: 'Nia-2', display_name: 'Nia' }],
})
const OFF_CARD = card({
  id: 'app:ghost/agents/x.json',
  origin: 'app',
  source: { kind: 'store', app: 'ghost', agent: 'agents/x.json' },
  role: 'Ghost',
  hireable: false,
  unavailable_code: 'template_not_materialized',
  unavailable_reason: "App 'ghost' has not installed its agent 'x' yet; re-enable the app",
})

function LocationProbe() {
  const loc = useLocation()
  return <span data-testid="location">{loc.pathname + loc.search}</span>
}

function renderGallery() {
  return renderWithProviders(
    <>
      <Routes>
        <Route path="/members/hire" element={<HireGalleryPage />} />
        <Route path="/members" element={<div data-testid="roster">roster</div>} />
      </Routes>
      <LocationProbe />
    </>,
    { route: '/members/hire' },
  )
}

function cardEl(cards: HTMLElement[], id: string): HTMLElement {
  return cards.find((c) => c.getAttribute('data-card-id') === id)!
}

/** Click Hire on a card, type a name, confirm; the naming dialog. */
async function hireThrough(el: HTMLElement, name: string) {
  fireEvent.click(within(el).getByTestId('hire-button'))
  const dialog = await screen.findByTestId('hire-name-dialog')
  fireEvent.change(within(dialog).getByTestId('hire-name-input'), { target: { value: name } })
  fireEvent.click(within(dialog).getByTestId('hire-name-confirm'))
  return dialog
}

describe('filterCards / categoriesPresent / hireName', () => {
  it('files by category, searches role, duty, tags and publisher, and offers only present chips', () => {
    const all = [APP_CARD, BUILTIN_CARD, HIRED_CARD]
    expect(filterCards(all, 'all', '').map((c) => c.id)).toEqual(all.map((c) => c.id))
    expect(filterCards(all, 'ops', '').map((c) => c.id)).toEqual([APP_CARD.id])
    expect(filterCards(all, 'other', '').map((c) => c.id)).toEqual([HIRED_CARD.id])
    expect(filterCards(all, 'all', 'rollback').map((c) => c.id)).toEqual([APP_CARD.id])
    expect(filterCards(all, 'all', 'release desk').map((c) => c.id)).toEqual([BUILTIN_CARD.id])
    expect(filterCards(all, 'engineering', 'oncall')).toEqual([])
    expect(categoriesPresent(all)).toEqual(['engineering', 'ops', 'other'])
    expect(categoriesPresent([card({ category: '' })])).toEqual(['other'])
  })

  it('hireName collapses whitespace and treats a blank as no name', () => {
    expect(hireName('  Pager   triage ')).toBe('Pager triage')
    expect(hireName('   ')).toBe('')
    expect(hireName('')).toBe('')
  })
})

describe('HireGalleryPage', () => {
  beforeEach(() => {
    vi.mocked(api.memberTemplates).mockReset()
    vi.mocked(api.hireMember).mockReset()
    vi.mocked(api.memberTemplates).mockResolvedValue({ templates: [APP_CARD, BUILTIN_CARD, HIRED_CARD, OFF_CARD, TWICE_CARD] })
  })

  it('lists cards from all sources with role, duty, three tags and the actions the crewmate count calls for', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    expect(cards).toHaveLength(5)
    const app = cardEl(cards, APP_CARD.id)
    expect(app).toHaveTextContent('Oncall Triage Engineer')
    expect(app).toHaveTextContent('Triages every page, correlates it with deploys.')
    // Three tags on the face, never the fourth; no publisher/version on the face.
    expect(app).toHaveTextContent('Rollback plans')
    expect(app).not.toHaveTextContent('Fourth tag')
    expect(app).not.toHaveTextContent('Oncall pack')
    // No crewmate yet: Hire alone. One: Hire stays primary with "Chat with
    // <name>" beside it. Two: "Your crewmates (2)". A built-in: a plain Hire.
    // Off: disabled with why.
    expect(within(app).getByTestId('hire-button')).toHaveTextContent('Hire')
    expect(within(app).queryByTestId('hire-chat-with')).toBeNull()
    expect(within(app).queryByTestId('hire-your-crewmates')).toBeNull()
    const hired = cardEl(cards, HIRED_CARD.id)
    expect(within(hired).getByTestId('hire-button')).toHaveTextContent('Hire')
    expect(within(hired).getByTestId('hire-chat-with')).toHaveTextContent('Chat with Minutes taker')
    expect(within(hired).queryByTestId('hire-your-crewmates')).toBeNull()
    const twice = cardEl(cards, TWICE_CARD.id)
    expect(within(twice).getByTestId('hire-button')).toHaveTextContent('Hire')
    expect(within(twice).getByTestId('hire-your-crewmates')).toHaveTextContent('Your crewmates (2)')
    expect(within(twice).queryByTestId('hire-chat-with')).toBeNull()
    const builtin = cardEl(cards, BUILTIN_CARD.id)
    expect(within(builtin).getByTestId('hire-button')).toHaveTextContent('Hire')
    expect(within(builtin).getByTestId('hire-button')).toBeEnabled()
    const off = cardEl(cards, OFF_CARD.id)
    expect(within(off).getByTestId('hire-button')).toBeDisabled()
    // The reason in task language, naming the destination -- not the server's
    // mechanism sentence ("has not installed its agent 'x' yet; re-enable the app")
    // -- on the card FACE (a touch reader never sees the button's title).
    expect(within(off).getByTestId('hire-card-unavailable')).toHaveTextContent('ghost hasn’t finished setting up this role. Use “Browse apps” above, turn ghost off and on again, then come back — turning it off and on doesn’t affect crewmates you already hired.')
    expect(within(off).getByTestId('hire-button')).toHaveAttribute('title', 'ghost hasn’t finished setting up this role. Use “Browse apps” above, turn ghost off and on again, then come back — turning it off and on doesn’t affect crewmates you already hired.')
    expect(within(app).queryByTestId('hire-card-unavailable')).toBeNull()
    // The title, the introductory sentence and the apps link.
    expect(screen.getByText('Hire Crewmates')).toBeInTheDocument()
    expect(
      screen.getByText('Each crewmate is a persistent AI teammate with its own skills, memory, permissions, and schedule.'),
    ).toBeInTheDocument()
    expect(screen.getByTestId('hire-browse-apps')).toHaveAttribute('href', '/apps')
  })

  it('scenario chips filter the grid and only present scenarios are offered', async () => {
    renderGallery()
    await screen.findAllByTestId('hire-template-card')
    const chips = within(screen.getByTestId('hire-category-chips')).getAllByRole('radio')
    expect(chips.map((c) => c.textContent)).toEqual(['All', 'Engineering', 'Ops & Incidents', 'Other'])
    fireEvent.click(screen.getByRole('radio', { name: 'Ops & Incidents' }))
    const shown = screen.getAllByTestId('hire-template-card')
    expect(shown).toHaveLength(1)
    expect(shown[0]).toHaveTextContent('Oncall Triage Engineer')
    fireEvent.change(screen.getByLabelText('Search roles'), { target: { value: 'zzz' } })
    expect(screen.getByTestId('hire-no-match')).toBeInTheDocument()
  })

  it('gate: Hire asks for a name, creates nothing until it is confirmed, and lands in the new crewmate’s thread', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: true, id: 'Pager-triage' })
    const { queryClient } = renderGallery()
    // The installed-agent list the drawer's Capabilities reads was fetched
    // before the hire: it must be marked stale by it, or the new copy's
    // capabilities read as "none" until a reload.
    queryClient.setQueryData(['agents-installed'], [])
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(within(cardEl(cards, APP_CARD.id)).getByTestId('hire-button'))
    const dialog = await screen.findByTestId('hire-name-dialog')
    expect(dialog).toHaveTextContent('Name your new crewmate')
    expect(dialog).toHaveTextContent('New hire: Oncall Triage Engineer')
    // The role is the placeholder, never the value: a blank cannot be confirmed.
    const input = within(dialog).getByTestId('hire-name-input') as HTMLInputElement
    expect(input.value).toBe('')
    expect(input).toHaveAttribute('placeholder', 'Oncall Triage Engineer')
    expect(input).toHaveFocus()
    expect(within(dialog).getByTestId('hire-name-confirm')).toBeDisabled()
    expect(dialog).toHaveTextContent('A name is required to hire.')
    fireEvent.change(input, { target: { value: '   ' } })
    expect(within(dialog).getByTestId('hire-name-confirm')).toBeDisabled()
    fireEvent.submit(input.closest('form')!)
    expect(api.hireMember).not.toHaveBeenCalled()
    // Typing enables Hire; still nothing has been posted.
    fireEvent.change(input, { target: { value: '  Pager   triage ' } })
    expect(within(dialog).getByTestId('hire-name-confirm')).toBeEnabled()
    expect(dialog).toHaveTextContent('You can rename it later from its chat.')
    expect(api.hireMember).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByTestId('hire-name-confirm'))
    await waitFor(() => expect(api.hireMember).toHaveBeenCalledTimes(1))
    // The source AND the name the owner typed (whitespace collapsed).
    expect(vi.mocked(api.hireMember).mock.calls[0][0]).toEqual({ source: APP_CARD.source, display_name: 'Pager triage' })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Pager-triage'))
    expect(screen.getByTestId('roster')).toBeInTheDocument()
    expect(queryClient.getQueryState(['agents-installed'])?.isInvalidated).toBe(true)
  })

  it('gate: cancelling the naming step writes nothing, and the next card gets a fresh field', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const app = cardEl(cards, APP_CARD.id)
    fireEvent.click(within(app).getByTestId('hire-button'))
    let dialog = await screen.findByTestId('hire-name-dialog')
    fireEvent.change(within(dialog).getByTestId('hire-name-input'), { target: { value: 'Pager triage' } })
    fireEvent.click(within(dialog).getByTestId('hire-name-cancel'))
    await waitFor(() => expect(screen.queryByTestId('hire-name-dialog')).toBeNull())
    expect(api.hireMember).not.toHaveBeenCalled()
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
    // Escape cancels the same way.
    fireEvent.click(within(app).getByTestId('hire-button'))
    dialog = await screen.findByTestId('hire-name-dialog')
    fireEvent.change(within(dialog).getByTestId('hire-name-input'), { target: { value: 'Pager triage' } })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('hire-name-dialog')).toBeNull())
    expect(api.hireMember).not.toHaveBeenCalled()
    // A name typed for one card is not handed to another card's dialog.
    fireEvent.click(within(cardEl(cards, BUILTIN_CARD.id)).getByTestId('hire-button'))
    dialog = await screen.findByTestId('hire-name-dialog')
    expect((within(dialog).getByTestId('hire-name-input') as HTMLInputElement).value).toBe('')
    expect(within(dialog).getByTestId('hire-name-input')).toHaveAttribute('placeholder', 'Pipeline Conductor')
  })

  it('gate: a card with one crewmate offers "Chat with <name>", which opens that crewmate by id and hires nothing', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(within(cardEl(cards, HIRED_CARD.id)).getByTestId('hire-chat-with'))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Scribe'))
    expect(api.hireMember).not.toHaveBeenCalled()
    expect(screen.queryByTestId('hire-name-dialog')).toBeNull()
  })

  it('gate: two crewmates open a picker; nothing navigates until one is chosen, and two sharing a label are told apart by id', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const twice = cardEl(cards, TWICE_CARD.id)
    fireEvent.click(within(twice).getByTestId('hire-your-crewmates'))
    const picker = await screen.findByTestId('hire-crewmate-picker')
    expect(picker).toHaveTextContent('Your crewmates from Reviewer')
    expect(picker).toHaveTextContent('2 crewmates were hired from this template.')
    const rows = within(picker).getAllByTestId('hire-pick-crewmate')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Nia')
    expect(rows[0]).toHaveAttribute('data-member-id', 'Nia')
    expect(rows[1]).toHaveTextContent('Nia')
    expect(rows[1]).toHaveAttribute('data-member-id', 'Nia-2')
    // Opening the picker navigated nowhere and hired nothing.
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
    expect(api.hireMember).not.toHaveBeenCalled()
    // Escape leaves the gallery where it was.
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('hire-crewmate-picker')).toBeNull())
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
    // An explicit choice of the SECOND crewmate opens that one, by its id.
    fireEvent.click(within(twice).getByTestId('hire-your-crewmates'))
    const again = await screen.findByTestId('hire-crewmate-picker')
    fireEvent.click(within(again).getAllByTestId('hire-pick-crewmate')[1])
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Nia-2'))
    expect(api.hireMember).not.toHaveBeenCalled()
  })

  it('a crewmate fired since the catalog was read is said, not opened or swapped for another', async () => {
    vi.mocked(api.members).mockResolvedValueOnce({ members: [{ name: 'Nia', display_name: 'Nia' }] } as never)
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(within(cardEl(cards, TWICE_CARD.id)).getByTestId('hire-your-crewmates'))
    const picker = await screen.findByTestId('hire-crewmate-picker')
    fireEvent.click(within(picker).getAllByTestId('hire-pick-crewmate')[1])
    const notice = await screen.findByTestId('hire-chat-error')
    expect(notice).toHaveTextContent('Nia isn’t one of your crewmates anymore.')
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
    expect(screen.queryByTestId('hire-crewmate-picker')).toBeNull()
    // The catalog is re-read so the count on the card catches up.
    await waitFor(() => expect(api.memberTemplates).toHaveBeenCalledTimes(2))
  })

  it('a click on the card body opens the detail layer, whose footer carries the same Hire and chat actions', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(cardEl(cards, APP_CARD.id))
    const detail = await screen.findByTestId('hire-template-detail')
    expect(detail).toHaveTextContent('Owns a paging queue end to end. Never rolls back without an ack.')
    expect(detail).toHaveTextContent('Fourth tag')
    const starters = within(detail).getByTestId('hire-detail-starters')
    // Previews, not controls: a row that read like a question must not commit a
    // hire, and it does not wear the thread's bordered Ask-row shape either.
    expect(within(starters).getAllByTestId('hire-detail-starter')).toHaveLength(2)
    expect(within(starters).queryAllByRole('button')).toHaveLength(0)
    expect(within(starters).getAllByTestId('hire-detail-starter')[0].className).not.toMatch(/rounded-lg|bg-bg-elevated/)
    expect(api.hireMember).not.toHaveBeenCalled()
    expect(starters).toHaveTextContent('incident.md')
    expect(within(detail).getByTestId('hire-detail-caps-toggle')).toHaveTextContent('Built-in capabilities 2')
    expect(within(detail).getByTestId('hire-detail-caps')).toHaveTextContent('pagerduty')
    // Kind tags in plain words at the moment of the hire decision, not "MCP" / "SKILL".
    expect(within(detail).getByTestId('hire-detail-caps')).toHaveTextContent('Tool server')
    expect(within(detail).getByTestId('hire-detail-caps')).toHaveTextContent('Skill')
    expect(within(detail).getByTestId('hire-detail-caps')).not.toHaveTextContent('MCP')
    fireEvent.click(within(detail).getByTestId('hire-detail-caps-toggle'))
    expect(within(detail).queryByTestId('hire-detail-caps')).toBeNull()
    // The quiet meta line: publisher · version · origin.
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('Oncall pack')
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('v1.2.0')
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('Installed app')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('hire-template-detail')).toBeNull())
    // The detail of a hired card: Hire primary, Chat with beside it.
    fireEvent.click(cardEl(screen.getAllByTestId('hire-template-card'), HIRED_CARD.id))
    const hiredDetail = await screen.findByTestId('hire-template-detail')
    expect(within(hiredDetail).getByTestId('hire-button')).toHaveTextContent('Hire')
    fireEvent.click(within(hiredDetail).getByTestId('hire-chat-with'))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Scribe'))
    expect(api.hireMember).not.toHaveBeenCalled()
  })

  it('gate: a card that already has a crewmate hires a second one the same way -- by name', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: true, id: 'Scribe-2' })
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    // The naming step says this ADDS one: the card already has a crewmate, and
    // a reader at the dialog could not otherwise tell a second hire from a duplicate.
    fireEvent.click(within(cardEl(cards, HIRED_CARD.id)).getByTestId('hire-button'))
    const another = await screen.findByTestId('hire-name-dialog')
    expect(another).toHaveTextContent('you already have 1 crewmate hired from this template')
    fireEvent.click(within(another).getByTestId('hire-name-cancel'))
    await waitFor(() => expect(screen.queryByTestId('hire-name-dialog')).toBeNull())
    await hireThrough(cardEl(cards, HIRED_CARD.id), 'Second scribe')
    await waitFor(() => expect(api.hireMember).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.hireMember).mock.calls[0][0]).toEqual({ source: HIRED_CARD.source, display_name: 'Second scribe' })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/members?member=Scribe-2'))
    // A card that cannot be hired right now keeps its chat action but not its Hire.
    vi.mocked(api.memberTemplates).mockResolvedValue({ templates: [{ ...OFF_CARD, hired_as: [{ id: 'Ghost', display_name: 'Casper' }] }] })
    renderGallery()
    await waitFor(() => expect(screen.getAllByTestId('hire-chat-with').length).toBeGreaterThan(0))
    expect(screen.getAllByTestId('hire-button')[0]).toBeDisabled()
  })

  it('the detail says a hired card by the display name the catalog carries, and counts two', async () => {
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(cardEl(cards, HIRED_CARD.id))
    const detail = await screen.findByTestId('hire-template-detail')
    expect(within(detail).getByTestId('hire-detail-meta')).toHaveTextContent('Hired as Minutes taker')
    expect(within(detail).getByTestId('hire-detail-meta')).not.toHaveTextContent('Hired as Scribe')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('hire-template-detail')).toBeNull())
    fireEvent.click(cardEl(cards, TWICE_CARD.id))
    expect(within(await screen.findByTestId('hire-template-detail')).getByTestId('hire-detail-meta')).toHaveTextContent('Hired 2 times')
  })

  it('a refused hire is said in the naming dialog with the typed name kept, and nothing navigates', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: false, error: "'Reviewer' would share its member space with 'reviewer'", code: 'slug_collision' })
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const app = cardEl(cards, APP_CARD.id)
    const dialog = await hireThrough(app, 'Reviewer')
    const notice = await within(dialog).findByTestId('hire-name-error')
    expect(notice).toHaveTextContent('would share its member space')
    // No hand-off from inside the dialog: the retry is the same button, the name still in the field.
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect((within(dialog).getByTestId('hire-name-input') as HTMLInputElement).value).toBe('Reviewer')
    expect(within(dialog).getByTestId('hire-name-confirm')).toBeEnabled()
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
    // Cancelling drops the error with the dialog; the next attempt starts clean.
    fireEvent.click(within(dialog).getByTestId('hire-name-cancel'))
    await waitFor(() => expect(screen.queryByTestId('hire-name-dialog')).toBeNull())
    fireEvent.click(within(app).getByTestId('hire-button'))
    expect(within(await screen.findByTestId('hire-name-dialog')).queryByTestId('hire-name-error')).toBeNull()
  })

  it('Hire from the detail dialog opens the same naming step, and its refusal is said there', async () => {
    vi.mocked(api.hireMember).mockResolvedValue({ ok: false, error: 'Oncall pack is disabled', code: 'app_disabled' })
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(cardEl(cards, APP_CARD.id))
    const detail = await screen.findByTestId('hire-template-detail')
    fireEvent.click(within(detail).getByTestId('hire-button'))
    const dialog = await screen.findByTestId('hire-name-dialog')
    expect(api.hireMember).not.toHaveBeenCalled()
    // One layer at a time: the detail closed as the naming step opened, so the
    // detail's Hire is not on screen under the dialog's confirm.
    await waitFor(() => expect(screen.queryByTestId('hire-template-detail')).toBeNull())
    fireEvent.change(within(dialog).getByTestId('hire-name-input'), { target: { value: 'Pager' } })
    fireEvent.click(within(dialog).getByTestId('hire-name-confirm'))
    const notice = await within(dialog).findByTestId('hire-name-error')
    expect(notice).toHaveTextContent('Oncall pack is disabled')
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
  })

  it('a card wraps its action cluster under the text on a narrow viewport instead of crushing it', async () => {
    // jsdom lays nothing out; the contract is the flex model: the card wraps,
    // the text has a minimum basis, the actions can drop to their own row and
    // never force the role and duty to zero width.
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    const card = cardEl(cards, TWICE_CARD.id)
    expect(card.className).toContain('flex-wrap')
    const text = card.querySelector('.basis-40.min-w-0.flex-1')
    expect(text).not.toBeNull()
    const actions = within(card).getByTestId('hire-card-actions')
    expect(actions.className).toContain('ml-auto')
    expect(actions.className).toContain('max-w-full')
    expect(actions.firstElementChild?.className).toContain('flex-wrap')
  })

  it('the naming dialog and the picker are composed through the standard header, body and footer slots', async () => {
    // DialogContent carries no padding of its own: the inset and the close
    // button's clearance come from DialogHeader (px-5 py-3 pr-12), DialogBody
    // (px-5 py-4) and DialogFooter (px-5 py-3). Content placed straight into
    // it sat flush against the edges and under the close button.
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(within(cardEl(cards, APP_CARD.id)).getByTestId('hire-button'))
    const dialog = await screen.findByTestId('hire-name-dialog')
    const header = within(dialog).getByTestId('hire-name-header')
    const body = within(dialog).getByTestId('hire-name-body')
    const footer = within(dialog).getByTestId('hire-name-footer')
    for (const el of [header, body, footer]) expect(el.className).toContain('px-5')
    expect(header.className).toContain('pr-12')
    expect(header.className).toContain('py-3')
    expect(body.className).toContain('py-4')
    expect(footer.className).toContain('py-3')
    // Each slot holds what it should: title in the header (a fixed-height
    // strip, so the description lives in the body), field in the body, the two
    // buttons in the footer; the form is the flex column wrapping all three, so
    // the body is what scrolls under the 90vh cap and Enter still submits.
    expect(within(header).getByText('Name your new crewmate')).toBeInTheDocument()
    expect(body).toContainElement(document.getElementById('hire-name-description'))
    expect(body.className).toContain('overflow-y-auto')
    expect(within(body).getByTestId('hire-name-input')).toBeInTheDocument()
    const form = body.closest('form')!
    expect(form.className).toMatch(/min-h-0/)
    expect(form.className).toMatch(/flex-1/)
    expect(within(footer).getByTestId('hire-name-cancel')).toBeInTheDocument()
    expect(within(footer).getByTestId('hire-name-confirm')).toBeInTheDocument()
    expect(footer.closest('form')).toBe(within(body).getByTestId('hire-name-input').closest('form'))
    // Nothing sits between the content and the slots: the content's only
    // element children are the form and the close button.
    const direct = [...dialog.children].map((c) => c.tagName)
    expect(direct).toEqual(['FORM', 'BUTTON'])
    expect(within(dialog).getByRole('button', { name: /close/i })).toBeInTheDocument()
    fireEvent.click(within(dialog).getByTestId('hire-name-cancel'))
    await waitFor(() => expect(screen.queryByTestId('hire-name-dialog')).toBeNull())

    fireEvent.click(within(cardEl(cards, TWICE_CARD.id)).getByTestId('hire-your-crewmates'))
    const picker = await screen.findByTestId('hire-crewmate-picker')
    const ph = within(picker).getByTestId('hire-picker-header')
    const pb = within(picker).getByTestId('hire-picker-body')
    expect(ph.className).toContain('px-5')
    expect(ph.className).toContain('pr-12')
    expect(pb.className).toContain('px-5')
    expect(pb.className).toContain('overflow-y-auto')
    expect(pb).toContainElement(document.getElementById('hire-picker-description'))
    expect(within(pb).getByTestId('hire-crewmate-list')).toBeInTheDocument()
    expect([...picker.children].map((c) => c.getAttribute('data-testid') ?? c.tagName)).toEqual(['hire-picker-header', 'hire-picker-body', 'BUTTON'])
  })

  it('a catalog that cannot be read is an ErrorNotice', async () => {
    vi.mocked(api.memberTemplates).mockRejectedValue(new Error('boom'))
    renderGallery()
    expect(await screen.findByTestId('hire-catalog-error')).toBeInTheDocument()
  })

  it('a roster that cannot be read when a chat is opened is said with the hand-off, and nothing navigates', async () => {
    vi.mocked(api.members).mockRejectedValueOnce(new Error('boom'))
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(within(cardEl(cards, HIRED_CARD.id)).getByTestId('hire-chat-with'))
    const notice = await screen.findByTestId('hire-chat-error')
    expect(notice).toHaveTextContent(/roster could not be read/i)
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
  })

  it('a chat that fails from the detail layer or the picker closes that layer so the notice is not covered', async () => {
    // From the detail layer: the roster read fails.
    vi.mocked(api.members).mockRejectedValueOnce(new Error('boom'))
    renderGallery()
    const cards = await screen.findAllByTestId('hire-template-card')
    fireEvent.click(cardEl(cards, HIRED_CARD.id))
    const detail = await screen.findByTestId('hire-template-detail')
    fireEvent.click(within(detail).getByTestId('hire-chat-with'))
    await screen.findByTestId('hire-chat-error')
    expect(screen.queryByTestId('hire-template-detail')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    // From the picker: the picked crewmate is gone.
    vi.mocked(api.members).mockResolvedValueOnce({ members: [{ name: 'Nia', display_name: 'Nia' }] } as never)
    fireEvent.click(cardEl(cards, TWICE_CARD.id))
    const detail2 = await screen.findByTestId('hire-template-detail')
    fireEvent.click(within(detail2).getByTestId('hire-your-crewmates'))
    const picker = await screen.findByTestId('hire-crewmate-picker')
    fireEvent.click(within(picker).getAllByTestId('hire-pick-crewmate')[1])
    await waitFor(() => expect(screen.getByTestId('hire-chat-error')).toHaveTextContent('isn’t one of your crewmates anymore'))
    expect(screen.queryByTestId('hire-crewmate-picker')).toBeNull()
    expect(screen.queryByTestId('hire-template-detail')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('location')).toHaveTextContent('/members/hire')
  })

  it('an empty catalog says so', async () => {
    vi.mocked(api.memberTemplates).mockResolvedValue({ templates: [] })
    renderGallery()
    expect(await screen.findByTestId('hire-empty')).toBeInTheDocument()
  })
})
