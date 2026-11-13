import { useState, useEffect, useRef } from 'react'

/**
 * `value`, once it has stopped changing for `ms` milliseconds. The first render
 * returns `value` itself, so a consumer never starts from `undefined`.
 */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  // Read by the timer, not captured by it: a timer armed for an earlier value can
  // fire after a newer value has rendered but before this effect's cleanup clears
  // it (passive effects flush after the commit). Publishing that overtaken value
  // would move a consumer's query key off the newer value's in-flight request.
  const latestValueRef = useRef(value)
  latestValueRef.current = value
  useEffect(() => {
    const t = setTimeout(() => {
      if (Object.is(latestValueRef.current, value)) setDebounced(value)
    }, ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}
