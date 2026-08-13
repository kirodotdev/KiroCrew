/**
 * CrewEditor — the Issue Radar crew create / edit dialog.
 *
 * Five properties, each one a way this dialog can silently do the wrong thing:
 *
 *  1. CREATE sends a COMPLETE spec. Every control the user filled in has to reach
 *     `createCrew`; a field that renders but is left out of the payload looks like
 *     it saved and did not.
 *  2. EDIT pre-fills from the record and sends ONLY what moved. A "patch" carrying
 *     the whole record would overwrite a field another surface changed while this
 *     dialog sat open.
 *  3. A duplicate name (409) lands INLINE on the name field. The conflict has one
 *     cause and one field, so a banner or a toast would make the user hunt for it.
 *  4. Pinning a face OVERRIDES the name-derived one. The pin is the only way to
 *     escape the hash, so a pin that silently loses to the seed is a dead control.
 *  5. The dialog has an accessible name — in edit mode one that says WHICH crew.
 *
 * i18n note: this file asserts through `i18nT` on the SAME keys the component
 * renders, never against English prose, so a copy edit to the catalog cannot
 * fail a test that is about this dialog's behaviour.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'

/* ── Mocks: the api transport and the shared context ──
   `repoScopeKey` deliberately lives in lib/links.ts, not api.ts, precisely so
   mocking the transport does not also take the cache-key builder with it. */
const mockApi = vi.hoisted(() => ({
  createCrew: vi.fn(),
  updateCrew: vi.fn(),
  suggestCrewNames: vi.fn(),
  labels: vi.fn(),
}))

vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: mockApi }))

/* The agent roster and the model list, stubbed at the HOOK rather than at
   `fetch`. Both hooks are the app's shared ones (`/api/agents` and the single
   `['available-models']` query), and both would otherwise reach the network:
   `useAgents` swallows its own failure, so an unstubbed roster is silently
   EMPTY and every "pick an agent" assertion would fail for a reason that has
   nothing to do with this dialog.

   `oncall` — the stored crew's agent below — is deliberately ABSENT from the
   roster, so the pre-fill assertion doubles as the stale-value case. */
const AGENTS = [
  { name: 'kirocrew', source: 'kiro', description: 'The default agent' },
  { name: 'kirocrew-crew', source: 'kiro', description: 'Issue worker' },
]
const MODELS = [
  { name: 'auto', description: '' },
  { name: 'claude-opus-5', description: 'Deep reasoning' },
  { name: 'gpt-5.6-sol', description: 'Fast' },
]

vi.mock('../hooks/useAgents', () => ({
  useAgents: () => ({ agents: AGENTS, defaultAgent: 'kirocrew' }),
}))
vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModels: () => MODELS,
}))

/* Plain-DOM stand-in for SimpleSelect, for one hard harness limit: this dialog
   is a Radix Dialog and SimpleSelect is a Radix Select, and Radix commits its
   discrete events with `ReactDOM.flushSync(() => target.dispatchEvent(e))`
   (@radix-ui/react-primitive). Testing Library runs every interaction inside
   `act()`, which is already a flush, and React throws "Should not already be
   working." on a flushSync nested inside one — it poisons the whole root, not
   just the interaction. `test/CrewEditorSelect.test.tsx` documents the same
   limit and stubs the same component for the same reason.

   The stub keeps the accessible surface the real one has — a `combobox` showing
   the selected row's text, an `option` per choice, and the `clearLabel` row
   first — and renders the options unconditionally rather than behind a portal.
   The trigger click is kept in the tests so they still read as a user flow.
   What is under test is THIS dialog's logic: which rows it offers, that a value
   the source no longer carries survives, and what each choice sends. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options,
    value,
    onChange,
    clearLabel,
    triggerFallback,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    clearLabel?: string
    triggerFallback?: string
    'aria-label'?: string
  }) => {
    // Mirrors the real trigger: the selected row's own text, and `clearLabel`
    // for the empty value — NOT an empty trigger.
    const selected = options.includes(value)
      ? value
      : value === '' && clearLabel !== undefined
        ? clearLabel
        : (triggerFallback ?? value)
    const listId = `stub-select-${ariaLabel ?? 'x'}`
    return (
      <div>
        <button
          type="button"
          role="combobox"
          aria-label={ariaLabel}
          aria-expanded={false}
          aria-controls={listId}
        >
          {selected}
        </button>
        <div id={listId}>
          {clearLabel !== undefined && (
            <button
              type="button"
              role="option"
              aria-selected={value === ''}
              onClick={() => onChange('')}
            >
              {clearLabel}
            </button>
          )}
          {options.map(o => (
            <button
              key={o}
              type="button"
              role="option"
              aria-selected={o === value}
              onClick={() => onChange(o)}
            >
              {o}
            </button>
          ))}
        </div>
      </div>
    )
  },
}))

const ACTIVE = { owner: 'kirodotdev', repo: 'KiroCrew' } // brand-ok: the repository name

vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ({ active: ACTIVE }),
}))

import CrewEditor from '../apps/issue-radar/components/CrewEditor'
import { djb2, ghostVariantCount } from '../apps/issue-radar/components/CrewGhost'
import type { Crew } from '../apps/issue-radar/api'

const K = 'apps.issueRadar.views.crews.editor'

const SUGGESTIONS = ['Sombrero', 'Bode', 'Butterfly', 'Carina', 'Draco', 'Fireworks']
const REPO_LABELS = [
  { name: 'area: dashboard', color: 'ededed', description: '' },
  { name: 'area: gateway', color: 'ededed', description: '' },
  { name: 'area: core', color: 'ededed', description: '' },
]

/** A stored crew, with values deliberately DIFFERENT from every create-mode
 *  default, so a pre-fill assertion cannot pass on a coincidence. */
