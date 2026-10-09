/**
 * The ThemeProvider settles its boot read after the first render. A test that
 * renders this probe beside its component and awaits `themeBootSettled()`
 * waits for that state, so the provider's update never lands after the test's
 * last barrier (an act() warning under KIROCREW_ACT_STRICT=1).
 */
import { screen } from '@testing-library/react'
import { useTheme } from '../hooks/useTheme'

export function ThemeBootProbe() {
  return useTheme().themeBootReady ? <span data-testid="theme-boot-settled" /> : null
}

export const themeBootSettled = () => screen.findByTestId('theme-boot-settled')
