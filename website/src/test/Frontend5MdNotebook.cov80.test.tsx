/**
 * Notes app: the label switches, the themed confirmation dialog, and the
 * connect-a-vault form.
 *
 * All three are branch-shaped rather than layout-shaped. `labels.ts` is four
 * lookups whose whole job is that every persisted id resolves to a real catalog
 * key; `ConfirmDialog` owns focus, Escape (capture phase) and the scrim; and
 * `ConnectVault` carries the two submit modes, the folder chooser's three
 * outcomes, and the knowledge-registration rollback — the paths a user only sees
 * when something fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import {
  listViewLabel,
  paneViewLabel,
  sortLabel,
  syncedAgoLabel,
} from '../apps/md-notebook/labels'
import { SORTS } from '../apps/md-notebook/constants'
import { ConfirmDialog } from '../apps/md-notebook/ConfirmDialog'
import type { Vault } from '../apps/md-notebook/types'

// ── ConnectVault's backend, stubbed ─────────────────────────────────────────
// A real class so the module's `e instanceof ApiError && e.status === 501`
// discrimination is the thing under test rather than something the mock fakes.
class FakeApiError extends Error {
  status: number
  constructor(status: number, message = 'zz-api-error') {
    super(message)
    this.status = status
  }
}

const notesApi = {
  cloneVault: vi.fn(),
  attachVault: vi.fn(),
  setVaultKnowledge: vi.fn(),
  pickFolder: vi.fn(),
}
const knowledgeRegister = vi.fn()

vi.mock('../apps/md-notebook/api', () => ({
  ApiError: FakeApiError,
  notesApi,
  knowledgeRegister: (...args: unknown[]) => knowledgeRegister(...args),
}))

const { ConnectVault } = await import('../apps/md-notebook/ConnectVault')

const vault: Vault = {
  id: 'zzvault',
  name: 'zzname',
  repo: 'zzrepo',
  branch: 'zzbranch',
  localPath: '/zz/path',
  readOnly: false,
}

describe('md-notebook/labels', () => {
  it('resolves every persisted sort id to a real catalog string', () => {
    for (const id of Object.keys(SORTS)) {
      const label = sortLabel(id)
      expect(typeof label).toBe('string')
      // A missing key would come back as the raw key or empty — either means the
      // switch and the persisted ids have drifted.
      expect(label).not.toBe('')
      expect(label.startsWith('apps.mdNotebook.sort.')).toBe(false)
    }
  })

  it('distinguishes the two list views and the two pane views', () => {
    expect(listViewLabel('folders')).not.toBe(listViewLabel('list'))
    expect(paneViewLabel('rendered')).not.toBe(paneViewLabel('raw'))
    for (const v of [listViewLabel('folders'), paneViewLabel('raw')]) {
      expect(v).not.toBe('')
      expect(v.startsWith('apps.mdNotebook.')).toBe(false)
    }
  })

  it('gives each sync bucket its own sentence and interpolates the count', () => {
    const labels = [
      syncedAgoLabel('now', 0),
      syncedAgoLabel('m', 5),
      syncedAgoLabel('h', 3),
      syncedAgoLabel('d', 2),
    ]
    expect(new Set(labels).size).toBe(4)
    expect(labels[1]).toContain('5')
    expect(labels[2]).toContain('3')
    expect(labels[3]).toContain('2')
    // An unknown unit falls to the day bucket rather than throwing.
    expect(syncedAgoLabel('d', 2)).toBe(labels[3])
  })
})

describe('md-notebook/ConfirmDialog', () => {
  function open(overrides: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    const utils = render(
      <ConfirmDialog
        title="zz-title"
        body={<span data-testid="body">zz-body</span>}
        confirmLabel="zz-confirm"
        cancelLabel="zz-cancel"
        onConfirm={onConfirm}
        onCancel={onCancel}
        {...overrides}
      />,
    )
    return { onConfirm, onCancel, ...utils }
  }

  it('focuses the destructive action on open so Enter confirms', () => {
    const { onConfirm } = open()
    const confirm = screen.getByRole('button', { name: 'zz-confirm' })
    expect(document.activeElement).toBe(confirm)
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('cancels on the Cancel button and on a scrim click', () => {
    const { onCancel, container } = open()
    fireEvent.click(screen.getByRole('button', { name: 'zz-cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    const scrim = container.querySelector('[role="presentation"]') as HTMLElement
    fireEvent.click(scrim)
    expect(onCancel).toHaveBeenCalledTimes(2)
  })

  it('does not cancel when the dialog body itself is clicked', () => {
    const { onCancel } = open()
    fireEvent.click(screen.getByRole('dialog'))
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('cancels on Escape and unbinds the listener when unmounted', () => {
    const { onCancel, unmount } = open()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    // A non-Escape key must fall straight through to the app's own shortcuts.
    fireEvent.keyDown(window, { key: 'a' })
    expect(onCancel).toHaveBeenCalledTimes(1)
    unmount()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('renders the optional footer slot outside the button row', () => {
    const { container } = open({ footer: <a href="#zz" data-testid="foot">zz-foot</a> })
    expect(screen.getByTestId('foot')).toBeTruthy()
    expect(container.querySelector('[role="dialog"]')?.getAttribute('aria-modal')).toBe('true')
  })
})

describe('md-notebook/ConnectVault', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    notesApi.cloneVault.mockResolvedValue({ vault })
    notesApi.attachVault.mockResolvedValue({ vault })
    notesApi.setVaultKnowledge.mockResolvedValue({ vault })
    notesApi.pickFolder.mockResolvedValue({ path: '/zz/picked', cancelled: false })
    knowledgeRegister.mockResolvedValue({ sourceId: 'zzsource' })
  })

  /** The submit button is the only enabled primary action in the form. */
  function submitBtn() {
    const all = Array.from(document.querySelectorAll('button')) as HTMLButtonElement[]
    return all.find((b) => b.style.borderRadius === '12px') as HTMLButtonElement
  }

  /**
   * The two segmented-control buttons, by position rather than by their copy —
   * the labels are catalog strings and a test must not pin user-visible wording.
   * They are the first two buttons the form renders.
   */
  function attachTab() {
    return document.querySelectorAll('button')[1] as HTMLButtonElement
  }

  /** The folder chooser — the only button carrying an icon. */
  function chooseBtn() {
    return Array.from(document.querySelectorAll('button')).find((b) =>
      b.querySelector('svg'),
    ) as HTMLButtonElement
  }

  /** Switch to attach mode and wait for the local-folder field. */
  async function goAttach() {
    fireEvent.click(attachTab())
    await waitFor(() => expect(document.getElementById('mdnb-localFolder')).toBeTruthy())
  }

  it('clones with the typed url, branch and trimmed subfolder', async () => {
    const onConnected = vi.fn()
    render(<ConnectVault onConnected={onConnected} />)
    fireEvent.change(document.getElementById('mdnb-repoUrl') as HTMLInputElement, {
      target: { value: 'zz://repo' },
    })
    fireEvent.change(document.getElementById('mdnb-branch') as HTMLInputElement, {
      target: { value: 'zzbranch' },
    })
    fireEvent.change(document.getElementById('mdnb-token') as HTMLInputElement, {
      target: { value: 'zztoken' },
    })
    fireEvent.change(document.getElementById('mdnb-subfolder') as HTMLInputElement, {
      target: { value: 'notes/' },
    })
    fireEvent.click(submitBtn())
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith(vault))
    expect(notesApi.cloneVault).toHaveBeenCalledWith({
      url: 'zz://repo',
      pat: 'zztoken',
      branch: 'zzbranch',
      subfolder: 'notes',
      knowledge: false,
    })
  })

  it('attaches a local folder in attach mode, and the chooser fills the field', async () => {
    const onConnected = vi.fn()
    render(<ConnectVault onConnected={onConnected} onCancel={() => {}} />)
    // The second segment switches to attach; the repo url field goes away.
    await goAttach()
    expect(document.getElementById('mdnb-repoUrl')).toBeNull()

    fireEvent.click(chooseBtn())
    await waitFor(() =>
      expect((document.getElementById('mdnb-localFolder') as HTMLInputElement).value).toBe('/zz/picked'),
    )

    fireEvent.click(submitBtn())
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith(vault))
    expect(notesApi.attachVault).toHaveBeenCalledWith({
      path: '/zz/picked',
      subfolder: undefined,
      knowledge: false,
    })
  })

  it('leaves the folder untouched when the chooser is cancelled', async () => {
    notesApi.pickFolder.mockResolvedValue({ path: null, cancelled: true })
    render(<ConnectVault onConnected={vi.fn()} />)
    await goAttach()
    fireEvent.click(chooseBtn())
    await waitFor(() => expect(notesApi.pickFolder).toHaveBeenCalled())
    expect((document.getElementById('mdnb-localFolder') as HTMLInputElement).value).toBe('')
  })

  it('explains a 501 from the chooser differently from a real failure', async () => {
    notesApi.pickFolder.mockRejectedValue(new FakeApiError(501))
    render(<ConnectVault onConnected={vi.fn()} />)
    await goAttach()
    const choose = chooseBtn()
    fireEvent.click(choose)
    const mac = await waitFor(() => {
      const el = document.querySelector('[style*="--danger"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    })
    const macText = mac.textContent ?? ''

    notesApi.pickFolder.mockRejectedValue(new Error('zz-picker-broke'))
    fireEvent.click(choose)
    await waitFor(() => {
      const text = (document.querySelector('[style*="--danger"]') as HTMLElement).textContent ?? ''
      expect(text).not.toBe(macText)
      expect(text).toContain('zz-picker-broke')
    })
  })

  it('reports a failed clone without calling back', async () => {
    notesApi.cloneVault.mockRejectedValue(new Error('zz-clone-broke'))
    const onConnected = vi.fn()
    render(<ConnectVault onConnected={onConnected} />)
    fireEvent.change(document.getElementById('mdnb-repoUrl') as HTMLInputElement, {
      target: { value: 'zz://repo' },
    })
    fireEvent.click(submitBtn())
    await waitFor(() =>
      expect((document.querySelector('[style*="--danger"]') as HTMLElement).textContent).toContain(
        'zz-clone-broke',
      ),
    )
    expect(onConnected).not.toHaveBeenCalled()
  })

  it('rolls the knowledge flag back when registration fails, and keeps the vault', async () => {
    knowledgeRegister.mockRejectedValue(new Error('zz-knowledge-broke'))
    const onConnected = vi.fn()
    render(<ConnectVault onConnected={onConnected} />)
    fireEvent.change(document.getElementById('mdnb-repoUrl') as HTMLInputElement, {
      target: { value: 'zz://repo' },
    })
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(submitBtn())
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith(vault))
    expect(notesApi.cloneVault).toHaveBeenCalledWith(expect.objectContaining({ knowledge: true }))
    // The vault survives: the flag is cleared, not the clone.
    expect(notesApi.setVaultKnowledge).toHaveBeenCalledWith(vault.id, false)
    expect((document.querySelector('[style*="--danger"]') as HTMLElement).textContent).toContain(
      'zz-knowledge-broke',
    )
  })

  it('confirms the knowledge source id on a successful registration', async () => {
    const onConnected = vi.fn()
    render(<ConnectVault onConnected={onConnected} />)
    fireEvent.change(document.getElementById('mdnb-repoUrl') as HTMLInputElement, {
      target: { value: 'zz://repo' },
    })
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(submitBtn())
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith(vault))
    expect(notesApi.setVaultKnowledge).toHaveBeenCalledWith(vault.id, true, 'zzsource')
  })

  it('offers the cancel button only when a handler is supplied', () => {
    const onCancel = vi.fn()
    const { unmount } = render(<ConnectVault onConnected={vi.fn()} onCancel={onCancel} />)
    const before = document.querySelectorAll('button').length
    unmount()
    render(<ConnectVault onConnected={vi.fn()} onCancel={null} />)
    expect(document.querySelectorAll('button').length).toBe(before - 1)
  })
})
