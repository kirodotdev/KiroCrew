/**
 * Tests for SessionControlHost — the popover that hosts an app-contributed
 * session control.
 *
 * The control's module is loaded with a dynamic `import()` of a runtime URL
 * (`/apps/<app>/ui/<entryPoint>`), which jsdom cannot resolve. That is not a
 * limitation to work around: the import failing is a real production path — an
 * app whose bundle is missing or broken — and the component is expected to show
 * a named failure with a Retry rather than an empty popover or a blank chat.
 * These tests cover the host's own behaviour (mount, identity, dismissal,
 * failure) rather than any app's UI.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SessionControlHost from '../components/SessionControlHost'
import type { SessionControlContext } from '../components/SessionControlHost'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

vi.mock('../app-sdk', () => ({
  // The provider's own behaviour is covered by its own tests; here it must only
  // pass children through so the host's tree is what we assert on.
  AppApiProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

const control: ResolvedSessionControl = {
  key: 'test-app:scope',
  appName: 'test-app',
  appDisplayName: 'Test App',
  appVersion: '0.1.0',
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Tag',
  allowedApi: [],
  allowedEvents: [],
  statusPath: 'session-status',
}

const session: SessionControlContext = {
  sessionKey: 'dashboard:chat-2-1787502679',
  folderId: '97fff46e2c09',
  folderName: 'Backend',
  cwd: '/home/me/repo',
}

/** A DOMRect is not constructible in jsdom; only these fields are read. */
const rect = (over: Partial<DOMRect> = {}): DOMRect =>
  ({ left: 100, top: 400, width: 40, height: 28, right: 140, bottom: 428, x: 100, y: 400,
     toJSON: () => ({}), ...over } as DOMRect)

const renderHost = (over: Partial<React.ComponentProps<typeof SessionControlHost>> = {}) => {
  const onClose = vi.fn()
  const utils = render(
    <SessionControlHost
      control={control}
      session={session}
      anchorRect={rect()}
      onClose={onClose}
      {...over}
    />,
    // The host uses react-router `useNavigate()` for its navigateFn (mirroring
    // AppHost), so it must render under a Router. In production it always does —
    // it lives inside ChatPage — so the wrapper reflects reality rather than
    // papering over a gap.
    { wrapper: MemoryRouter },
  )
  return { onClose, ...utils }
}

