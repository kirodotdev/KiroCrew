import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { i18nT } from '../i18n/t'
import { OPEN_MESSAGE_TYPE } from './useFrameOpenLink'
import { MAX_WEB_LINK_LENGTH, useFrameLinkBridge, webUrlOf } from './useFrameLinkBridge'

const URL_A = 'https://github.com/example-org/repo/pull/7'

/** A host with one frame. The frame's window is a unique object: the hook compares identity only. */
function Host({ frameWindow, open, gesture }: {
  frameWindow: Window
  open: (url: string) => void
  gesture?: (frame: HTMLIFrameElement) => boolean
}) {
  const ref = useRef<HTMLIFrameElement | null>({ contentWindow: frameWindow } as HTMLIFrameElement)
  const dialog = useFrameLinkBridge([ref], open, gesture ?? (() => true))
  return <>{dialog}</>
}

function post(data: unknown, source: unknown) {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  act(() => { window.dispatchEvent(event) })
}

const title = () => i18nT('hooks.frameLinkBridge.title')
const openLabel = () => i18nT('hooks.frameLinkBridge.open')

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useFrameLinkBridge', () => {
  it('asks with the full URL shown, and opens it only on confirm', async () => {
    const frameWindow = {} as Window
    const open = vi.fn()
    render(<Host frameWindow={frameWindow} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frameWindow)
    expect(await screen.findByText(title())).toBeInTheDocument()
    expect(screen.getByText(URL_A)).toBeInTheDocument()
    expect(open).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: openLabel() }))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    expect(open).toHaveBeenCalledWith(URL_A)
  })

  it('opens nothing when the person cancels', async () => {
    const frameWindow = {} as Window
    const open = vi.fn()
    render(<Host frameWindow={frameWindow} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: 'https://code.example.test/reviews/CR-1' }, frameWindow)
    await screen.findByText(title())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    await waitFor(() => expect(screen.queryByRole('button', { name: openLabel() })).toBeNull())
    expect(open).not.toHaveBeenCalled()
  })

  it('does not even ask without a click in that frame', () => {
    const frameWindow = {} as Window
    const open = vi.fn()
    render(<Host frameWindow={frameWindow} open={open} gesture={() => false} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frameWindow)
    expect(screen.queryByText(title())).toBeNull()
    expect(open).not.toHaveBeenCalled()
  })

  it('ignores a message from a window that is not its frame', () => {
    const frameWindow = {} as Window
    const open = vi.fn()
    render(<Host frameWindow={frameWindow} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, {} as Window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, null)
    expect(screen.queryByText(title())).toBeNull()
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,<p>x</p>',
    'blob:https://example.test/0b1c',
    'file:///etc/hosts',
    'mailto:someone@example.test',
    'vscode://file/x',
    '/relative/path',
    'https://user:pass@example.test/',
    'https://github.com@evil.example/',
    `https://example.test/${'a'.repeat(MAX_WEB_LINK_LENGTH)}`,
  ])('refuses %s without asking', (url) => {
    const frameWindow = {} as Window
    const open = vi.fn()
    render(<Host frameWindow={frameWindow} open={open} />)
    post({ type: OPEN_MESSAGE_TYPE, url }, frameWindow)
    expect(screen.queryByText(title())).toBeNull()
    expect(open).not.toHaveBeenCalled()
  })

  it('shows and opens the parsed URL, so what is read is what opens', () => {
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: 'HTTPS://Example.TEST/a b' })).toBe('https://example.test/a%20b')
    expect(webUrlOf({ type: OPEN_MESSAGE_TYPE, url: 'http://example.test' })).toBe('http://example.test/')
    expect(webUrlOf({ type: 'kirocrew-dashboard:act', url: URL_A })).toBeNull()
  })

  it('opens through window.open with noopener and noreferrer by default', async () => {
    const frameWindow = {} as Window
    const spy = vi.spyOn(window, 'open').mockImplementation(() => null)
    function DefaultHost() {
      const ref = useRef<HTMLIFrameElement | null>({ contentWindow: frameWindow } as HTMLIFrameElement)
      return <>{useFrameLinkBridge([ref], undefined, () => true)}</>
    }
    render(<DefaultHost />)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frameWindow)
    fireEvent.click(await screen.findByRole('button', { name: openLabel() }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith(URL_A, '_blank', 'noopener,noreferrer'))
  })
})
