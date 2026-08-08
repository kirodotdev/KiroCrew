import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import FolderConfigModal from '../components/FolderConfigModal'
import { ChatFolder } from '../types'

vi.mock('../api/client', () => ({
  api: {
    // ProjectPicker fetches these on open; the modal itself never calls them.
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
  },
}))

const folder = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id, name: id, order: 0, ...extra }) as ChatFolder

const AGENTS = [{ name: 'kirocrew' }, { name: 'kirocrew-dev' }]

function open(props: Partial<React.ComponentProps<typeof FolderConfigModal>> = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  const onClose = vi.fn()
  const utils = render(
    <FolderConfigModal
      open={true}
      mode="create"
      parentId=""
      folders={[]}
      installedAgents={AGENTS}
      onClose={onClose}
      onSubmit={onSubmit}
      {...props}
    />
  )
  return { onSubmit, onClose, ...utils }
}

/* The agent picker is a Radix Select (SimpleSelect), not a native <select>, so
 * it is addressed by its accessible name — the `aria-label` that carries the
 * "Default agent" heading's key, since a <button> cannot be reached by an
 * external <label htmlFor>. Its options live in a portal that only exists while
 * the popup is open, so any assertion about them has to open it first.
 *
 * NOTE ON THE HARNESS: a Radix Select nested in a Radix Dialog cannot be driven
 * in jsdom (Radix's flushSync inside Testing Library's act() throws "Should not
 * already be working"), which is why CrewEditorSelect.test.tsx and
 * WorkspaceModal.test.tsx stub SimpleSelect out. That does NOT apply here:
 * `Modal` is hand-rolled (createPortal + framer-motion), so there is no Radix
 * layer above the select and the real component is driven directly. Keep it
 * that way — a stub here would stop testing the shipped dropdown. */
function agentTrigger() {
  return screen.getByRole('combobox', { name: 'Default agent' })
}

/** Open the agent popup and return its option labels in render order. */
async function openAgents(): Promise<string[]> {
  fireEvent.click(agentTrigger())
  // findAllByRole, not findByRole: the latter throws on more than one match, and
  // every case here has at least the inherit/None row plus an agent.
  const opts = await screen.findAllByRole('option')
  return opts.map(o => o.textContent ?? '')
}

/** Pick an agent by its visible label, then wait for the popup to unmount —
 *  Radix marks the rest of the page inert while it is up, so a later click on
 *  Submit would be swallowed. */
async function pickAgent(label: string | RegExp) {
  fireEvent.click(agentTrigger())
  fireEvent.click(await screen.findByRole('option', { name: label }))
  await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
}

