import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import {
  LIQUID_GLASS_CHANGED_EVENT,
  applyLiquidGlass,
  liquidGlassFromEvent,
  notifyLiquidGlassChanged,
  persistLiquidGlass,
  readLiquidGlass,
  toTranslucentPanelsStored,
} from '../utils/liquidGlass'

/**
 * State + setter for the "Translucent panels" display setting that turns the
 * Liquid Glass primitive on. Off is the stored default (an absent key); the
 * first-run "Pick your look" step turns it on for a fresh install, and from
 * then on the user's choice holds (`components/OnboardingFlow.tsx`).
 * The effect keeps the root `data-reduce-transparency` attribute in step with
 * the state, so flipping the switch re-renders every Liquid Glass pane at
 * once. The value lives on the workspace (`dashboard.translucent_panels`,
 * written here through `PUT /api/config/theme` and read back at boot by
 * `useTheme`), with localStorage as the render cache; index.html applies the
 * cached value before hydration, so mounting this hook never causes a flash --
 * it only takes over ownership of the attribute. Every mounted instance
 * follows `LIQUID_GLASS_CHANGED_EVENT`, so the Settings row and the first-run
 * row agree with each other and with the boot adoption.
 */
export function useLiquidGlass(): { liquidGlass: boolean; setLiquidGlass: (on: boolean) => void } {
  const [liquidGlass, setState] = useState<boolean>(() => readLiquidGlass())
  useEffect(() => { applyLiquidGlass(liquidGlass) }, [liquidGlass])
  useEffect(() => {
    // The event carries the value: with site storage blocked a re-read would
    // answer the solid default and undo the flip the user just made.
    const onChanged = (event: Event) => setState(liquidGlassFromEvent(event))
    window.addEventListener(LIQUID_GLASS_CHANGED_EVENT, onChanged)
    return () => window.removeEventListener(LIQUID_GLASS_CHANGED_EVENT, onChanged)
  }, [])
  const setLiquidGlass = useCallback((on: boolean) => {
    persistLiquidGlass(on)
    setState(on)
    notifyLiquidGlassChanged(on)
    // Best effort, off the flip's own call stack (the same lazy shape as
    // useTheme's persistTheme mutation): the browser copy already holds the
    // choice for this session, and a workspace that could not be written
    // simply keeps its last value, so nothing here may fail the flip.
    void Promise.resolve()
      .then(() => api.updateThemeConfig({ translucent_panels: toTranslucentPanelsStored(on) }))
      .catch(() => {})
  }, [])
  return { liquidGlass, setLiquidGlass }
}