const CREW: Crew = {
  schema: 1,
  id: 'c_1a2b3c4d',
  name: 'Whirlpool',
  avatar_seed: 'Whirlpool',
  avatar_variant: null,
  agent: 'oncall',
  model: 'claude-opus-5',
  extra_prompt: 'never touch CI config',
  labels: ['area: gateway'],
  auto_resolve_conflicts: false,
  auto_merge: false,
  unattended: false,
  max_open: 2,
  worktree_root: '~/wt',
  slot_key: 'crew-c_1a2b3c4d',
  enabled: true,
  paused_reason: '',
  created_at: '2026-08-01T00:00:00Z',
  retired_at: null,
}

function renderEditor(crew?: Crew | null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const onClose = vi.fn()
  const rendered = render(
    <QueryClientProvider client={qc}>
      <CrewEditor open onClose={onClose} crew={crew} />
    </QueryClientProvider>,
  )
  return { ...rendered, onClose }
}

/** The dialog, once its two queries have resolved and the name has pre-filled. */
async function openCreate() {
  const r = renderEditor()
  const dialog = await screen.findByTestId('crew-editor')
  await waitFor(() =>
    expect(screen.getByTestId('crew-editor-name')).toHaveValue(SUGGESTIONS[0]),
  )
  await waitFor(() => expect(screen.getByText('area: core')).toBeInTheDocument())
  return { ...r, dialog }
}

const submitBtn = () => screen.getByTestId('crew-editor-submit')

/** The agent / model pickers, addressed through the `data-testid` on each
 *  field's wrapper: both are on screen at once, so a bare `getByRole('option')`
 *  would be ambiguous. The control itself is a `combobox` — Radix Select renders
 *  a <button>, which no external `<label htmlFor>` can associate with, so the
 *  heading is a span and the string is repeated as the control's `aria-label`. */
const field = (testId: string) => within(screen.getByTestId(testId))
const trigger = (testId: string) => field(testId).getByRole('combobox')

function pick(testId: string, option: string) {
  fireEvent.click(trigger(testId))
  fireEvent.click(field(testId).getByRole('option', { name: option }))
}

/** Every row currently offered by one picker, in order. */
function optionsOf(testId: string): string[] {
  fireEvent.click(trigger(testId))
  return field(testId)
    .getAllByRole('option')
    .map(o => o.textContent ?? '')
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.suggestCrewNames.mockResolvedValue({ suggestions: SUGGESTIONS })
  mockApi.labels.mockResolvedValue({
    owner: ACTIVE.owner,
    repo: ACTIVE.repo,
    labels: REPO_LABELS,
    from_cache: false,
  })
  mockApi.createCrew.mockResolvedValue({ crew: CREW })
  mockApi.updateCrew.mockResolvedValue({ crew: CREW })
})

