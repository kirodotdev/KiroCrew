import React from 'react'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppApiProvider, useChatLauncher, activeLocale, fmtNumber, fmtCurrency, useLanguageGeneration, APP_SDK_LEVEL } from '../app-sdk'
import { fmtCurrency as hostFmtCurrency } from '../i18n/format'
import { Clickable, SettingsInput, SettingsToggle, Modal, ErrorNotice, Select, guideCaution } from '../kirocrew-ui'
import { guideCaution as hostGuideCaution } from '../guide/trustRoot'
import HostModal from '../components/Modal'
import HostErrorNotice from '../components/ErrorNotice'
import { i18next } from '../i18n/all'

function launcher() {
  const navigate = vi.fn()
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <AppApiProvider appName="example" appVersion="1" allowedApiPaths={[]} allowedEvents={[]}
      subscribeFn={() => () => {}} navigateFn={navigate} notifyFn={() => {}}>
      {children}
    </AppApiProvider>
  )
  return { ...renderHook(() => useChatLauncher(), { wrapper }), navigate }
}

afterEach(() => { delete (window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch })

describe('shared app interaction contracts', () => {
  it('carries draft/target options while encoding only the target in the URL', () => {
    const { result, navigate } = launcher()
    act(() => result.current.openChat({ slotKey: 'slot&other=1', message: 'private draft', autoSend: false }))
    expect(navigate).toHaveBeenCalledWith('/chat?sid=slot%26other%3D1')
    expect((window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch)
      .toMatchObject({ slotKey: 'slot&other=1', message: 'private draft', autoSend: false })
  })

  it('uses new=1 only for a fresh draft and preserves the default route', () => {
    const { result, navigate } = launcher()
    act(() => result.current.openChat({ message: 'draft', autoSend: false }))
    expect(navigate).toHaveBeenLastCalledWith('/chat?new=1')
    act(() => result.current.openChat({ message: 'send' }))
    expect(navigate).toHaveBeenLastCalledWith('/chat')
  })

  it('opens a new draft session for a session-shaping launch and drops the shape for a target', () => {
    const { result, navigate } = launcher()
    const staged = () => (window as Window & { __mc_chat_launch?: Record<string, unknown> }).__mc_chat_launch
    act(() => result.current.openChat({ agent: 'advisor', memoryMode: 'persistent', sessionFolder: false }))
    expect(navigate).toHaveBeenLastCalledWith('/chat?new=1')
    expect(staged()).toMatchObject({ agent: 'advisor', memoryMode: 'persistent', sessionFolder: false })
    act(() => result.current.openChat({ message: 'x', sessionFolder: false }))
    expect(navigate).toHaveBeenLastCalledWith('/chat?new=1')
    // An unknown mode is dropped, so the launch keeps its ordinary send path.
    act(() => result.current.openChat({ message: 'x', memoryMode: 'forever' as never }))
    expect(navigate).toHaveBeenLastCalledWith('/chat')
    expect(staged()).not.toHaveProperty('memoryMode')
    act(() => result.current.openChat({ slotKey: 's1', memoryMode: 'incognito', sessionFolder: false }))
    expect(navigate).toHaveBeenLastCalledWith('/chat?sid=s1')
    expect(staged()).not.toHaveProperty('memoryMode')
    expect(staged()).not.toHaveProperty('sessionFolder')
  })

  it('exports the host currency formatter, caution marker and seam level', () => {
    expect(fmtCurrency).toBe(hostFmtCurrency)
    expect(guideCaution).toBe(hostGuideCaution)
    expect(APP_SDK_LEVEL).toBeGreaterThanOrEqual(1)
  })

  it('renders the narrowed Select over the host dropdown', () => {
    const change = vi.fn()
    render(<Select options={['a', 'b']} optionLabels={['Alpha', 'Beta']} value="b" onChange={change} aria-label="Pick" />)
    expect(screen.getByLabelText('Pick').textContent).toContain('Beta')
  })

  it('reuses settings labels and accessible keyboard interactions', () => {
    const change = vi.fn()
    render(<><SettingsInput label="Name" value="example" onChange={change} />
      <SettingsToggle label="Enabled" checked={false} onChange={change} />
      <Clickable onClick={change}>Action</Clickable></>)
    expect(screen.getByLabelText('Name')).toHaveValue('example')
    fireEvent.click(screen.getByRole('switch', { name: 'Enabled' }))
    expect(change).toHaveBeenCalledWith(true)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Action' }), { key: 'Enter' })
    expect(change).toHaveBeenCalledTimes(2)
  })

  it('exports the existing dialog and error surfaces, not separate copies', () => {
    expect(Modal).toBe(HostModal)
    expect(ErrorNotice).toBe(HostErrorNotice)
  })

  it('observes host language changes and formats with the selected locale', async () => {
    const previous = i18next.language
    const { result } = renderHook(() => { useLanguageGeneration(); return { locale: activeLocale(), number: fmtNumber(1234.5) } })
    try {
      await act(async () => { await i18next.changeLanguage('de') })
      expect(result.current.locale).toBe('de')
      expect(result.current.number).toBe(new Intl.NumberFormat('de').format(1234.5))
    } finally {
      await act(async () => { await i18next.changeLanguage(previous) })
    }
  })
})
