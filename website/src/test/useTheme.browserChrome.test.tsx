import { act, cleanup, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { ThemeProvider, useTheme } from '../hooks/useTheme'

vi.mock('../api/client', () => ({ api: {
  themeBoot: () => new Promise(() => {}),
  themes: () => new Promise(() => {}),
  setTheme: () => Promise.resolve({}),
} }))

const clients: QueryClient[] = []
function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  return <QueryClientProvider client={client}><ThemeProvider>{children}</ThemeProvider></QueryClientProvider>
}

let meta: HTMLMetaElement
let sheet: HTMLStyleElement
let frames: Map<number, FrameRequestCallback>
let nextFrame: number
function paint() {
  const pending = [...frames.values()]
  frames.clear()
  act(() => pending.forEach(callback => callback(0)))
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-color-theme', 'emerald')
  frames = new Map()
  nextFrame = 0
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++nextFrame, callback)
    return nextFrame
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  meta = document.createElement('meta')
  meta.name = 'theme-color'
  meta.content = '#0d0f12'
  sheet = document.createElement('style')
  sheet.textContent = '[data-mode="dark"] { --bg: rgb(10, 20, 30); } [data-mode="light"] { --bg: rgb(240, 245, 250); } [data-theme="kiro-light"] { --bg: rgb(255, 255, 255); } body { background-color: rgb(30, 32, 37); }'
  document.head.append(meta, sheet)
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach(client => client.clear())
  meta.remove()
  sheet.remove()
  vi.unstubAllGlobals()
  localStorage.clear()
  delete document.documentElement.dataset.theme
  delete document.documentElement.dataset.mode
  delete document.documentElement.dataset.modePref
})

it('follows the painted canvas on mode and palette changes', () => {
  const { result } = renderHook(() => useTheme(), { wrapper })
  paint()
  expect(meta.content).toBe('rgb(10, 20, 30)')
  act(() => result.current.setTheme('light'))
  paint()
  expect(meta.content).toBe('rgb(240, 245, 250)')
  act(() => result.current.setColorTheme('kiro'))
  paint()
  expect(meta.content).toBe('rgb(255, 255, 255)')
})

it('keeps the boot colour until CSS supplies a background token', () => {
  sheet.remove()
  renderHook(() => useTheme(), { wrapper })
  paint()
  expect(meta.content).toBe('#0d0f12')
})

it('does not schedule work or recreate a missing theme-color tag', () => {
  meta.remove()
  renderHook(() => useTheme(), { wrapper })
  expect(frames.size).toBe(0)
  paint()
  expect(document.querySelector('meta[name="theme-color"]')).toBeNull()
})

it('cancels a queued update when the provider unmounts', () => {
  const { unmount } = renderHook(() => useTheme(), { wrapper })
  unmount()
  paint()
  expect(meta.content).toBe('#0d0f12')
})
