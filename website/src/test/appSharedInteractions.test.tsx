import React from 'react'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppApiProvider, useChatLauncher, activeLocale, fmtNumber, useLanguageGeneration } from '../app-sdk'
import { Clickable, SettingsInput, SettingsToggle, Modal, ErrorNotice } from '../kirocrew-ui'
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
