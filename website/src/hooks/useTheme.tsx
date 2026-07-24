import {
  useState,
  useEffect,
  useCallback,
  createContext,
  useContext,
  type ReactNode,
} from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { reportSeamCollision } from '../apps/seamCollision'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { safeSetItem } from '../utils/safeStorage'

export type ModePreference = 'dark' | 'light' | 'system'
export type ResolvedMode = 'dark' | 'light'
export type ColorTheme = string  // built-in slug or 'custom-{slug}'

export interface ThemeEntry {
  value: string
  label: string
  custom?: boolean
}

export interface CustomThemeData {
  name: string
  slug: string
  emoji: string
  dark: Record<string, string>
  light: Record<string, string>
}

function getSystemMode(): ResolvedMode {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function resolveMode(pref: ModePreference): ResolvedMode {
  return pref === 'system' ? getSystemMode() : pref
}

function applyTheme(colorTheme: ColorTheme, mode: ResolvedMode) {
  const el = document.documentElement
  if (colorTheme.startsWith('custom-')) {
    el.dataset.theme = `${colorTheme}-${mode}`
  } else {
    el.dataset.theme = colorTheme === 'emerald' ? mode : `${colorTheme}-${mode}`
  }
  el.dataset.mode = mode
}

// Allowlist of allowed CSS custom property names for themes.
// Only these variables will be injected — unknown keys are silently dropped.
const ALLOWED_CSS_VARS = new Set([
  '--bg', '--bg-accent', '--bg-elevated', '--bg-hover',
  '--card', '--card-fg', '--card-hl',
  '--panel', '--panel-strong', '--chrome',
  '--text', '--text-strong', '--muted', '--muted-strong',
  '--border', '--border-strong', '--border-hover',
  '--accent', '--accent-hover', '--accent-subtle',
  '--accent-glow', '--ring',
  '--ok', '--ok-subtle', '--warn', '--warn-subtle',
  '--danger', '--danger-subtle', '--info',
  '--aim', '--aim-subtle',
  '--clarify', '--clarify-subtle',
  '--diff-add', '--diff-add-text',
  '--diff-del', '--diff-del-text',
  '--diff-hunk', '--diff-hunk-text', '--diff-meta-text',
  '--shadow-sm', '--shadow-md', '--shadow-lg',
])

/**
 * Positive-allowlist CSS value sanitizer lives in src/lib/cssSanitize.ts so
 * WidgetFrame and any other surface that serializes theme vars uses the same
 * filter. See that file for the security rationale.
 */
const escapeCssValue = sanitizeCssValue

/** Inject a custom theme's CSS variables as a <style> tag in the document head. */
function injectCustomThemeCSS(theme: CustomThemeData) {
  // Validate slug is safe for use in CSS selector
  const slug = theme.slug.replace(/[^a-z0-9-]/g, '')
  if (!slug) return

  const id = `mc-custom-theme-${slug}`
  document.getElementById(id)?.remove()

  const style = document.createElement('style')
  style.id = id

  const buildVars = (vars: Record<string, string>) =>
    Object.entries(vars)
      .filter(([k]) => ALLOWED_CSS_VARS.has(k))
      .map(([k, v]): [string, string] => [k, escapeCssValue(v)])
      .filter(([, v]) => v !== '')  // drop entries with empty/rejected values
      .map(([k, v]) => `${k}:${v}`)
      .join(';')

  // Static defaults (not user-controlled)
  const darkDefaults =
    '--font-body:\'Space Grotesk\',-apple-system,BlinkMacSystemFont,sans-serif;' +
    '--mono:\'JetBrains Mono\',ui-monospace,SFMono-Regular,monospace;' +
    '--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;' +
    'color-scheme:dark;'
  const lightDefaults =
    '--font-body:\'Space Grotesk\',-apple-system,BlinkMacSystemFont,sans-serif;' +
    '--mono:\'JetBrains Mono\',ui-monospace,SFMono-Regular,monospace;' +
    '--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;' +
    'color-scheme:light;'

  const darkCss = buildVars(theme.dark)
  const lightCss = buildVars(theme.light)

  style.textContent =
    `[data-theme="custom-${slug}-dark"]{${darkCss};${darkDefaults}}\n` +
    `[data-theme="custom-${slug}-light"]{${lightCss};${lightDefaults}}`

  document.head.appendChild(style)
}

/** Remove injected CSS for a custom theme. */
function removeCustomThemeCSS(slug: string) {
  document.getElementById(`mc-custom-theme-${slug}`)?.remove()
}

export const THEMES: ThemeEntry[] = [
  { value: 'emerald', label: '🌿 Emerald' },
  { value: 'monokai', label: '🎨 Monokai' },
  { value: 'solarized', label: '☀️ Solarized' },
  { value: 'amber', label: '🔥 Amber' },
  { value: 'dracula', label: '🔮 Dracula' },
  { value: 'nord', label: '🌊 Nord' },
  { value: 'rosepine', label: '🌹 Rosé Pine' },
  { value: 'catppuccin', label: '🐱 Catppuccin' },
  { value: 'tokyonight', label: '🌃 Tokyo Night' },
  { value: 'gruvbox', label: '🍦 Gruvbox' },
  { value: 'ice', label: '🧊 Ice' },
  { value: 'amoled', label: '🖤 AMOLED' },
  { value: 'kiro', label: '👻 Kiro' },
  { value: 'intellij', label: '😶‍🌫️ IntelliJ' },
  { value: 'highcontrast', label: '🔆 High Contrast' },
  { value: 'lumon', label: '🛗 Lumon' },
  { value: 'everforest', label: '🌲 Everforest' },
  { value: 'amoled-midnight', label: '🌌 AMOLED Midnight' },
  { value: 'amoled-grey-calm', label: '🌑 AMOLED Grey Calm' },
]

/** Default color theme applied on first run when no preference is persisted. */
export const DEFAULT_COLOR_THEME: ColorTheme = 'kiro'

/**
 * Downstream-registered built-in themes.
 *
 * Extension seam: a downstream edition (or plugin bundle) adds its own theme
 * options to the theme picker via `registerTheme()` from its entry module,
 * instead of editing the `THEMES` array on every upstream sync. These are
 * built-in (non-`custom`) themes — the theme's CSS block ships with the
 * edition's overlay; this registry only contributes the picker entry (value +
 * label). The core registers none, so the stock picker shows only `THEMES`.
 *
 * Read via `allThemes` (`[...THEMES, ...registered, ...customThemes]`), so a
 * registered theme appears in the picker without touching this file. Registration
 * is expected at module-load time (edition composition), before the picker
 * renders — this registry is not reactive.
 */
const REGISTERED_THEMES: ThemeEntry[] = []

/**
 * Register additional built-in theme picker entries at runtime. A duplicate
 * `value` (already in `THEMES` or previously registered) is ignored and logs a
 * warning, so re-entrant registration (e.g. HMR) stays idempotent.
 */
export function registerTheme(entries: ThemeEntry[]): void {
  for (const entry of entries) {
    if (
      THEMES.some((t) => t.value === entry.value) ||
      REGISTERED_THEMES.some((t) => t.value === entry.value)
    ) {
      reportSeamCollision('theme', `theme ${entry.value} already registered; ignoring duplicate`)
      continue
    }
    REGISTERED_THEMES.push(entry)
  }
}

/** All registered downstream themes, in insertion order. */
export function getRegisteredThemes(): readonly ThemeEntry[] {
  return REGISTERED_THEMES
}

const SYNC_EVENT = 'mc-theme-sync'
export const CUSTOM_THEMES_CHANGED_EVENT = 'mc-custom-themes-changed'

function broadcast(mode: ModePreference, colorTheme: ColorTheme) {
  window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: { mode, colorTheme } }))
}