describe('SessionControlHost', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => cleanup())

  it('renders nothing until the trigger has been measured', () => {
    // The chip reports its rect on click; before that there is nowhere to anchor.
    renderHost({ anchorRect: null })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renders a labelled dialog naming the control and its app', () => {
    renderHost()
    const dialog = screen.getByRole('dialog')
    // Both names: a user with two apps contributing a "Scope" chip must
    // be able to tell which one is open.
    expect(dialog.getAttribute('aria-label')).toBe('Scope — Test App')
  })

  it('portals to document.body rather than nesting inside the composer', () => {
    // Nested, the popover would be clipped by the composer's overflow.
    const { container } = renderHost()
    expect(container.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
  })

  it('closes on Escape', () => {
    const { onClose } = renderHost()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on a click outside', () => {
    const { onClose } = renderHost()
    fireEvent.mouseDown(document.body)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('stays open on a click inside', () => {
    // Otherwise every interaction with the control would dismiss it.
    const { onClose } = renderHost()
    fireEvent.mouseDown(screen.getByRole('dialog'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not treat the triggering chip as outside', () => {
    // mousedown fires before click, so closing here would let the chip's own
    // toggle re-open the popover — clicking an open chip would flicker instead
    // of dismissing. Regression for AutoSDE f-fc907279.
    const { onClose } = renderHost()
    const chip = document.createElement('button')
    chip.setAttribute('data-session-control-chip', '')
    document.body.appendChild(chip)
    fireEvent.mouseDown(chip)
    expect(onClose).not.toHaveBeenCalled()
    chip.remove()
  })

  it('still closes on a click that is genuinely outside', () => {
    const { onClose } = renderHost()
    const other = document.createElement('div')
    document.body.appendChild(other)
    fireEvent.mouseDown(other)
    expect(onClose).toHaveBeenCalledTimes(1)
    other.remove()
  })

  it('ignores keys other than Escape', () => {
    const { onClose } = renderHost()
    fireEvent.keyDown(document, { key: 'a' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not close on Escape while an IME composition is live', () => {
    // An IME user presses Escape to cancel a composition candidate, not to
    // dismiss the dialog. Closing would unmount the control and discard an
    // unsaved draft with no recovery, so the composition latch has to win.
    const { onClose } = renderHost()
    document.dispatchEvent(new CompositionEvent('compositionstart'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes on Escape once the composition has ended', () => {
    // The guard must be narrow: after composition the dialog is dismissible
    // again, or an IME user could never close it with the keyboard.
    vi.useFakeTimers()
    try {
      const { onClose } = renderHost()
      document.dispatchEvent(new CompositionEvent('compositionstart'))
      document.dispatchEvent(new CompositionEvent('compositionend'))
      // The latch holds a short post-composition window (the browser can still
      // be acting on the commit); step past it rather than guessing.
      vi.advanceTimersByTime(200)
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(onClose).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not let a key pressed inside the control reach the global shortcut handler', () => {
    // The data-loss path this closes: the global chat-jump chord claims
    // Ctrl+digit even while focus is in a text field (digits are deliberately
    // not input-gated), and switching slots unmounts this host. So a routine
    // Ctrl+1 while typing in a control would switch chats and discard the
    // unsaved draft. The global handler listens on `window`, so asserting the
    // event never arrives there is the real proof — not that some flag was set.
    const onWindowKey = vi.fn()
    window.addEventListener('keydown', onWindowKey)
    try {
      renderHost()
      const dialog = screen.getByRole('dialog')
      fireEvent.keyDown(dialog, { key: '1', code: 'Digit1', ctrlKey: true })
      expect(onWindowKey).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', onWindowKey)
    }
  })

  it('still lets Escape out to the document, so the popover can close', () => {
    // The isolation above must be narrow: Escape is handled by a document-level
    // listener, so swallowing it too would trap the user in an open control.
    const onWindowKey = vi.fn()
    window.addEventListener('keydown', onWindowKey)
    try {
      const { onClose } = renderHost()
      const dialog = screen.getByRole('dialog')
      fireEvent.keyDown(dialog, { key: 'Escape' })
      expect(onWindowKey).toHaveBeenCalled()
      expect(onClose).toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', onWindowKey)
    }
  })

  it('names the control when its bundle cannot load, and says the chat is fine', async () => {
    // The production case this guards: a missing or broken ui/dist bundle. The
    // import is deliberately resolved with a component rather than rejected, so
    // the message names the control instead of surfacing a bare chunk-load
    // error to the nearest boundary on every render.
    renderHost()
    await waitFor(() =>
      expect(screen.getByRole('dialog').textContent).toMatch(/Could not load Scope/),
    )
  })

  it('shows the underlying reason, not just that it failed', async () => {
    renderHost()
    await waitFor(() =>
      expect(screen.getByRole('dialog').textContent).toMatch(
        /Cannot find module .*dist\/session-control\.mjs/,
      ),
    )
  })

  it('re-imports when the session changes, so state cannot cross chats', async () => {
    // LazyControl is keyed on sessionKey; a control holding per-session state
    // must not carry it into another chat.
    const { rerender } = renderHost()
    await waitFor(() =>
      expect(screen.getByRole('dialog').textContent).toMatch(/Could not load/),
    )
    rerender(
      <SessionControlHost
        control={control}
        session={{ ...session, sessionKey: 'dashboard:chat-9-1787600000' }}
        anchorRect={rect()}
        onClose={vi.fn()}
      />,
    )
    await waitFor(() =>
      expect(screen.getByRole('dialog').textContent).toMatch(/Could not load Scope/),
    )
  })

  it('clamps a trigger near the right edge into the viewport', () => {
    // A chip at the far right would otherwise open a 340px popover off-screen.
    renderHost({ anchorRect: rect({ left: window.innerWidth - 10 }) })
    const dialog = screen.getByRole('dialog') as HTMLElement
    expect(parseFloat(dialog.style.left)).toBeLessThanOrEqual(window.innerWidth - 340)
    expect(parseFloat(dialog.style.left)).toBeGreaterThanOrEqual(8)
  })

  it('anchors above the trigger', () => {
    renderHost({ anchorRect: rect({ top: 400 }) })
    const dialog = screen.getByRole('dialog') as HTMLElement
    expect(parseFloat(dialog.style.bottom)).toBe(window.innerHeight - 400 + 6)
  })

  it('never draws wider than the viewport it sits in', () => {
    // Regression: the width was a fixed 340. Below ~356 the left clamp has
    // already bottomed out at its 8px minimum, so it cannot absorb the
    // difference and the right edge clipped off-screen with nothing to scroll.
    const orig = window.innerWidth
    try {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 320 })
      renderHost({ anchorRect: rect({ left: 300 }) })
      const dialog = screen.getByRole('dialog') as HTMLElement
      const left = parseFloat(dialog.style.left)
      const width = parseFloat(dialog.style.width)
      expect(width).toBeLessThanOrEqual(320 - 16)
      expect(left + width).toBeLessThanOrEqual(320)
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: orig })
    }
  })

  it('still uses its full width when the viewport is wide enough', () => {
    renderHost({ anchorRect: rect({ left: 100 }) })
    const dialog = screen.getByRole('dialog') as HTMLElement
    expect(parseFloat(dialog.style.width)).toBe(340)
  })

  it('never computes a negative width, however absurd the reported viewport', () => {
    // `innerWidth - 16` goes negative below 16px. A negative CSS length is
    // invalid and drops the declaration, so the popover would render at its
    // content width rather than narrow — worse than the clip it replaced.
    const orig = window.innerWidth
    try {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 10 })
      renderHost({ anchorRect: rect({ left: 0 }) })
      const dialog = screen.getByRole('dialog') as HTMLElement
      expect(parseFloat(dialog.style.width)).toBeGreaterThanOrEqual(0)
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: orig })
    }
  })
})
