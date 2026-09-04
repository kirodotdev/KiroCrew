import { safeSetItem } from '../utils/safeStorage'
import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  createContext,
  useContext,
  type ReactNode,
} from 'react'

/**
 * UI mode — orthogonal to the theme system.
 *
 *   data-theme : color palette (kiro-dark, monokai-dark, …) — owned by useTheme
 *   data-mode  : light/dark hint derived from theme — owned by useTheme
 *   data-ui    : interface paradigm (chat | cli) — owned by THIS hook
 *
 * Persisted to localStorage as 'mc-ui'. Default 'chat' (no surface change for
 * existing users). Consumed by `src/styles/cli-mode.css` which is loaded in
 * main.tsx and applies the terminal aesthetic to the chat surface only when
 * data-ui="cli". The main app sidebar, topbar, sessions sidebar, and right
 * activity panel are intentionally left untouched.
 *
 * Cross-tab sync: the provider listens for `StorageEvent` so toggling Chat ⇄
 * CLI in one tab updates other open tabs without requiring a reload.
 */

export type UIMode = 'chat' | 'cli'

const STORAGE_KEY = 'mc-ui'

function readInitial(): UIMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'cli' ? 'cli' : 'chat'
  } catch {
    return 'chat'
  }
}

function applyUI(mode: UIMode) {
  document.documentElement.dataset.ui = mode
}

export interface UIModeContextValue {
  uiMode: UIMode
  setUIMode: (m: UIMode) => void
  /** Binary toggle helper. If a third UIMode value is added, this needs to
   *  become a cycle (or the helper should be removed in favour of explicit
   *  setUIMode calls). */
  toggleUIMode: () => void
}

const UIModeContext = createContext<UIModeContextValue | null>(null)

export function UIModeProvider({ children }: { children: ReactNode }) {
  const [uiMode, setUIModeState] = useState<UIMode>(readInitial)

  useEffect(() => {
    applyUI(uiMode)
    try {
      safeSetItem(STORAGE_KEY, uiMode)
    } catch {
      // localStorage unavailable (private mode, quota) — DOM still updates
    }
  }, [uiMode])

  // Cross-tab sync: StorageEvent fires in OTHER tabs when localStorage is
  // written. We never receive our own writes here, so no echo loop.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== STORAGE_KEY) return
      const next: UIMode = e.newValue === 'cli' ? 'cli' : 'chat'
      setUIModeState(prev => (prev === next ? prev : next))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const setUIMode = useCallback((m: UIMode) => setUIModeState(m), [])
  const toggleUIMode = useCallback(
    () => setUIModeState(m => (m === 'cli' ? 'chat' : 'cli')),
    []
  )

  // Memoise the context value so consumers don't re-render when the provider's
  // parent re-renders. Both setUIMode and toggleUIMode are stable via
  // useCallback with no deps, so this effectively only invalidates when
  // uiMode changes — exactly what consumers care about.
  const value = useMemo<UIModeContextValue>(
    () => ({ uiMode, setUIMode, toggleUIMode }),
    [uiMode, setUIMode, toggleUIMode]
  )

  return (
    <UIModeContext.Provider value={value}>
      {children}
    </UIModeContext.Provider>
  )
}

export function useUIMode(): UIModeContextValue {
  const ctx = useContext(UIModeContext)
  if (!ctx) {
    throw new Error('useUIMode must be used within UIModeProvider')
  }
  return ctx
}
