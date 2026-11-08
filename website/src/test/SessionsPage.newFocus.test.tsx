// Focus on the Sessions page's New session buttons while the create is out.
//
// A native `disabled` on the button a reader just pressed blurs it to `<body>`.
// When the create fails the page stays put and shows its notice, so a keyboard
// user had lost their place for nothing. The pressed button stays focusable and
// announces itself unavailable instead, and a second press sends nothing.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Routes, Route } from 'react-router-dom'
import SessionsPage from '../pages/SessionsPage'
import { renderWithProviders, createTestStore } from './helpers'
import type { ChatTag } from '../types'

const chatTags = vi.fn<[], Promise<ChatTag[]>>(() => Promise.resolve([]))
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, chatTags: (...a: unknown[]) => chatTags(...(a as [])) } }
})

function renderEmptyPage() {
  const store = createTestStore()
  const state = store.getState()
  return renderWithProviders(
    <Routes>
      <Route path="/sessions" element={<SessionsPage />} />
    </Routes>,
    {
      store: createTestStore({ ...state, dashboard: { ...state.dashboard, slots: [], slotsLoaded: true } }),
      route: '/sessions',
    },
  )
}

/** A create request the test fails by hand, so the pending state can be read. */
function holdCreate() {
  const refused = () => new Response('slot pool exhausted', { status: 500 })
  const held: Array<(r: Response) => void> = []
  let failed = false
  // Every request after the failure (a client retry) is refused at once.
  const fetchMock = vi.spyOn(global, 'fetch').mockImplementation(
    () => failed ? Promise.resolve(refused()) : new Promise<Response>(resolve => { held.push(resolve) }),
  )
  return {
    fetchMock,
    fail: () => {
      failed = true
      for (const resolve of held) resolve(refused())
    },
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SessionsPage New session focus while the create is out', () => {
  for (const testId of ['sessions-new', 'sessions-empty-new']) {
    it(`keeps focus on ${testId}, refuses a second press, and keeps it after a failure`, async () => {
      renderEmptyPage()
      const button = await screen.findByTestId(testId)
      const { fetchMock, fail } = holdCreate()

      button.focus()
      await userEvent.keyboard('{Enter}')
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(button.getAttribute('aria-disabled')).toBe('true')
      expect(button.hasAttribute('disabled')).toBe(false)
      expect(document.activeElement).toBe(button)
      // Held, it looks like a disabled primary button: the muted surface.
      for (const look of ['bg-bg-hover', 'text-muted', 'border-transparent', 'shadow-none', 'opacity-60']) {
        expect(button.className).toContain(`aria-disabled:${look}`)
      }

      await userEvent.keyboard('{Enter}')
      expect(fetchMock).toHaveBeenCalledTimes(1)

      fail()
      expect(await screen.findByTestId('sessions-create-error')).toBeTruthy()
      expect(document.activeElement).toBe(button)
      expect(button.hasAttribute('aria-disabled')).toBe(false)
    })
  }
})
