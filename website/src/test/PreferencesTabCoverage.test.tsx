import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { PreferencesTab } from '../apps/personal-shopper/PreferencesTab'
import { renderWithProviders } from './helpers'
import * as shopApi from '../apps/personal-shopper/api'

vi.mock('../apps/personal-shopper/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
}))

interface Pref {
  id: string
  text: string
  tags: string[]
  created_at: string
  updated_at: string
}

interface Grp {
  id: string
  name: string
  icon: string
  sort_order: number
}

const STAMP = '2026-08-10T12:00:00+00:00'

const mkPref = (id: string, text: string, tags: string[] = []): Pref => ({
  id,
  text,
  tags,
  created_at: STAMP,
  updated_at: STAMP,
})

const mkGroup = (id: string, name: string, sort_order = 0): Grp => ({ id, name, icon: '', sort_order })

const mockGet = vi.mocked(shopApi.get)
const mockPost = vi.mocked(shopApi.post)
const mockPut = vi.mocked(shopApi.put)
const mockDel = vi.mocked(shopApi.del)

/** Answer both queries the tab fires, so neither resolves undefined. */
function seed({ preferences = [] as Pref[], groups = [] as Grp[] } = {}) {
  mockGet.mockImplementation((path: string) => {
    if (path === '/groups') return Promise.resolve({ groups })
    return Promise.resolve({ preferences })
  })
  mockPost.mockResolvedValue({ id: 'new-id' })
  mockPut.mockResolvedValue(undefined)
  mockDel.mockResolvedValue(undefined)
}

const PREF_PLACEHOLDER = 'Add a preference (e.g. "shoe size US 10")'

const prefInput = () => screen.getByPlaceholderText(PREF_PLACEHOLDER)

/** The nearest enclosing form of `el` — submitted directly so the guard branch
 *  behind a disabled submit button is still reachable. */
function formOf(el: HTMLElement): HTMLFormElement {
  const form = el.closest('form')
  if (!form) throw new Error('element is not inside a form')
  return form
}

/** A group's <section>, located through its heading so same-named preference
 *  text in a sibling section cannot be confused for it. */
function sectionFor(groupName: string): HTMLElement {
  const section = screen.getByRole('heading', { name: groupName }).closest('section')
  if (!section) throw new Error(`no section for group ${groupName}`)
  return section as HTMLElement
}

const editBtn = (text: string) => screen.getByRole('button', { name: `Edit preference \u201c${text}\u201d` })
const deletePrefBtn = (text: string) =>
  screen.getByRole('button', { name: `Delete preference \u201c${text}\u201d` })

beforeEach(() => {
  vi.clearAllMocks()
})

// ── Query states ───────────────────────────────────────────────────────────

describe('PreferencesTab — query states', () => {
  it('shows the loading placeholder while the preferences query is in flight', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/groups') return Promise.resolve({ groups: [] })
      return new Promise<never>(() => {})
    })
    renderWithProviders(<PreferencesTab />)
    expect(await screen.findByText('Loading preferences...')).toBeInTheDocument()
    // The whole editing surface is replaced while loading.
    expect(screen.queryByPlaceholderText(PREF_PLACEHOLDER)).toBeNull()
  })

  it('shows the empty state and hides the group picker when nothing exists yet', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    expect(await screen.findByText('No preferences yet')).toBeInTheDocument()
    expect(screen.getByText('Add one above, or import them later.')).toBeInTheDocument()
    // No groups -> no "assign to group" dropdown next to the add field.
    expect(screen.queryByRole('combobox', { name: 'Assign to group' })).toBeNull()
    expect(screen.queryByText('Ungrouped')).toBeNull()
    expect(screen.getByRole('button', { name: 'Add group' })).toBeInTheDocument()
  })

  it('lists tagless preferences under Ungrouped', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    expect(await screen.findByText('Ungrouped')).toBeInTheDocument()
    const section = screen.getByText('Ungrouped').closest('section') as HTMLElement
    expect(within(section).getByText('shoe size US 10')).toBeInTheDocument()
    expect(screen.queryByText('No preferences yet')).toBeNull()
  })
})

// ── Group sections ─────────────────────────────────────────────────────────

