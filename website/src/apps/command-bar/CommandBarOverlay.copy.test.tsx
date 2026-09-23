import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The Command Bar's copy gesture.
 *
 * What is worth pinning here is that copy is a LAYER, not a command: no row in the
 * launcher was written to be copyable, and these tests assert that rows of three
 * different origins — a settings row from the root index, an artifact row from a
 * scoped view's provider, a deployed artifact carrying its own address — all answer
 * ⌘C without any of them knowing about copying.
 *
 * The rest pins the failures a reader cannot see: a confirmation shown over a
 * clipboard that never changed, and a copy that silently takes the chord away from a
 * text selection of their own.
 */

const dispatch = vi.fn()
const navigate = vi.fn()

const storeState: {
  dashboard: { slots: Record<string, unknown>[]; unreadSlots: string[] }
  chat: { slotStatusDetail: Record<string, unknown>; activeSlot: string | null }
} = {
  dashboard: { slots: [], unreadSlots: [] },
  chat: { slotStatusDetail: {}, activeSlot: null },
}

/**
 * The clipboard write, spied.
 *
 * Mocked at the shared helper rather than at `navigator.clipboard`, because the
 * helper is the contract this surface depends on: it reports whether the text
 * ACTUALLY landed, and jsdom's clipboard would make that boolean a property of the
 * test environment instead of something the overlay has to respect.
 */
const copyToClipboard = vi.fn(async (_text: string) => true)
vi.mock('../../utils/clipboard', () => ({
  copyToClipboard: (...a: unknown[]) => copyToClipboard(...(a as [string])),
}))

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  requestFolderReveal: (folderId: string) => ({ type: 'requestFolderReveal', folderId }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({
    navigate,
    enterInsertOrNewSession: vi.fn(),
    newSessionWithToken: vi.fn(),
  }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<
    typeof import('../../components/commandPalette/providers/recentsProvider')
  >()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

const listApps = vi.fn(async () => [])
const chatFolders = vi.fn(async () => [])
const artifacts = vi.fn(async (_filters?: Record<string, unknown>) => ({ artifacts: [] }))
vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...(a as [])),
    chatFolders: (...a: unknown[]) => chatFolders(...(a as [])),
    artifacts: (...a: unknown[]) => artifacts(...(a as [])),
  },
}))

/**
 * One plain artifact and one DEPLOYED one.
 *
 * The pair is the whole point of the override: both rows open a page on this
 * dashboard, and only the second has an address that means anything to someone who
 * cannot reach this dashboard at all.
 */
const PLAIN = {
  slug: 'onboarding-runbook',
  name: 'Onboarding Runbook',
  kind: 'markdown',
  description: 'How a new hire gets set up',
  tags: [],
  version: 1,
  updated_at: '2026-08-01T00:00:00Z',
}
const DEPLOYED = {
  slug: 'kanban-board',
  name: 'Kanban Board',
  kind: 'webapp',
  description: 'Deployed board',
  tags: [],
  version: 2,
  updated_at: '2026-09-01T00:00:00Z',
  webapp_metadata: { deploy_target: { public_url: 'https://d2nzmpzyp0.cloudfront.net/kanban/' } },
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose }
}

const input = (): HTMLInputElement => screen.getByRole('combobox') as HTMLInputElement
const type = (text: string) => fireEvent.change(input(), { target: { value: text } })
const pressCopy = () => fireEvent.keyDown(input(), { key: 'c', metaKey: true })

/** The row the keyboard is on — what ⌘C acts upon. */
const selectedRow = (): HTMLElement => {
  const hit = screen.queryAllByRole('option').find(r => r.getAttribute('aria-selected') === 'true')
  if (!hit) throw new Error('no row is selected')
  return hit
}

const hasRow = (text: string): boolean =>
  screen.queryAllByRole('option').some(r => (r.textContent || '').includes(text))

const rowByText = (text: string): HTMLElement => {
  const rows = screen.queryAllByRole('option')
  const hit = rows.find(r => (r.textContent || '').includes(text))
  if (!hit) {
    throw new Error(
      `no option row containing "${text}"; rows: ${rows.map(r => r.textContent).join(' | ')}`,
    )
  }
  return hit
}

/**
 * Walk the keyboard onto a SETTINGS row and return it.
 *
 * Not `type()` plus an assumption about what ranked first: the top hit for a settings
 * word is "Toggle Theme", an `invoke` row that correctly has no address, so a test
 * that copied whatever was selected would be testing the ranking rather than the copy
 * layer. Arrowing to the row named "Setting" in its meta column is how a reader
 * reaches it too.
 */
const selectSettingsRow = async (): Promise<HTMLElement> => {
  type('theme')
  await waitFor(() => expect(screen.queryAllByRole('option').length).toBeGreaterThan(1))
  for (let step = 0; step < 12; step += 1) {
    if ((selectedRow().textContent || '').includes('Setting')) return selectedRow()
    fireEvent.keyDown(input(), { key: 'ArrowDown' })
  }
  throw new Error(
    `no settings row within 12 steps; rows: ${screen
      .queryAllByRole('option')
      .map(r => r.textContent)
      .join(' | ')}`,
  )
}