describe('CrewEditor — create mode', () => {
  it('submits every field the form collected as one complete spec', async () => {
    const { onClose } = await openCreate()

    // Touch one control per section, so a payload that drops a whole section
    // (rather than one key) is caught too.
    pick('crew-editor-agent', 'kirocrew-crew')
    pick('crew-editor-model', 'claude-opus-5')
    fireEvent.change(screen.getByTestId('crew-editor-prompt'), {
      target: { value: 'stay off the release branch' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'area: dashboard' }))
    fireEvent.click(
      within(screen.getByTestId('crew-editor-auto-merge')).getByRole('switch'),
    )
    fireEvent.change(screen.getByTestId('crew-editor-max-open'), { target: { value: '5' } })
    fireEvent.change(screen.getByTestId('crew-editor-worktree'), {
      target: { value: '~/workplace/oss' },
    })

    fireEvent.click(submitBtn())

    await waitFor(() => expect(mockApi.createCrew).toHaveBeenCalledTimes(1))
    expect(mockApi.createCrew).toHaveBeenCalledWith(ACTIVE, {
      name: 'Sombrero',
      avatar_variant: null,
      agent: 'kirocrew-crew',
      model: 'claude-opus-5',
      extra_prompt: 'stay off the release branch',
      labels: ['area: dashboard'],
      auto_resolve_conflicts: true,
      auto_merge: false,
      unattended: true,
      max_open: 5,
      worktree_root: '~/workplace/oss',
    })
    // A create that closes before the write lands would hide a 409.
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(mockApi.updateCrew).not.toHaveBeenCalled()
  })

  it('carries a free-entry label the repo does not list', async () => {
    // The repo's label list is not a closed vocabulary: a crew can own a label
    // that has not been created on the forge yet.
    await openCreate()
    fireEvent.click(screen.getByRole('button', { name: i18nT(`${K}.labels_add`) }))
    fireEvent.change(await screen.findByTestId('crew-editor-new-label'), {
      target: { value: 'area: crews' },
    })
    fireEvent.click(screen.getByLabelText(i18nT(`${K}.labels_add_commit`)))

    fireEvent.click(submitBtn())
    await waitFor(() => expect(mockApi.createCrew).toHaveBeenCalledTimes(1))
    expect(mockApi.createCrew.mock.calls[0][1].labels).toEqual(['area: crews'])
  })
})

