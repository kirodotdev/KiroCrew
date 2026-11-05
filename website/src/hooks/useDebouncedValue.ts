import { useState, useEffect } from 'react'

/**
 * `value`, once it has stopped changing for `ms` milliseconds. The first render
 * returns `value` itself, so a consumer never starts from `undefined`.
 */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}
