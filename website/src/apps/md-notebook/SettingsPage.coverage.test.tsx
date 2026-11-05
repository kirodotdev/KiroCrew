/**
 * `SettingsPage` and `SettingsBar`: vault rows, forget, trash reveal, knowledge toggles with rollback,
 * token save and clear, sync prefs, shortcut capture and close. The notes API and knowledge calls are
 * faked at the `./api` boundary; assertions check the callbacks and the messages a user sees.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { i18nT } from '../../i18n/t'
import { DEFAULT_SYNC_SHORTCUT } from './constants'
import { SettingsBar, SettingsPage, type SettingsPageProps } from './SettingsPage'
import type { Vault } from './types'

const h = vi.hoisted(() => ({ openTrash: vi.fn(), register: vi.fn(), unregister: vi.fn() }))
vi.mock('./api', () => ({
  notesApi: { openTrash: h.openTrash },
  knowledgeRegister: h.register,
  knowledgeUnregister: h.unregister,
}))

const LOCAL: Vault = {
  id: 'v1', name: 'Notes', repo: 'me/notes', branch: 'main', localPath: '/vaults/notes',
  readOnly: false, external: true, knowledge: false, knowledgeSourceId: null,
}
const CLONED: Vault = {
  id: 'v2', name: 'Work', repo: 'me/work', branch: 'dev', localPath: '/vaults/work',
  readOnly: false, subfolder: 'docs', localOnly: true, knowledge: true, knowledgeSourceId: 'src-2',
}

const props = (over: Partial<SettingsPageProps> = {}): SettingsPageProps => ({
  vaults: [LOCAL, CLONED], activeVaultId: 'v1', hasPat: false, hasGhAuth: true, autoSync: false,
  autoSyncMins: 15, autoCommit: true, syncPrefsError: null, shortcut: DEFAULT_SYNC_SHORTCUT,
  onClose: vi.fn(), onSwitchVault: vi.fn(), onConnect: vi.fn(), onForget: vi.fn().mockResolvedValue(undefined),
  onSetPat: vi.fn().mockResolvedValue(undefined), onSetKnowledge: vi.fn().mockResolvedValue(undefined),
  onAutoSync: vi.fn(), onAutoSyncMins: vi.fn(), onAutoCommit: vi.fn(), onSetShortcut: vi.fn(),
  onRecordingChange: vi.fn(), ...over,
})

const buttons = (name: string) => screen.getAllByRole('button', { name })
const switches = (name: string) => screen.getAllByRole('switch', { name })
const KNOW = i18nT('apps.mdNotebook.knowledge.label')

beforeEach(() => {
  h.openTrash.mockReset().mockResolvedValue({ empty: false })
  h.register.mockReset().mockResolvedValue({ sourceId: 'src-1', fileCount: 3 })
  h.unregister.mockReset().mockResolvedValue(undefined)
})

describe('SettingsPage vaults', () => {
  it('marks the active vault and offers Open only for the others', () => {
    const p = props()
    render(<SettingsPage {...p} />)
    expect(screen.getByText(i18nT('apps.mdNotebook.settings.active'))).toBeTruthy()
    expect(screen.getByText('me/notes · main')).toBeTruthy()
    expect(screen.getByText(/^.+ · dev · \/docs$/)).toBeTruthy()
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.open'))[0])
    expect(p.onSwitchVault).toHaveBeenCalledWith('v2')
  })

  it('removes a vault after confirmation, and keeps the bar open on failure', async () => {
    const p = props({ onForget: vi.fn().mockRejectedValueOnce(new Error('disk busy')).mockResolvedValue(undefined) })
    render(<SettingsPage {...p} />)
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.remove'))[0])
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.removeIt'))[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.settings.removeFailed', { message: 'disk busy' }))).toBeTruthy()
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.removeIt'))[0])
    await waitFor(() => expect(screen.queryByText(i18nT('apps.mdNotebook.settings.removeConfirm'))).toBeNull())
    expect(p.onForget).toHaveBeenNthCalledWith(2, 'v1')
  })

  it('reports trash as empty, unsupported, or failed, per vault', async () => {
    h.openTrash
      .mockResolvedValueOnce({ empty: true })
      .mockRejectedValueOnce(Object.assign(new Error('x'), { body: { code: 'folder_open_unsupported' } }))
      .mockRejectedValueOnce(new Error('denied'))
    render(<SettingsPage {...props()} />)
    const opens = screen.getAllByText(i18nT('apps.mdNotebook.trash.open'))
    fireEvent.click(opens[1])
    expect(await screen.findByText(i18nT('apps.mdNotebook.trash.empty'))).toBeTruthy()
    expect(h.openTrash).toHaveBeenCalledWith('v2')
    fireEvent.click(opens[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.trash.unsupported'))).toBeTruthy()
    fireEvent.click(opens[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.trash.failed', { message: 'denied' }))).toBeTruthy()
  })
})

describe('SettingsPage knowledge, token and sync', () => {
  it('registers a source, records it, and reports the file count', async () => {
    const p = props()
    render(<SettingsPage {...p} />)
    fireEvent.click(switches(KNOW)[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.settings.knowledgeAddedCount', { n: 3 }))).toBeTruthy()
    expect(h.register).toHaveBeenCalledWith(LOCAL)
    expect(p.onSetKnowledge).toHaveBeenCalledWith('v1', true, 'src-1')
  })

  it('unregisters a new source when recording it fails', async () => {
    h.register.mockResolvedValue({ sourceId: 'src-1' })
    render(<SettingsPage {...props({ onSetKnowledge: vi.fn().mockRejectedValue(new Error('db down')) })} />)
    fireEvent.click(switches(KNOW)[0])
    expect(await screen.findByText('db down')).toBeTruthy()
    await waitFor(() => expect(h.unregister).toHaveBeenCalledWith('src-1'))
  })

  it('removes a source, and restores the record when the host delete fails', async () => {
    const p = props()
    render(<SettingsPage {...p} />)
    fireEvent.click(switches(KNOW)[1])
    expect(await screen.findByText(i18nT('apps.mdNotebook.settings.knowledgeRemoved'))).toBeTruthy()
    expect(p.onSetKnowledge).toHaveBeenCalledWith('v2', false)
    expect(h.unregister).toHaveBeenCalledWith('src-2')
    h.unregister.mockRejectedValueOnce(new Error('host gone'))
    fireEvent.click(switches(KNOW)[1])
    expect(await screen.findByText('host gone')).toBeTruthy()
    expect(p.onSetKnowledge).toHaveBeenLastCalledWith('v2', true, 'src-2')
  })

  it('saves and clears the token, and reports each outcome', async () => {
    const p = props({ hasPat: true, onSetPat: vi.fn().mockRejectedValueOnce(new Error('bad token')).mockResolvedValue(undefined) })
    render(<SettingsPage {...p} />)
    fireEvent.change(screen.getByLabelText(i18nT('apps.mdNotebook.connect.token')), { target: { value: 'ghp_x' } })
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.save'))[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.settings.tokenSaveFailed', { message: 'bad token' }))).toBeTruthy()
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.save'))[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.settings.tokenSaved'))).toBeTruthy()
    expect(p.onSetPat).toHaveBeenLastCalledWith('ghp_x')
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.clear'))[0])
    expect(await screen.findByText(i18nT('apps.mdNotebook.settings.tokenCleared'))).toBeTruthy()
    expect(p.onSetPat).toHaveBeenLastCalledWith('')
  })

  it('shows the sync error and routes the sync switches and interval', () => {
    const p = props({ autoSync: true, syncPrefsError: 'prefs write refused' })
    render(<SettingsPage {...p} />)
    expect(screen.getByText('prefs write refused')).toBeTruthy()
    fireEvent.click(switches(i18nT('apps.mdNotebook.settings.autoCommit'))[0])
    expect(p.onAutoCommit).toHaveBeenCalledWith(false)
    fireEvent.click(switches(i18nT('apps.mdNotebook.settings.autoSync'))[0])
    expect(p.onAutoSync).toHaveBeenCalledWith(false)
    fireEvent.change(screen.getByLabelText(i18nT('apps.mdNotebook.settings.intervalAria')), { target: { value: '30' } })
    expect(p.onAutoSyncMins).toHaveBeenCalledWith(30)
  })
})

describe('SettingsPage shortcut, close and SettingsBar', () => {
  it('records a modified key, refuses a bare letter, and cancels on Escape', () => {
    const p = props()
    render(<SettingsPage {...p} />)
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.shortcut'))[0])
    expect(p.onRecordingChange).toHaveBeenLastCalledWith(true)
    fireEvent.keyDown(window, { key: 'a' })
    expect(screen.getByText(i18nT('apps.mdNotebook.settings.shortcutNeedsModifier'))).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(p.onRecordingChange).toHaveBeenLastCalledWith(false)
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.shortcut'))[0])
    fireEvent.keyDown(window, { key: 'K', metaKey: true, shiftKey: true })
    expect(p.onSetShortcut).toHaveBeenCalledWith({ key: 'k', meta: true, ctrl: false, alt: false, shift: true })
  })

  it('closes on Escape and from the close button', () => {
    const p = props()
    render(<SettingsPage {...p} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.close'))[0])
    expect(p.onClose).toHaveBeenCalledTimes(2)
  })

  it('opens Settings from the row or the gear, and marks the open state', () => {
    const onOpen = vi.fn()
    const { rerender } = render(<SettingsBar open={false} onOpen={onOpen} />)
    fireEvent.click(screen.getByText(i18nT('apps.mdNotebook.settings.title')))
    fireEvent.click(buttons(i18nT('apps.mdNotebook.settings.title'))[1])
    expect(onOpen).toHaveBeenCalledTimes(2)
    rerender(<SettingsBar open onOpen={onOpen} />)
    expect(document.querySelector('[aria-current="page"]')).toBeTruthy()
  })
})
