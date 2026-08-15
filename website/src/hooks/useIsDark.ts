import { useEffect, useState } from 'react'

/** Reactively track dark/light theme via MutationObserver on data-theme. */
export function useIsDark(): boolean {
  const [dark, setDark] = useState(
    () => (document.documentElement.getAttribute('data-theme') || '').includes('dark'),
  )
  useEffect(() => {
    const obs = new MutationObserver(() =>
      setDark((document.documentElement.getAttribute('data-theme') || '').includes('dark')),
    )
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return dark
}