/** Notify all useTheme instances that custom themes have changed. */
function broadcastCustomThemesChanged() {
  window.dispatchEvent(new Event(CUSTOM_THEMES_CHANGED_EVENT))
}

export interface ThemeContextValue {
  theme: ResolvedMode
  preference: ModePreference
  cycle: () => void
  setTheme: (pref: ModePreference) => void
  colorTheme: ColorTheme
  setColorTheme: (t: ColorTheme) => void
  allThemes: ThemeEntry[]
  customThemes: ThemeEntry[]
  customThemeDataMap: Map<string, CustomThemeData>
  themeVersion: number
  onboarded: boolean
  markOnboarded: () => void
  addCustomTheme: (data: Omit<CustomThemeData, 'slug'> & { slug?: string }) => Promise<CustomThemeData>
  deleteCustomTheme: (slug: string) => Promise<void>
  loadCustomThemes: () => Promise<void>
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

/**
 * Mount once near the app root. All theme state lives in this provider's
 * single useThemeState() instance; every useTheme() consumer downstream
 * reads from the shared context rather than spinning up its own
 * localStorage / matchMedia / API subscriptions.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const value = useThemeState()
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used within <ThemeProvider>')
  }
  return ctx
}

/**
 * Internal state hook — ONLY called once, by ThemeProvider. All theme state,
 * effects, listeners, and API calls live here. Consumers reach this via
 * useTheme() → useContext(ThemeContext), so there's exactly one subscription
 * regardless of how many components render it.
 */
function useThemeState(): ThemeContextValue {
  const [mode, setMode] = useState<ModePreference>(
    () => (localStorage.getItem('mc-theme') as ModePreference) || 'system'
  )
  const [colorTheme, setColorThemeState] = useState<ColorTheme>(
    () => (localStorage.getItem('mc-color-theme') as ColorTheme) || DEFAULT_COLOR_THEME
  )
  const [resolved, setResolved] = useState<ResolvedMode>(() => resolveMode(mode))
  const [customThemes, setCustomThemes] = useState<ThemeEntry[]>([])
  const [customThemeDataMap, setCustomThemeDataMap] = useState<Map<string, CustomThemeData>>(new Map())
  // Monotonic counter bumped on any change that affects computed CSS vars on
  // documentElement: mode change, color-theme change, and in-place edits to
  // the active custom theme (same slug, new values). Consumers that read the
  // DOM's computed styles (e.g. WidgetFrame serializing vars into an iframe)
  // include this in their memo deps so they re-read after the edit flow in
  // themeEditor dispatches CUSTOM_THEMES_CHANGED_EVENT.
  const [themeVersion, setThemeVersion] = useState(0)
  const bumpThemeVersion = useCallback(() => setThemeVersion(v => v + 1), [])
  const [onboarded, setOnboarded] = useState(() => !!localStorage.getItem('mc-onboarded'))

  const loadCustomThemes = useCallback(async () => {
    try {
      const res = await api.themes()
      const themes: ThemeEntry[] = (res.themes || []).map((t: { slug: string; name: string; emoji: string }) => ({
        value: `custom-${t.slug}`,
        label: `${t.emoji} ${t.name}`,
        custom: true,
      }))
      setCustomThemes(themes)

      // Fetch all theme details in parallel to avoid serial waterfall
      const dataMap = new Map<string, CustomThemeData>()
      const results = await Promise.allSettled(
        (res.themes || []).map((t: { slug: string }) => api.themeDetail(t.slug))
      )
      for (const r of results) {
        if (r.status === 'fulfilled') {
          dataMap.set(r.value.slug, r.value)
          injectCustomThemeCSS(r.value)
        }
      }
      setCustomThemeDataMap(dataMap)
      bumpThemeVersion()
    } catch {
      // API not available yet — ignore
    }
  }, [bumpThemeVersion])

  // Load custom themes from API on mount + listen for cross-instance changes
  useEffect(() => {
    loadCustomThemes()
    const handler = () => loadCustomThemes()
    window.addEventListener(CUSTOM_THEMES_CHANGED_EVENT, handler)
    return () => window.removeEventListener(CUSTOM_THEMES_CHANGED_EVENT, handler)
  }, [loadCustomThemes])

  // Fetch workspace theme config from server on boot.
  // Server is the source of truth; localStorage is a render cache.
  const { data: bootData } = useQuery({
    queryKey: ['theme-boot'],
    queryFn: () => api.themeBoot(),
    staleTime: Infinity,  // only need it once on mount
    retry: false,         // if server unavailable, fall back to localStorage silently
  })

  useEffect(() => {
    if (!bootData) return
    if (bootData.mode && bootData.mode !== mode) {
      safeSetItem('mc-theme', bootData.mode)
      setMode(bootData.mode as ModePreference)
      setResolved(resolveMode(bootData.mode as ModePreference))
    }
    if (bootData.color && bootData.color !== colorTheme) {
      safeSetItem('mc-color-theme', bootData.color)
      setColorThemeState(bootData.color)
    }
    if (bootData.onboarded) {
      safeSetItem('mc-onboarded', '1')
      setOnboarded(true)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootData])  // Only react when boot data arrives

  useEffect(() => {
    applyTheme(colorTheme, resolved)
    bumpThemeVersion()
  }, [resolved, colorTheme, bumpThemeVersion])

  // Report the resolved accent to the Electron shell (if present) so the NEXT
  // launch's boot splash (loading.html) paints in the user's chosen colour.
  // Reads the computed --accent after paint; a no-op in a plain browser.
  useEffect(() => {
    const bridge = (window as unknown as {
      electronAPI?: { setThemeAccent?: (hex: string) => void }
    }).electronAPI
    if (!bridge?.setThemeAccent) return
    const id = requestAnimationFrame(() => {
      const hex = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()
      if (hex) bridge.setThemeAccent!(hex)
    })
    return () => cancelAnimationFrame(id)
  }, [resolved, colorTheme, themeVersion])

  useEffect(() => {
    const handler = (e: Event) => {
      const { mode: m, colorTheme: ct } = (e as CustomEvent).detail
      setMode(m)
      setResolved(resolveMode(m))
      setColorThemeState(ct)
    }
    window.addEventListener(SYNC_EVENT, handler)
    return () => window.removeEventListener(SYNC_EVENT, handler)
  }, [])

  useEffect(() => {
    if (mode !== 'system') return
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => setResolved(getSystemMode())
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [mode])

  const { mutate: persistTheme } = useMutation({
    mutationFn: (body: { mode?: string; color?: string; onboarded?: boolean }) =>
      api.updateThemeConfig(body),
  })

  const setMode_ = useCallback((pref: ModePreference) => {
    safeSetItem('mc-theme', pref)
    setMode(pref)
    setResolved(resolveMode(pref))
    const ct = (localStorage.getItem('mc-color-theme') as ColorTheme) || DEFAULT_COLOR_THEME
    broadcast(pref, ct)
    persistTheme({ mode: pref })
  }, [persistTheme])

  const cycleMode = useCallback(() => {
    const next: ModePreference = mode === 'system' ? 'light' : mode === 'light' ? 'dark' : 'system'
    setMode_(next)
  }, [mode, setMode_])

  const setColorTheme = useCallback((t: ColorTheme) => {
    safeSetItem('mc-color-theme', t)
    setColorThemeState(t)
    const m = (localStorage.getItem('mc-theme') as ModePreference) || 'system'
    broadcast(m, t)
    persistTheme({ color: t })
  }, [persistTheme])

  /** Add a new custom theme via API, inject CSS, and select it. */
  const addCustomTheme = useCallback(async (data: Omit<CustomThemeData, 'slug'> & { slug?: string }) => {
    const res = await api.createTheme(data)
    if (!res.ok) throw new Error(res.error || 'Failed to create theme')
    const theme: CustomThemeData = res.theme
    injectCustomThemeCSS(theme)
    await loadCustomThemes()
    setColorTheme(`custom-${theme.slug}`)
    broadcastCustomThemesChanged()
    return theme
  }, [loadCustomThemes, setColorTheme])

  /** Delete a custom theme via API. */
  const deleteCustomTheme = useCallback(async (slug: string) => {
    await api.deleteTheme(slug)
    removeCustomThemeCSS(slug)
    if (colorTheme === `custom-${slug}`) {
      setColorTheme(DEFAULT_COLOR_THEME)
    }
    await loadCustomThemes()
    broadcastCustomThemesChanged()
  }, [colorTheme, setColorTheme, loadCustomThemes])

  // Combined themes list: built-in + custom
  const allThemes: ThemeEntry[] = [...THEMES, ...REGISTERED_THEMES, ...customThemes]

  const markOnboarded = useCallback(() => {
    safeSetItem('mc-onboarded', '1')
    setOnboarded(true)
    persistTheme({ onboarded: true })
  }, [persistTheme])

  return {
    theme: resolved,
    preference: mode,
    cycle: cycleMode,
    setTheme: setMode_,
    colorTheme,
    setColorTheme,
    allThemes,
    customThemes,
    customThemeDataMap,
    themeVersion,
    onboarded,
    markOnboarded,
    addCustomTheme,
    deleteCustomTheme,
    loadCustomThemes,
  }
}