describe('CrewEditor — edit mode', () => {
  it('pre-fills from the crew and sends only the fields that moved', async () => {
    const { onClose } = renderEditor(CREW)
    await screen.findByTestId('crew-editor')

    // Pre-fill: the record, not the create-mode defaults.
    expect(screen.getByTestId('crew-editor-name')).toHaveValue('Whirlpool')
    // The pickers show a value, not an <input> — and `oncall` is not in the
    // roster, so this is also the "keep a value the roster dropped" case.
    expect(trigger('crew-editor-agent')).toHaveTextContent('oncall')
    expect(trigger('crew-editor-model')).toHaveTextContent('claude-opus-5')
    expect(screen.getByTestId('crew-editor-prompt')).toHaveValue('never touch CI config')
    expect(screen.getByTestId('crew-editor-max-open')).toHaveValue(2)
    expect(screen.getByTestId('crew-editor-worktree')).toHaveValue('~/wt')
    expect(
      within(screen.getByTestId('crew-editor-unattended')).getByRole('switch'),
    ).toHaveAttribute('aria-checked', 'false')
    // The crew's own label is selected even before the repo list resolves.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'area: gateway' })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    )

    pick('crew-editor-model', 'gpt-5.6-sol')
    fireEvent.click(submitBtn())

    await waitFor(() => expect(mockApi.updateCrew).toHaveBeenCalledTimes(1))
    // Exactly one key. Re-sending `labels` (identical members, possibly reordered
    // by the chip strip) or an untouched toggle would clobber a concurrent write.
    expect(mockApi.updateCrew).toHaveBeenCalledWith(ACTIVE, CREW.id, {
      model: 'gpt-5.6-sol',
    })
    expect(mockApi.createCrew).not.toHaveBeenCalled()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('sends an empty patch rather than the whole record when nothing changed', async () => {
    renderEditor(CREW)
    await screen.findByTestId('crew-editor')
    fireEvent.click(submitBtn())
    await waitFor(() => expect(mockApi.updateCrew).toHaveBeenCalledTimes(1))
    expect(mockApi.updateCrew).toHaveBeenCalledWith(ACTIVE, CREW.id, {})
  })
})

describe('CrewEditor — duplicate name', () => {
  /** What the client actually delivers on a 409: `parseErrorBody` flattens the
   *  status away and leaves `crew_store`'s own message. */
  const TAKEN = new Error("crew name 'Sombrero' is already taken in this repo")

  it('renders the conflict inline on the name field, not as a generic failure', async () => {
    mockApi.createCrew.mockRejectedValue(TAKEN)
    const { onClose } = await openCreate()

    fireEvent.click(submitBtn())

    const err = await screen.findByTestId('crew-editor-name-error')
    expect(err).toHaveTextContent(i18nT(`${K}.name_taken`))
    // Wired to the field, so a screen reader reaches it from the input itself.
    const input = screen.getByTestId('crew-editor-name')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', err.id)
    expect(err.id).toBeTruthy()
    // Not the catch-all banner, and the dialog stays open with the form intact.
    expect(screen.queryByTestId('crew-editor-error')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('crew-editor')).toBeInTheDocument()
  })

  it('clears the inline conflict as soon as the name is edited', async () => {
    mockApi.createCrew.mockRejectedValue(TAKEN)
    await openCreate()
    fireEvent.click(submitBtn())
    await screen.findByTestId('crew-editor-name-error')

    fireEvent.change(screen.getByTestId('crew-editor-name'), { target: { value: 'Tucana' } })
    expect(screen.queryByTestId('crew-editor-name-error')).not.toBeInTheDocument()
    expect(screen.getByTestId('crew-editor-name')).not.toHaveAttribute('aria-invalid')
  })

  it('routes a non-conflict failure to the form-level message instead', async () => {
    mockApi.createCrew.mockRejectedValue(new Error('HTTP 502'))
    await openCreate()
    fireEvent.click(submitBtn())

    await screen.findByTestId('crew-editor-error')
    expect(screen.queryByTestId('crew-editor-name-error')).not.toBeInTheDocument()
  })
})

describe('CrewEditor — face', () => {
  /** The variant the NAME hashes to, which is what the strip shows unpinned. */
  const derived = djb2(SUGGESTIONS[0]) % ghostVariantCount
  const pinned = (derived + 3) % ghostVariantCount

  it('starts on the seed-derived face', async () => {
    await openCreate()
    expect(screen.getByTestId(`crew-face-${derived}`)).toHaveAttribute('aria-pressed', 'true')
  })

  it('pinning a variant overrides the seed default and is what gets submitted', async () => {
    await openCreate()
    fireEvent.click(screen.getByTestId(`crew-face-${pinned}`))

    expect(screen.getByTestId(`crew-face-${pinned}`)).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId(`crew-face-${derived}`)).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(submitBtn())
    await waitFor(() => expect(mockApi.createCrew).toHaveBeenCalledTimes(1))
    expect(mockApi.createCrew.mock.calls[0][1].avatar_variant).toBe(pinned)
  })

  it('un-pins when the face already in effect is clicked again', async () => {
    // Otherwise an accidental pin can only be undone by closing the dialog.
    await openCreate()
    fireEvent.click(screen.getByTestId(`crew-face-${pinned}`))
    fireEvent.click(screen.getByTestId(`crew-face-${pinned}`))

    expect(screen.getByTestId(`crew-face-${derived}`)).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(submitBtn())
    await waitFor(() => expect(mockApi.createCrew).toHaveBeenCalledTimes(1))
    expect(mockApi.createCrew.mock.calls[0][1].avatar_variant).toBeNull()
  })
})

describe('CrewEditor — the agent and model pickers', () => {
  it('offers the app roster and the served model list, not free text', async () => {
    await openCreate()
    // A free-text box here let a typo point a crew at an agent that does not
    // exist, and the failure only surfaced on the crew's first cycle.
    expect(field('crew-editor-agent').queryByRole('textbox')).toBeNull()
    expect(field('crew-editor-model').queryByRole('textbox')).toBeNull()

    expect(optionsOf('crew-editor-agent')).toEqual(['kirocrew', 'kirocrew-crew'])
  })

  it('leads the model list with one explicit Auto row that means "inherit"', async () => {
    // `''` is the store's default and used to be only a placeholder — a choice
    // the user could not see they had made. The served list's own `auto` entry
    // folds INTO this row; two rows both reading "Auto" would be a coin flip.
    await openCreate()
    expect(optionsOf('crew-editor-model')).toEqual([
      i18nT(`${K}.model_auto`),
      'claude-opus-5',
      'gpt-5.6-sol',
    ])
    expect(trigger('crew-editor-model')).toHaveTextContent(i18nT(`${K}.model_auto`))
  })

  it('sends an empty model when Auto is chosen', async () => {
    // Without a row for it, a user who picked a model could never put the crew
    // back on the agent's own default.
    renderEditor(CREW)
    await screen.findByTestId('crew-editor')
    pick('crew-editor-model', i18nT(`${K}.model_auto`))
    fireEvent.click(submitBtn())

    await waitFor(() => expect(mockApi.updateCrew).toHaveBeenCalledTimes(1))
    expect(mockApi.updateCrew).toHaveBeenCalledWith(ACTIVE, CREW.id, { model: '' })
  })

  it('keeps a stored value the roster or the model list no longer carries', async () => {
    // A crew outlives the agent template and the model it names. Dropping the
    // value would silently re-point the crew on the next save, which is a
    // config change nobody asked for and nobody sees.
    renderEditor({ ...CREW, agent: 'retired-agent', model: 'retired-model' })
    await screen.findByTestId('crew-editor')

    expect(optionsOf('crew-editor-agent')).toEqual([
      'retired-agent',
      'kirocrew',
      'kirocrew-crew',
    ])
    expect(optionsOf('crew-editor-model')).toEqual([
      i18nT(`${K}.model_auto`),
      'retired-model',
      'claude-opus-5',
      'gpt-5.6-sol',
    ])
    // Selected, not merely listed.
    expect(
      field('crew-editor-model').getByRole('option', { name: 'retired-model' }),
    ).toHaveAttribute('aria-selected', 'true')
    expect(
      field('crew-editor-agent').getByRole('option', { name: 'retired-agent' }),
    ).toHaveAttribute('aria-selected', 'true')
  })
})

describe('CrewEditor — the form survives what happens around it', () => {
  /** Escape, dispatched where Radix listens for it: DismissableLayer binds
   *  `keydown` on `document` with `{ capture: true }`, so a handler on the input
   *  itself is already too late — which is why the interception lives on
   *  `DialogContent`'s `onEscapeKeyDown`. */
  const pressEscape = () => fireEvent.keyDown(document, { key: 'Escape' })

  it('retracts the label entry box on Escape without discarding the form', async () => {
    const { onClose } = await openCreate()
    fireEvent.click(screen.getByRole('button', { name: i18nT(`${K}.labels_add`) }))
    await screen.findByTestId('crew-editor-new-label')

    pressEscape()

    await waitFor(() =>
      expect(screen.queryByTestId('crew-editor-new-label')).not.toBeInTheDocument(),
    )
    // The whole point: one keypress must not cost the user the filled-in form.
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('crew-editor-name')).toHaveValue(SUGGESTIONS[0])

    // With the inner layer gone, Escape closes the dialog again.
    pressEscape()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('keeps an in-progress edit when the roster hands down a new object for the same crew', async () => {
    // The crews query refetches in the background and yields a fresh object every
    // time. Re-initializing on the object's identity would wipe the user's typing
    // whenever an unrelated poll landed.
    const { rerender, onClose } = renderEditor(CREW)
    await screen.findByTestId('crew-editor')
    fireEvent.change(screen.getByTestId('crew-editor-prompt'), {
      target: { value: 'half-typed' },
    })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={qc}>
        {/* Same crew, structurally equal, different reference. */}
        <CrewEditor open onClose={onClose} crew={{ ...CREW }} />
      </QueryClientProvider>,
    )

    expect(screen.getByTestId('crew-editor-prompt')).toHaveValue('half-typed')
  })
})

/**
 * Closing is guarded, but only once there is something to lose.
 *
 * Both halves matter. A dirty form discarded on one stray Escape costs the user
 * everything they typed; a confirmation on EVERY close is its own annoyance and
 * teaches them to dismiss the prompt unread, which is when it stops protecting
 * anything. So the guard is asserted from both directions.
 */
describe('CrewEditor — the close guard', () => {
  const pressEscape = () => fireEvent.keyDown(document, { key: 'Escape' })
  const guard = () => screen.queryByTestId('crew-editor-discard')

  it('closes an untouched create dialog on one Escape', async () => {
    // The name pre-fill is the DIALOG writing to its own form. Counting it as an
    // edit would make every create dialog ask before closing.
    const { onClose } = await openCreate()
    pressEscape()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(guard()).not.toBeInTheDocument()
  })

  it('closes an untouched edit dialog on one Escape', async () => {
    const { onClose } = renderEditor(CREW)
    await screen.findByTestId('crew-editor')
    pressEscape()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(guard()).not.toBeInTheDocument()
  })

  it('asks before discarding a half-filled form, and does not close on its own', async () => {
    const { onClose } = await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-prompt'), {
      target: { value: 'never touch the release branch' },
    })

    pressEscape()

    expect(await screen.findByTestId('crew-editor-discard')).toBeInTheDocument()
    // The veto: the form is still there, with the text still in it.
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('crew-editor-prompt')).toHaveValue('never touch the release branch')
  })

  it('keeps editing when the guard is declined, and the form is intact', async () => {
    const { onClose } = await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-name'), { target: { value: 'Tucana' } })
    pressEscape()
    fireEvent.click(await screen.findByTestId('crew-editor-discard-keep'))

    await waitFor(() => expect(guard()).not.toBeInTheDocument())
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('crew-editor-name')).toHaveValue('Tucana')
  })

  it('closes only when the discard is confirmed', async () => {
    const { onClose } = await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-name'), { target: { value: 'Tucana' } })
    pressEscape()
    fireEvent.click(await screen.findByTestId('crew-editor-discard-confirm'))

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('guards the footer Cancel button too, not just Escape', async () => {
    // Cancel used to call `onClose` directly, so the one control most likely to be
    // clicked by mistake was the one path with no guard on it.
    const { onClose } = await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-prompt'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: i18nT(`${K}.cancel`) }))

    expect(await screen.findByTestId('crew-editor-discard')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('treats a toggle and a label chip as edits, not just typing', async () => {
    // Dirtiness is measured field by field off the values the form opened with, so
    // every control counts — not only the text inputs.
    const { onClose } = await openCreate()
    fireEvent.click(within(screen.getByTestId('crew-editor-unattended')).getByRole('switch'))
    pressEscape()
    expect(await screen.findByTestId('crew-editor-discard')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not ask when an edit is undone back to where it started', async () => {
    // The comparison is against VALUES, not a "was touched" flag, so a user who
    // types and then reverts has nothing to lose and must not be asked.
    const { onClose } = renderEditor(CREW)
    await screen.findByTestId('crew-editor')
    const prompt = screen.getByTestId('crew-editor-prompt')
    fireEvent.change(prompt, { target: { value: 'something else' } })
    fireEvent.change(prompt, { target: { value: CREW.extra_prompt } })

    pressEscape()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(guard()).not.toBeInTheDocument()
  })

  it('does not ask after a successful save', async () => {
    // The dialog is maximally dirty at this point and closes anyway: the work is
    // on the server, so there is nothing left to protect.
    const { onClose } = await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-prompt'), { target: { value: 'x' } })
    fireEvent.click(submitBtn())

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(guard()).not.toBeInTheDocument()
  })
})

describe('CrewEditor — the slot limit reads what the input reports', () => {
  it('takes scientific notation at face value instead of truncating it to 1', async () => {
    // REGRESSION: `parseInt('1e1', 10)` is 1 — it stops at the 'e'. An
    // <input type="number"> accepts 1e1 as a valid number and hands it back
    // verbatim, so the clamp saw 1 and a ten-slot crew was created with ONE
    // slot. Silent, and only visible later as a crew that will not pick up work.
    await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-max-open'), { target: { value: '1e1' } })
    expect(screen.getByTestId('crew-editor-max-open')).toHaveValue(10)
  })

  it('keeps the previous value for a fraction rather than flooring it', async () => {
    // Number() admits '1.5' where parseInt floored it. Neither is what was typed,
    // so the field holds its last good value instead of inventing one.
    await openCreate()
    fireEvent.change(screen.getByTestId('crew-editor-max-open'), { target: { value: '3' } })
    expect(screen.getByTestId('crew-editor-max-open')).toHaveValue(3)
    fireEvent.change(screen.getByTestId('crew-editor-max-open'), { target: { value: '1.5' } })
    expect(screen.getByTestId('crew-editor-max-open')).toHaveValue(3)
  })
})

describe('CrewEditor — accessible name', () => {
  it('names the create dialog', async () => {
    renderEditor()
    expect(await screen.findByRole('dialog', { name: i18nT(`${K}.aria_create`) }))
      .toBeInTheDocument()
  })

  it('names the edit dialog after the crew being edited', async () => {
    // The visible title is just "Edit crew"; the accessible name has to say which.
    renderEditor(CREW)
    expect(
      await screen.findByRole('dialog', {
        name: i18nT(`${K}.aria_edit`, { name: CREW.name }),
      }),
    ).toBeInTheDocument()
  })
})