describe('PreferencesTab — group sections', () => {
  it('files a preference under its group and marks an unfilled group as empty', async () => {
    seed({
      preferences: [mkPref('p1', 'wide toe box', ['g1'])],
      groups: [mkGroup('g1', 'Footwear'), mkGroup('g2', 'Colours', 1)],
    })
    renderWithProviders(<PreferencesTab />)
    const footwear = await waitFor(() => sectionFor('Footwear'))
    expect(within(footwear).getByText('wide toe box')).toBeInTheDocument()
    // Its own group pill would only restate the heading above it.
    expect(within(footwear).queryByText('Footwear')).toBe(
      within(footwear).getByRole('heading', { name: 'Footwear' }),
    )
    expect(within(sectionFor('Colours')).getByText('No items in this group')).toBeInTheDocument()
  })

  it('renders a pill for every other tag, falling back to the raw id when unknown', async () => {
    seed({
      preferences: [mkPref('p1', 'wide toe box', ['g1', 'legacy-default-tag'])],
      groups: [mkGroup('g1', 'Footwear')],
    })
    renderWithProviders(<PreferencesTab />)
    const footwear = await waitFor(() => sectionFor('Footwear'))
    expect(within(footwear).getByText('legacy-default-tag')).toBeInTheDocument()
  })

  it('names known groups in the pill of a preference shown in another group', async () => {
    seed({
      preferences: [mkPref('p1', 'wide toe box', ['g1', 'g2'])],
      groups: [mkGroup('g1', 'Footwear'), mkGroup('g2', 'Colours', 1)],
    })
    renderWithProviders(<PreferencesTab />)
    const footwear = await waitFor(() => sectionFor('Footwear'))
    // Same preference is listed in both sections; each shows the OTHER group's name.
    expect(within(footwear).getByText('Colours')).toBeInTheDocument()
    expect(within(sectionFor('Colours')).getByText('Footwear')).toBeInTheDocument()
  })

  it('collects every preference sharing a tag into that one group', async () => {
    seed({
      preferences: [mkPref('p1', 'wide toe box', ['g1']), mkPref('p2', 'no wool lining', ['g1'])],
      groups: [mkGroup('g1', 'Footwear')],
    })
    renderWithProviders(<PreferencesTab />)
    const footwear = await waitFor(() => sectionFor('Footwear'))
    expect(within(footwear).getByText('wide toe box')).toBeInTheDocument()
    expect(within(footwear).getByText('no wool lining')).toBeInTheDocument()
    expect(within(footwear).queryByText('No items in this group')).toBeNull()
  })

  it('deletes a preference from inside a group section', async () => {
    seed({ preferences: [mkPref('p1', 'wide toe box', ['g1'])], groups: [mkGroup('g1', 'Footwear')] })
    renderWithProviders(<PreferencesTab />)
    const footwear = await waitFor(() => sectionFor('Footwear'))
    fireEvent.click(
      within(footwear).getByRole('button', { name: 'Delete preference \u201cwide toe box\u201d' }),
    )
    await waitFor(() => expect(mockDel).toHaveBeenCalledWith('/preferences/p1'))
  })

  it('deletes a group by id without touching its preferences', async () => {
    seed({ preferences: [mkPref('p1', 'wide toe box', ['g1'])], groups: [mkGroup('g1', 'Footwear')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete group Footwear' }))
    await waitFor(() => expect(mockDel).toHaveBeenCalledWith('/groups/g1'))
    expect(mockDel).not.toHaveBeenCalledWith('/preferences/p1')
  })
})

// ── Adding preferences ─────────────────────────────────────────────────────

describe('PreferencesTab — adding a preference', () => {
  it('posts trimmed text with no tags and clears the field on success', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    const input = await screen.findByPlaceholderText(PREF_PLACEHOLDER)
    fireEvent.change(input, { target: { value: '  ships to Canada  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith('/preferences', { text: 'ships to Canada', tags: [] }),
    )
    await waitFor(() => expect(prefInput()).toHaveValue(''))
  })

  it('keeps the submit button disabled until the field has non-blank text', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    const input = await screen.findByPlaceholderText(PREF_PLACEHOLDER)
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(input, { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(input, { target: { value: 'no wool' } })
    expect(screen.getByRole('button', { name: 'Add' })).toBeEnabled()
  })

  it('posts nothing when the form is submitted with a blank field', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    const input = await screen.findByPlaceholderText(PREF_PLACEHOLDER)
    fireEvent.submit(formOf(input))
    await waitFor(() => expect(mockGet).toHaveBeenCalled())
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('sends the chosen group as the preference tag so the group is fillable', async () => {
    seed({ groups: [mkGroup('g1', 'Footwear')] })
    renderWithProviders(<PreferencesTab />)
    const trigger = await screen.findByRole('combobox', { name: 'Assign to group' })
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Footwear' }))
    fireEvent.change(prefInput(), { target: { value: 'wide toe box' } })
    fireEvent.submit(formOf(prefInput()))
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith('/preferences', { text: 'wide toe box', tags: ['g1'] }),
    )
  })

  it('offers a no-group row that clears the assignment back to untagged', async () => {
    seed({ groups: [mkGroup('g1', 'Footwear')] })
    renderWithProviders(<PreferencesTab />)
    const trigger = await screen.findByRole('combobox', { name: 'Assign to group' })
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'Footwear' }))
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'No group' }))
    fireEvent.change(prefInput(), { target: { value: 'no wool' } })
    fireEvent.submit(formOf(prefInput()))
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith('/preferences', { text: 'no wool', tags: [] }),
    )
  })
})