describe('FolderConfigModal', () => {
  beforeEach(() => vi.clearAllMocks())

  it('offers no parent-folder input — the destination is fixed by the entry point', () => {
    open({ parentId: '' })
    // A picker for the parent would let the user contradict where they clicked.
    // The only dropdown in the modal is the agent picker.
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
    expect(agentTrigger()).toBeTruthy()
  })

  it('restates the destination as a read-only breadcrumb', () => {
    const folders = [folder('a', { name: 'Kiro' }), folder('b', { name: 'Backend', parent_id: 'a' })]
    open({ folders, parentId: 'b' })
    const dest = screen.getByTestId('folder-config-destination')
    expect(dest.textContent).toContain('Kiro')
    expect(dest.textContent).toContain('Backend')
    // Read-only: no editable control inside it.
    expect(dest.querySelector('input,select,button')).toBeNull()
  })

  it('blocks submit until a name is entered', () => {
    const { onSubmit } = open()
    const submit = screen.getByTestId('folder-config-submit') as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    expect(submit.disabled).toBe(false)
    fireEvent.click(submit)
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ name: 'Payments' }))
  })

  it('trims the name before submitting', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: '  Spaced  ' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ name: 'Spaced' }))
  })

  it('treats a whitespace-only name as empty', () => {
    open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: '   ' } })
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
  })

  it('submits with no color by default', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Plain' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ color: '' }))
  })

  it('picks a color from the palette', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Redteam' } })
    // The palette is always visible — no trigger to click first.
    fireEvent.click(screen.getByLabelText('Set color to Red'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ color: '#ef4444' }))
  })

  it('has no icon preview — a folder carries no icon, only a palette color', () => {
    open()
    // The emoji/icon system was removed; the palette swatch row is the only
    // identity affordance, so a glyph preview would advertise a control that
    // does not exist.
    expect(screen.queryByTestId('folder-config-preview')).toBeNull()
    expect(screen.getByTestId('folder-config-color-reset')).toBeTruthy()
  })




  it('keeps an uninstalled agent selectable so Save cannot wipe it', async () => {
    // Found by looking at the built UI: a folder set to an agent that is not in
    // installedAgents had no matching option, so the picker displayed "None"
    // and Save wrote default_agent:'' — silently destroying the folder's config.
    // Happens in production whenever an agent is uninstalled or renamed.
    const f = folder('f1', { name: 'Payments', default_agent: 'retired-agent' })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    expect(agentTrigger()).toHaveTextContent(/retired-agent.*not installed/i)
    // ...and it is a real row in the popup, flagged, so the user can see why it
    // is not running. Ordered right after the inherit/None row, where its
    // <option> used to sit.
    expect(await openAgents()).toEqual(['None', 'retired-agent (not installed)', 'kirocrew', 'kirocrew-dev'])
    // Escape dismisses the popup alone; the modal beneath must survive it.
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ defaultAgent: 'retired-agent' }))
  })

  it('does not flag an installed agent as uninstalled', async () => {
    const f = folder('f1', { name: 'Payments', default_agent: 'kirocrew-dev' })
    open({ mode: 'edit', folder: f, folders: [f] })
    expect(agentTrigger()).toHaveTextContent('kirocrew-dev')
    // Open the popup before the negative assertion: the rows only exist while
    // it is up, so asserting the absence of the flag on a closed picker would
    // pass vacuously.
    expect(await openAgents()).toEqual(['None', 'kirocrew', 'kirocrew-dev'])
    expect(screen.queryByText(/not installed/i)).toBeNull()
  })

  it('clearing the agent back to inherit submits an empty string', async () => {
    // SimpleSelect routes '' through an internal sentinel because Radix reserves
    // '' for "no selection". '' is a real instruction here — it restores the
    // fall-back to the global default — so it has to survive the round trip
    // rather than arriving as the sentinel or as undefined.
    const f = folder('f1', { name: 'Payments', default_agent: 'kirocrew-dev' })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f], globalDefaultAgent: 'kirocrew' })
    // With a global default the top row names it, so the user can see what
    // "inherit" actually resolves to.
    expect(await openAgents()).toEqual(['Inherit (kirocrew)', 'kirocrew', 'kirocrew-dev'])
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
    await pickAgent('Inherit (kirocrew)')
    expect(agentTrigger()).toHaveTextContent('Inherit (kirocrew)')
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      defaultAgent: '', touched: ['defaultAgent'],
    }))
  })

  it('labels the inherited directory as inherited, not as a value', () => {
    // A bare inherited path renders identically to a real value, so the field
    // read as "already set" when it was actually empty.
    const folders = [folder('a', { name: 'Kiro', project_dir: '/projects/root' })]
    open({ folders, parentId: 'a' })
    const dir = screen.getByTestId('folder-config-project-dir') as HTMLInputElement
    expect(dir.value).toBe('')
    expect(dir.placeholder).toContain('/projects/root')
    expect(dir.placeholder).toMatch(/inherited/i)
  })

  describe('failed save keeps the draft', () => {
    // GPT (blocking), Design and UX all converged on this: submit was
    // fire-and-forget, so a 400 from the backend closed the modal and threw the
    // whole draft away with no feedback. The backend rejects a free-typed
    // project_dir (not absolute / not an existing directory / sensitive),
    // which this modal can produce.
    const reject = () => vi.fn().mockRejectedValue(new Error('project_dir must be an existing directory'))

    it('stays open and keeps every field when the save is rejected', async () => {
      const onSubmit = reject()
      const onClose = vi.fn()
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={onClose} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: 'relative/path' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))

      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      // Never auto-closes on failure...
      expect(onClose).not.toHaveBeenCalled()
      // ...and the draft survives so the user can correct the one bad field.
      await waitFor(() => {
        expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Payments')
        expect((screen.getByTestId('folder-config-project-dir') as HTMLInputElement).value).toBe('relative/path')
      })
    })

    it("surfaces the backend's reason verbatim", async () => {
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={reject()} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      const err = await screen.findByTestId('folder-config-error')
      // friendlyErrText already unwraps {"error": …} into ApiError.message.
      expect(err.textContent).toContain('must be an existing directory')
      expect(err.getAttribute('role')).toBe('alert')
    })

    it('closes only after the save resolves', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      const onClose = vi.fn()
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={onClose} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Good' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      // The parent owns closing on success; the modal must not have errored.
      expect(screen.queryByTestId('folder-config-error')).toBeNull()
    })

    it('does not double-submit while a save is in flight', async () => {
      let release: (() => void) | undefined
      const onSubmit = vi.fn(() => new Promise<void>(res => { release = res }))
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Once' } })
      const btn = screen.getByTestId('folder-config-submit') as HTMLButtonElement
      fireEvent.click(btn)
      await waitFor(() => expect(btn.disabled).toBe(true))
      fireEvent.click(btn)
      expect(onSubmit).toHaveBeenCalledTimes(1)
      release?.()
    })

    it('clears a previous error when the retry succeeds', async () => {
      const onSubmit = vi.fn()
        .mockRejectedValueOnce(new Error('project_dir must be an existing directory'))
        .mockResolvedValueOnce(undefined)
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Retry' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await screen.findByTestId('folder-config-error')
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(screen.queryByTestId('folder-config-error')).toBeNull())
    })
  })

  describe('round-2 review findings', () => {
    it('does not re-seed when the folder object identity changes mid-failure', async () => {
      // GPT blocking: the re-seed effect was keyed on the `folder` OBJECT. A
      // rejected edit produces three cache changes in a row (optimistic write ->
      // rollback -> invalidate), each handing down a fresh object, so the effect
      // re-ran and restored the persisted fields — erasing the very draft the
      // keep-open-on-error fix exists to preserve.
      const f = () => folder('f1', { name: 'Payments', project_dir: '/repo/pay' })
      const first = f()
      const { rerender } = render(
        <FolderConfigModal open={true} mode="edit" folder={first} folders={[first]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments v2' } })
      // Same id, brand-new object — exactly what the cache churn hands down.
      const churned = f()
      rerender(
        <FolderConfigModal open={true} mode="edit" folder={churned} folders={[churned]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Payments v2'))
    })

    it('still re-seeds when it retargets to a different folder', async () => {
      const a = folder('a', { name: 'Alpha' })
      const b = folder('b', { name: 'Beta' })
      const { rerender } = render(
        <FolderConfigModal open={true} mode="edit" folder={a} folders={[a, b]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'edited' } })
      rerender(
        <FolderConfigModal open={true} mode="edit" folder={b} folders={[a, b]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Beta'))
    })



    it('ignores backdrop and Escape once the draft is dirty', () => {
      // UX: four fields of work behind a click-anywhere backdrop.
      const { onClose } = open()
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)   // clean draft still dismisses

      onClose.mockClear()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Typed' } })
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('a color-only pick also arms the dismiss guard', () => {
      // Regression: the dirty check once omitted `color`, so a draft whose
      // ONLY change was a swatch pick was silently discarded by Escape.
      const { onClose } = open()
      fireEvent.click(screen.getByLabelText('Set color to Red'))
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('always closes from the explicit Cancel button, even when dirty', () => {
      const { onClose } = open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Typed' } })
      fireEvent.click(screen.getByText(/^Cancel$/))
      expect(onClose).toHaveBeenCalledTimes(1)
    })

    it('shows the typed name as the breadcrumb leaf in create mode', () => {
      open({ parentId: '' })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments rewrite' } })
      expect(screen.getByTestId('folder-config-destination').textContent).toContain('Payments rewrite')
    })
  })


  describe('reports only user-edited fields (touched)', () => {
    // The caller builds its PATCH from `touched` (measured against what the
    // modal opened with), never from a diff against live cache — a field
    // another client changed while the modal was open differs from the draft
    // without the user having touched it, and re-sending the stale value
    // would silently revert it. Only the modal knows the open-time seed, so
    // it is the only place that can say what the *user* changed.
    const seedFolder = folder('f1', { name: 'Payments', project_dir: '/repo/pay' })

    it('a name-only edit reports name alone', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={seedFolder} folders={[seedFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments v2' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const draft = onSubmit.mock.calls[0][0]
      expect(draft.touched).toEqual(['name'])
    })

    it('reports nothing when the user opens and saves without editing', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={seedFolder} folders={[seedFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      expect(onSubmit.mock.calls[0][0].touched).toEqual([])
    })

    it('reports each field the user actually edited', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={seedFolder} folders={[seedFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/new' } })
      await pickAgent('kirocrew-dev')
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const t = onSubmit.mock.calls[0][0].touched
      expect(t).toContain('projectDir')
      expect(t).toContain('defaultAgent')
      expect(t).not.toContain('name')
    })

  })

  it('associates every label with its control', () => {
    // eslint's jsx-a11y/label-has-for cannot see through the `Input` wrapper to
    // confirm nesting, so it warns even when the association is correct. Assert
    // the runtime truth instead of silencing the rule.
    open()
    expect(screen.getByLabelText(/^Name$/)).toBe(screen.getByTestId('folder-config-name'))
    // The agent picker renders a <button>, so its name comes from an aria-label
    // carrying the same "Default agent" key its visible heading uses — an
    // external <label htmlFor> would dangle.
    expect(screen.getByLabelText(/Default agent/)).toBe(agentTrigger())
  })

  it('does not submit while an IME composition is in flight', () => {
    // Regression guard: the first cut of this modal used a bare
    // `if (e.key === 'Enter') submit()`, so the Enter that COMMITS a Chinese /
    // Japanese / Korean composition also created the folder — named after a
    // half-typed word. The inline input this modal replaced guarded it; so must this.
    const { onSubmit } = open()
    const name = screen.getByTestId('folder-config-name')
    fireEvent.change(name, { target: { value: '支付' } })
    fireEvent.compositionStart(name)
    fireEvent.keyDown(name, { key: 'Enter', keyCode: 13 })
    expect(onSubmit).not.toHaveBeenCalled()
    // After the composition ends the same key submits normally.
    fireEvent.compositionEnd(name)
    fireEvent.keyDown(name, { key: 'Enter', keyCode: 229 })   // still IME-processing
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('does not submit from the project-dir field mid-composition', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Named' } })
    const dir = screen.getByTestId('folder-config-project-dir')
    fireEvent.compositionStart(dir)
    fireEvent.keyDown(dir, { key: 'Enter', keyCode: 13 })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('shows the inherited project dir as a placeholder, never a value', () => {
    const folders = [folder('a', { name: 'Kiro', project_dir: '/projects/root' })]
    open({ folders, parentId: 'a' })
    const dir = screen.getByTestId('folder-config-project-dir') as HTMLInputElement
    // Pre-filling would write a duplicate explicit value and sever the
    // inheritance link that resolveFolderProjectDir provides.
    expect(dir.value).toBe('')
    expect(dir.placeholder).toContain('/projects/root')
  })

  it('inherits through a grandparent', () => {
    const folders = [
      folder('a', { name: 'Kiro', project_dir: '/projects/root' }),
      folder('b', { name: 'Backend', parent_id: 'a' }),
    ]
    open({ folders, parentId: 'b' })
    expect((screen.getByTestId('folder-config-project-dir') as HTMLInputElement).placeholder)
      .toContain('/projects/root')
  })

  describe('edit mode', () => {
    const existing = folder('f1', {
      name: 'Payments', project_dir: '/repo/pay', default_agent: 'kirocrew-dev',
    })

    it('prefills every field from the folder', () => {
      open({ mode: 'edit', folder: existing, folders: [existing], parentId: undefined })
      expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Payments')
      expect((screen.getByTestId('folder-config-project-dir') as HTMLInputElement).value).toBe('/repo/pay')
      expect(agentTrigger()).toHaveTextContent('kirocrew-dev')
    })

    it('reset clears the color back to default', () => {
      const withColor = { ...existing, color: '#3b82f6' }
      const { onSubmit } = open({ mode: 'edit', folder: withColor, folders: [withColor] })
      // "No color" is the leading swatch in the always-visible palette row.
      fireEvent.click(screen.getByTestId('folder-config-color-reset'))
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      // '' is a real instruction on the color pipe: PATCH color:'' clears it.
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ color: '', touched: expect.arrayContaining(['color']) }))
    })

    it('clearing the project dir submits an empty string, restoring inheritance', () => {
      const { onSubmit } = open({ mode: 'edit', folder: existing, folders: [existing] })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ projectDir: '' }))
    })
  })

  it('does not leak a draft between openings', async () => {
    const a = folder('a', { name: 'Alpha' })
    const b = folder('b', { name: 'Beta' })
    const { rerender } = render(
      <FolderConfigModal open={true} mode="edit" folder={a} folders={[a, b]}
        installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} />
    )
    expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Alpha')
    rerender(
      <FolderConfigModal open={true} mode="edit" folder={b} folders={[a, b]}
        installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} />
    )
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Beta'))
  })

  it('opens the project picker from Browse', async () => {
    open()
    fireEvent.click(screen.getByTestId('folder-config-browse'))
    const { api } = await import('../api/client')
    await waitFor(() => expect(api.recentProjects).toHaveBeenCalled())
  })
})
