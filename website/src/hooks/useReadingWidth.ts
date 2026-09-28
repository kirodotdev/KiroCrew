import { safeSetItem } from '../utils/safeStorage'
import { useState } from 'react'

export type ReadingWidth = 'md' | 'full'

const LS_KEY = 'mc-reading-width'

export function useReadingWidth() {
  const [readingWidth, setReadingWidth] = useState<ReadingWidth>(() => {
    const stored = localStorage.getItem(LS_KEY)
    return stored === 'full' ? 'full' : 'md'
  })

  const toggle = () => {
    const next: ReadingWidth = readingWidth === 'md' ? 'full' : 'md'
    setReadingWidth(next)
    safeSetItem(LS_KEY, next)
  }

  const previewStyle: React.CSSProperties | undefined = readingWidth === 'md'
    ? { maxWidth: 'var(--mc-content-width, 900px)', margin: '0 auto' }
    : undefined

  return { readingWidth, toggle, previewStyle }
}
