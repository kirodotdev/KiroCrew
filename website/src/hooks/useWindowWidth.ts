import { useSyncExternalStore } from 'react'

/**
 * The live window width (`window.innerWidth`), re-rendering on resize.
 *
 * One store for every reader, so ChatPage's drawer and panel sizing and the
 * sidebar's own paint and drag ceiling read the same width in the same render.
 * Each subscriber hears the same `resize` event, and React reads the snapshot
 * at render time, so no reader can paint against a width another has not seen.
 */
function subscribe(onChange: () => void): () => void {
  window.addEventListener('resize', onChange)
  return () => window.removeEventListener('resize', onChange)
}

const getSnapshot = () => window.innerWidth

export function useWindowWidth(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