// ── Editing and deleting a row ─────────────────────────────────────────────

describe('PreferencesTab — preference row', () => {
  it('saves an edited row through PUT and leaves edit mode', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await waitFor(() => editBtn('shoe size US 10')))
    const editField = screen.getByDisplayValue('shoe size US 10')
    fireEvent.change(editField, { target: { value: ' shoe size US 11 ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(mockPut).toHaveBeenCalledWith('/preferences/p1', { text: 'shoe size US 11' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull())
  })

  it('leaves edit mode without a PUT when the text was not changed', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await waitFor(() => editBtn('shoe size US 10')))
    fireEvent.submit(formOf(screen.getByDisplayValue('shoe size US 10')))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull())
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('discards the edit on Escape', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await waitFor(() => editBtn('shoe size US 10')))
    const editField = screen.getByDisplayValue('shoe size US 10')
    fireEvent.change(editField, { target: { value: 'typed then abandoned' } })
    fireEvent.keyDown(editField, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull())
    expect(mockPut).not.toHaveBeenCalled()
    expect(screen.getByText('shoe size US 10')).toBeInTheDocument()
  })

  it('discards the edit through the cancel control', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await waitFor(() => editBtn('shoe size US 10')))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull())
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('stays in edit mode for keys other than Escape', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await waitFor(() => editBtn('shoe size US 10')))
    const editField = screen.getByDisplayValue('shoe size US 10')
    fireEvent.keyDown(editField, { key: 'a' })
    fireEvent.keyDown(editField, { key: 'Tab' })
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
  })

  it('deletes a row by id', async () => {
    seed({ preferences: [mkPref('p1', 'shoe size US 10')] })
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await waitFor(() => deletePrefBtn('shoe size US 10')))
    await waitFor(() => expect(mockDel).toHaveBeenCalledWith('/preferences/p1'))
  })
})

// ── Creating a group ───────────────────────────────────────────────────────

describe('PreferencesTab — creating a group', () => {
  it('creates a group with a blank icon and closes the form on success', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add group' }))
    const nameField = screen.getByPlaceholderText('Group name')
    fireEvent.change(nameField, { target: { value: '  Footwear  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/groups', { name: 'Footwear', icon: '' }))
    await waitFor(() => expect(screen.queryByPlaceholderText('Group name')).toBeNull())
    expect(screen.getByRole('button', { name: 'Add group' })).toBeInTheDocument()
  })

  it('closes the group form on cancel without posting', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add group' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByPlaceholderText('Group name')).toBeNull())
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('posts nothing when the group form is submitted blank', async () => {
    seed()
    renderWithProviders(<PreferencesTab />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add group' }))
    const nameField = screen.getByPlaceholderText('Group name')
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled()
    fireEvent.submit(formOf(nameField))
    await waitFor(() => expect(screen.getByPlaceholderText('Group name')).toBeInTheDocument())
    expect(mockPost).not.toHaveBeenCalled()
  })
})