/** Enter the artifacts view the way a user does: activate its root row. */
const enterArtifacts = async () => {
  type('artifact')
  await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
  fireEvent.mouseDown(rowByText('Search Artifacts'))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: /Back to all commands/ })).toBeTruthy(),
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  copyToClipboard.mockResolvedValue(true)
  artifacts.mockResolvedValue({ artifacts: [] })
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
  storeState.dashboard = { slots: [], unreadSlots: [] }
  storeState.chat = { slotStatusDetail: {}, activeSlot: null }
  localStorage.clear()
})

describe('command bar — copy the selected row', () => {
  it('copies a settings row as a dashboard link, with nothing added to that row', async () => {
    // A settings row is built by the ROOT index, which predates the declarative
    // action model and says where it goes with `kind` + `route`. Copy is derived from
    // that, so every row of this shape arrived copyable.
    mount()
    await selectSettingsRow()
    pressCopy()
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledTimes(1))
    const copied = copyToClipboard.mock.calls[0][0]
    expect(copied).toMatch(new RegExp(`^${window.location.origin}/settings/`))
    // The address travels with the word: "Copied" alone left the reader to find out
    // WHICH address on paste, and a deployed artifact yields a different one.
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Copied'))
    expect(screen.getByRole('status').textContent).toContain(copied)
  })

  it('copies an artifact row as a link to that artifact', async () => {
    artifacts.mockResolvedValue({ artifacts: [PLAIN] })
    mount()
    await enterArtifacts()
    await waitFor(() => expect(selectedRow().textContent).toContain('Onboarding Runbook'))
    pressCopy()
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledTimes(1))
    expect(copyToClipboard.mock.calls[0][0]).toBe(
      `${window.location.origin}/artifacts/onboarding-runbook`,
    )
  })

  it('copies a DEPLOYED artifact as its public URL, not as a link to this dashboard', async () => {
    // The row still OPENS the dashboard page. What is worth handing to another person
    // is the address that does not require this dashboard, and only the provider
    // holding the deployment record knows it.
    artifacts.mockResolvedValue({ artifacts: [DEPLOYED] })
    mount()
    await enterArtifacts()
    await waitFor(() => expect(selectedRow().textContent).toContain('Kanban Board'))
    pressCopy()
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledTimes(1))
    expect(copyToClipboard.mock.calls[0][0]).toBe('https://d2nzmpzyp0.cloudfront.net/kanban/')
  })

  it('says so, and writes nothing, for a row that has no address', async () => {
    // A `view` row opens a surface INSIDE the bar, so there is nothing to hand
    // anybody. Silence would read as a copy that worked.
    mount()
    type('artifact')
    await waitFor(() => expect(selectedRow().textContent).toContain('Search Artifacts'))
    pressCopy()
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Nothing here to copy.'))
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('reports a failed clipboard write instead of confirming one', async () => {
    // The helper returns false on a plain-HTTP gateway, where the async clipboard API
    // is unavailable. A tick there would send the reader away believing they hold a
    // link they do not.
    copyToClipboard.mockResolvedValue(false)
    mount()
    await selectSettingsRow()
    pressCopy()
    // Through the product's ERROR surface, not the status line: a write that did not
    // land is an error, and `errors-use-error-notice` is a blocking repository rule.
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Copy failed'))
    expect(screen.queryByRole('status')).toBeNull()
    // And the address it failed to write is ON SCREEN, in full. The message tells the
    // reader to select the text and copy it by hand, and this is the one path where
    // nothing else puts that text anywhere -- an instruction pointing at nothing is
    // worse than no instruction.
    const attempted = copyToClipboard.mock.calls[0][0]
    const strip = screen.getByRole('alert').parentElement as HTMLElement
    expect(strip.textContent).toContain(attempted)
  })

  it('leaves the chord to a text selection the reader made in the query', async () => {
    // The input holds focus the whole time the bar is open, so a selection in it is a
    // deliberate one, and taking ⌘C from it would make the field behave unlike every
    // other text box.
    mount()
    await selectSettingsRow()
    input().setSelectionRange(0, 3)
    pressCopy()
    await waitFor(() => expect(selectedRow().textContent).toBeTruthy())
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('drops the notice when the reader moves to another row', async () => {
    // The notice is a claim about ONE row; left up, it reads as a claim about
    // whichever row the reader arrived at next.
    mount()
    await selectSettingsRow()
    pressCopy()
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Copied'))
    fireEvent.keyDown(input(), { key: 'ArrowDown' })
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  })

  it('names the chord in the footer only while the selected row has an address', async () => {
    // A gesture with no visible counterpart is one most readers never learn exists.
    // Withheld on a row with no address rather than greyed, so the hint doubles as the
    // answer to "can this one be copied".
    mount()
    await selectSettingsRow()
    expect(screen.getByText('Copy link')).toBeTruthy()
    // "Toggle Theme" is an `invoke` row: a callback has no address.
    fireEvent.change(input(), { target: { value: 'toggle theme' } })
    await waitFor(() => expect(selectedRow().textContent).toContain('Toggle Theme'))
    expect(screen.queryByText('Copy link')).toBeNull()
  })
})
