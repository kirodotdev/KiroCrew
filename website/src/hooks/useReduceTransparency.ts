import { useCallback, useEffect, useState } from 'react'
import { applyReduceTransparency, persistReduceTransparency, readReduceTransparency } from '../utils/reduceTransparency'

/**
 * State + setter for the "Reduce glass transparency" display setting. The
 * effect keeps the root `data-reduce-transparency` attribute in step with the
 * state, so flipping the switch re-renders every Liquid Glass pane at once;
 * the value persists in localStorage (browser-local, like the font family).
 * index.html applies the stored value before hydration, so mounting this hook
 * never causes a flash -- it only takes over ownership of the attribute.
 */
export function useReduceTransparency(): { reduceTransparency: boolean; setReduceTransparency: (on: boolean) => void } {
  const [reduceTransparency, setState] = useState<boolean>(() => readReduceTransparency())
  useEffect(() => { applyReduceTransparency(reduceTransparency) }, [reduceTransparency])
  const setReduceTransparency = useCallback((on: boolean) => {
    persistReduceTransparency(on)
    setState(on)
  }, [])
  return { reduceTransparency, setReduceTransparency }
}
