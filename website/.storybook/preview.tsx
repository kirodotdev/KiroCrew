import React from 'react'
import type { Decorator, Preview } from '@storybook/react-vite'
import { DecoratorHelpers } from '@storybook/addon-themes'
import { initI18n } from '../src/i18n/all'
import { THEMES, themeDataAttribute } from '../src/hooks/useTheme'
import '../src/index.css'

// Same reason `main.tsx` calls it before the first render: a primitive that reads
// `i18nT` ahead of initialization throws on a missing instance, not on a missing key.
// Booted through the all-languages entry, like every page entry, so a story renders
// the same catalogs the dashboard does rather than an English-only subset.
initI18n()

type Mode = 'dark' | 'light'

/**
 * One toolbar entry per shipped theme x mode. Both the list (`THEMES`) and the
 * attribute spelling (`themeDataAttribute`) come from `useTheme.tsx`, so a theme
 * added to the picker, or a renamed default palette, reaches the toolbar without
 * a second edit here.
 */
const THEME_KEYS: Record<string, { attr: string; mode: Mode }> = {}
for (const mode of ['dark', 'light'] as const) {
  for (const t of THEMES) {
    if (t.custom) continue
    const attr = themeDataAttribute(t.value, mode)
    THEME_KEYS[attr] = { attr, mode }
  }
}
const DEFAULT_THEME = 'dark'

DecoratorHelpers.initializeThemeState(Object.keys(THEME_KEYS), DEFAULT_THEME)

function ThemeAttributes({ theme }: { theme: string }) {
  React.useEffect(() => {
    const entry = THEME_KEYS[theme] ?? THEME_KEYS[DEFAULT_THEME]
    const el = document.documentElement
    // Both attributes, because the stylesheet keys palettes on `data-theme` and a
    // handful of mode-only rules (code backgrounds, the body backdrop) on `data-mode`.
    // Setting one without the other renders a palette with the wrong mode overlay.
    el.dataset.theme = entry.attr
    el.dataset.mode = entry.mode
  }, [theme])
  return null
}

const withDashboardTheme: Decorator = (Story, context) => {
  const selected = DecoratorHelpers.pluckThemeFromContext(context)
  const { themeOverride } = DecoratorHelpers.useThemeParameters(context) ?? {}
  const theme = themeOverride || selected || DEFAULT_THEME
  return (
    <>
      <ThemeAttributes theme={theme} />
      <div style={{ background: 'var(--bg)', color: 'var(--text)', minHeight: '100vh', padding: 24 }}>
        <Story />
      </div>
    </>
  )
}

const preview: Preview = {
  decorators: [withDashboardTheme],
  parameters: {
    // The decorator paints the token background itself, so the built-in
    // backgrounds toolbar would only fight it.
    backgrounds: { disable: true },
    layout: 'fullscreen',
    a11y: { test: 'error' },
  },
}

export default preview
