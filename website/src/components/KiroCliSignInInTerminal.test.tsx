import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import ModelEffortDropdown from './ModelEffortDropdown'
import { KiroCliSignInButton, useKiroCliCanRunHere, useKiroCliSignInInTerminal } from './KiroCliSignInInTerminal'
import { KIRO_PREREQUISITE_KEY } from '../hooks/useKiroPrerequisite'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { i18nT } from '../i18n/t'
import { copyCode } from '../utils/clipboard'

vi.mock('../utils/clipboard', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  copyCode: vi.fn(),
}))

/* ── A model list held back by kiro-cli's own sign-in (`/api/models` 503
 *    `kiro_prerequisite_required`): the picker says why, and its button runs
 *    kiro-cli's sign-in in the Terminal panel behind the usual confirm, so no
 *    one has to type a command. ── */

beforeEach(() => setTerminalEnabledFlag(true))
afterEach(() => { cleanup(); setTerminalEnabledFlag(false) })

function client(loginCommand?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (loginCommand) qc.setQueryData(KIRO_PREREQUISITE_KEY, { login_command: loginCommand })
  return qc
}

function Host({ canRun = true }: { canRun?: boolean }) {
  const { askToSignIn, dialog, command } = useKiroCliSignInInTerminal()
  return <><KiroCliSignInButton signIn={{ run: askToSignIn, command, canRun }} />{dialog}</>
}

function runRequests() {
  const seen: { code: string }[] = []
  const on = (e: Event) => seen.push((e as CustomEvent).detail)
  window.addEventListener('mc:run-in-terminal', on)
  return { seen, stop: () => window.removeEventListener('mc:run-in-terminal', on) }
}

describe('useKiroCliSignInInTerminal', () => {
  it("runs the status's own sign-in command in the terminal once confirmed", () => {
    const bundled = "'/opt/Kiro Crew/kiro-cli-chat' login"
    render(<QueryClientProvider client={client(bundled)}><Host /></QueryClientProvider>)
    const runs = runRequests()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.kiroCliSignInInTerminal.sign_in') }))
    // Nothing runs before the confirm, which shows the exact command.
    expect(runs.seen).toEqual([])
    expect(screen.getByText(bundled)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.runInTerminalConfirm.run') }))
    runs.stop()
    expect(runs.seen.map(d => d.code)).toEqual([bundled])
  })

  it('falls back to the bare command before the status is read, with no query client at all', () => {
    render(<Host />)
    const runs = runRequests()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.kiroCliSignInInTerminal.sign_in') }))
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.runInTerminalConfirm.run') }))
    runs.stop()
    expect(runs.seen.map(d => d.code)).toEqual(['kiro-cli login'])
  })
})

describe('KiroCliSignInButton where no terminal here can run it', () => {
  it('offers to copy the command instead of a Run that would time out or be refused', () => {
    render(<QueryClientProvider client={client()}><Host canRun={false} /></QueryClientProvider>)
    expect(screen.queryByRole('button', { name: i18nT('components.kiroCliSignInInTerminal.sign_in') })).toBeNull()
    expect(screen.getByRole('button', { name: i18nT('components.kiroCliSignInInTerminal.copy_sign_in') })).toBeInTheDocument()
  })

  it('shows the command to select by hand when the clipboard refuses it', async () => {
    vi.mocked(copyCode).mockResolvedValueOnce(false)
    render(<QueryClientProvider client={client('kiro-cli login --use-device-flow')}><Host canRun={false} /></QueryClientProvider>)
    expect(screen.queryByText('kiro-cli login --use-device-flow')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.kiroCliSignInInTerminal.copy_sign_in') }))
    expect(await screen.findByText(i18nT('components.runYourselfCard.copy_failed'))).toBeInTheDocument()
    expect(screen.getByText('kiro-cli login --use-device-flow')).toBeVisible()
  })
})

describe('useKiroCliCanRunHere', () => {
  afterEach(() => window.history.replaceState(null, '', '/'))

  it.each([
    ['the main window, a chat that runs here', '/chat', true, true],
    ['a chat whose placement is not read yet', '/chat', undefined, false],
    ['a chat on another machine', '/chat', false, false],
    ['an embed window, which has no dock', '/embed/chat', true, false],
    ['a popout window, which has no dock', '/popout/chat', true, false],
  ])('%s', (_label, path, runsHere, expected) => {
    window.history.replaceState(null, '', path)
    expect(renderHook(() => useKiroCliCanRunHere(runsHere)).result.current).toBe(expected)
  })

  it('is false while the terminal is turned off', () => {
    setTerminalEnabledFlag(false)
    expect(renderHook(() => useKiroCliCanRunHere(true)).result.current).toBe(false)
  })
})

describe('ModelEffortDropdown on a list kiro-cli sign-in holds back', () => {
  const props = {
    anchorRect: { right: 400, top: 300 } as DOMRect,
    dropdownRef: React.createRef<HTMLDivElement>(),
    inputRef: React.createRef<HTMLInputElement>(),
    models: [{ name: 'auto', description: 'Default' }],
    activeModel: 'auto',
    onSelectModel: vi.fn(),
    filter: '',
    setFilter: vi.fn(),
    onClose: vi.fn(),
    hasEffort: false,
    slot: 'dashboard:1',
    currentEffort: '',
    onListKeyDown: vi.fn(),
  }

  function wrap(ui: React.ReactElement) {
    const store = configureStore({
      reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    })
    return render(<Provider store={store}><QueryClientProvider client={client()}>{ui}</QueryClientProvider></Provider>)
  }

  it('says why the list is short and hands the sign-in to its caller', () => {
    const onKiroCliSignIn = vi.fn()
    wrap(<ModelEffortDropdown {...props} kiroCliSignIn={{ run: onKiroCliSignIn, command: 'kiro-cli login', canRun: true }} />)
    expect(screen.getByText(i18nT('components.kiroCliSignInInTerminal.models_signed_out'))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.kiroCliSignInInTerminal.sign_in') }))
    expect(onKiroCliSignIn).toHaveBeenCalledTimes(1)
  })

  it('shows no sign-in row while the list is not held back', () => {
    wrap(<ModelEffortDropdown {...props} />)
    expect(screen.queryByTestId('kiro-cli-models-signed-out')).not.toBeInTheDocument()
  })
})
