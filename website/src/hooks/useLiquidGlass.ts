import { useCallback, useEffect, useState } from 'react'
import { applyLiquidGlass, persistLiquidGlass, readLiquidGlass } from '../utils/liquidGlass'

/**
 * State + setter for the "Translucent panels" display setting (opt-in, default
 * off) that turns the Liquid Glass primitive on.
 * The effect keeps the root `data-reduce-transparency` attribute in step with
 * the state, so flipping the switch re-renders every Liquid Glass pane at
 * once; the value persists in localStorage (browser-local, like the font
 * family). index.html applies the stored value before hydration, so mounting
 * this hook never causes a flash -- it only takes over ownership of the
 * attribute.
 */
export function useLiquidGlass(): { liquidGlass: boolean; setLiquidGlass: (on: boolean) => void } {
  const [liquidGlass, setState] = useState<boolean>(() => readLiquidGlass())
  useEffect(() => { applyLiquidGlass(liquidGlass) }, [liquidGlass])
  const setLiquidGlass = useCallback((on: boolean) => {
    persistLiquidGlass(on)
    setState(on)
  }, [])
  return { liquidGlass, setLiquidGlass }
}
