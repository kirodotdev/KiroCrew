import React from 'react'
import { render, renderHook, type RenderOptions } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

/** Create a fresh Redux store, optionally with preloaded state. */
export function createTestStore(preloadedState?: Partial<RootState>) {
  return configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    preloadedState,
  })
}

type TestStore = ReturnType<typeof createTestStore>

interface WrapperOptions extends Omit<RenderOptions, 'wrapper'> {
  store?: TestStore
  route?: string
  /**
   * Extra query defaults, merged over this helper's own.
   *
   * The default client here uses React Query's `staleTime: 0`, while the shipped
   * client (`api/queryClient.ts`) sets `staleTime: 30_000`. A cache-freshness bug
   * is therefore INVISIBLE by default: a `fetchQuery` that wrongly serves a cached
   * entry in production re-fetches happily under the test client. Pass
   * `queryDefaults: { staleTime: 30_000 }` when the behaviour under test depends
   * on an entry being considered fresh.
   */
  queryDefaults?: Record<string, unknown>
}

/** Render with Redux Provider + MemoryRouter + ThemeProvider. */
export function renderWithProviders(
  ui: React.ReactElement,
  {
    store = createTestStore(),
    route = '/',
    queryDefaults,
    ...renderOptions
  }: WrapperOptions = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, ...queryDefaults } },
  })
  function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>
    )
  }
  return { store, queryClient, ...render(ui, { wrapper: Wrapper, ...renderOptions }) }
}

/** renderHook with Redux Provider + MemoryRouter + ThemeProvider. */
export function renderHookWithProviders<T>(
  hook: () => T,
  { store = createTestStore(), route = '/' }: { store?: TestStore; route?: string } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>
    )
  }
  return { store, ...renderHook(hook, { wrapper: Wrapper }) }
}

// ---------------------------------------------------------------------------
// Rich composer (Lexical contenteditable) test drivers.
//
// With `lexicalComposer` on, the composer is a contenteditable: no `.value`, no
// `fireEvent.change`, no `selectionStart`. The editable root carries a handle as
// `__composer` (set by LexicalComposerInput), so tests drive the REAL editor.
// ---------------------------------------------------------------------------
import { act as rtlAct, fireEvent as rtlFireEvent, screen as rtlScreen, waitFor as rtlWaitFor } from '@testing-library/react'
import { composerHandleOf, type ComposerRootHandle } from '../components/composerControl'

/** The handle LexicalComposerInput hangs on its editable root (`ComposerRootHandle`). */
type ComposerHandle = ComposerRootHandle
type ComposerRoot = HTMLElement & { __composer?: ComposerHandle }

/** The composer's editable root. Scope with `within` when several composers are mounted. */
export function composerRoot(container?: HTMLElement | Document): ComposerRoot {
  const scope = container ?? document
  const el = scope.querySelector<HTMLElement>('[data-composer-input]')
  if (!el) throw new Error('composerRoot: no [data-composer-input] mounted')
  return el as ComposerRoot
}

/**
 * Wait for the composer to mount and attach its handle, then return the root.
 * The Lexical editor is LAZY-loaded behind a Suspense fallback that carries the
 * same aria-label, so `getByLabelText('Message input')` resolves on the spinner;
 * only `[data-composer-input]` proves the real editor is up.
 */
export async function awaitComposer(container?: HTMLElement | Document): Promise<ComposerRoot> {
  const scope = container ?? document
  await rtlWaitFor(() => {
    if (!scope.querySelector('[data-composer-input]')) throw new Error('composer not mounted yet')
  })
  await rtlAct(async () => {})
  return composerRoot(container)
}

function handleOf(root: ComposerRoot): ComposerHandle {
  const handle = composerHandleOf(root)
  if (!handle) throw new Error('composer handle not attached yet — await act(async () => {}) after render')
  return handle
}

/** The composer's string value (the same token string ChatPage receives via onChange). */
export function composerValue(root: ComposerRoot = composerRoot()): string {
  return handleOf(root).getValue()
}

/** Replace the whole composer text (select-all + insert), through the real editor → onChange path. */
export async function setComposerValue(text: string, root: ComposerRoot = composerRoot()): Promise<void> {
  await rtlAct(async () => {
    const h = handleOf(root)
    const cur = h.getValue()
    h.setSelection(0, cur.length)
    h.insertText(text)
  })
}

/** Insert text at the current caret (append when the caret is untouched: caret starts at the end). */
export async function typeIntoComposer(text: string, root: ComposerRoot = composerRoot()): Promise<void> {
  await rtlAct(async () => { handleOf(root).insertText(text) })
}

/** Put the caret / selection at string offsets. */
export async function setComposerSelection(start: number, end = start, root: ComposerRoot = composerRoot()): Promise<void> {
  await rtlAct(async () => { handleOf(root).setSelection(start, end) })
}

/** Current selection in string offsets. */
export function composerSelection(root: ComposerRoot = composerRoot()): { start: number; end: number } {
  const s = handleOf(root).getSelection()
  return s ? { start: s.start, end: s.end } : { start: 0, end: 0 }
}

/** keyDown on the composer root (Enter/Escape/arrows/history…). */
export function pressInComposer(key: string, init: Record<string, unknown> = {}, root: ComposerRoot = composerRoot()): void {
  rtlFireEvent.keyDown(root, { key, ...init })
}

/** Paste plain text into the composer (goes through ChatInput's paste handling: big → pill). */
export async function pasteIntoComposer(text: string, root: ComposerRoot = composerRoot()): Promise<void> {
  await rtlAct(async () => {
    rtlFireEvent.paste(root, { clipboardData: { types: ['text/plain'], items: [], getData: () => text } })
  })
}

/** The placeholder overlay text currently shown (or null). The rich composer has no `placeholder` attribute. */
export function composerPlaceholder(): string | null {
  const overlay = document.querySelector<HTMLElement>('[data-composer-placeholder]')
  return overlay ? overlay.textContent : null
}

/** `screen.getByLabelText('Message input')` replacement that returns the editable root. */
export function getComposer(label: string | RegExp = 'Message input'): ComposerRoot {
  return rtlScreen.getByLabelText(label) as ComposerRoot
}
