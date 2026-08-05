import { useState, useCallback, useMemo } from 'react'

export function usePanelState() {
  const [isOpen, setIsOpen] = useState(false)
  const [filePath, setFilePath] = useState('')
  const [content, setContent] = useState('')
  // Chat slot the panel was opened from. Comment submission routes here so
  // it lands in the document's originating session, not whatever session is
  // active when the user clicks "Submit All" after switching sessions.
  const [slot, setSlot] = useState<string | null>(null)

  const openPanel = useCallback((fp: string, c: string, originSlot: string | null = null) => {
    setFilePath(fp); setContent(c); setSlot(originSlot); setIsOpen(true)
  }, [])
  const closePanel = useCallback(() => {
    setIsOpen(false); setFilePath(''); setContent(''); setSlot(null)
  }, [])

  // Memoize the returned object so consumers wrapping it in useCallback /
  // useMemo / memo() see a stable reference between renders. Without this
  // every parent re-render produces a fresh `panel = {…}`, which cascades
  // into downstream `useCallback` deps and triggers child effect re-runs
  // (most visibly: DiffBlock's HEAD probe flickering its Open button).
  //
  // setContent is in the dep array even though React guarantees state
  // setters are stable across renders — kept for exhaustive-deps lint
  // compliance and to mirror the pattern in useTouchedFiles.
  return useMemo(
    () => ({ isOpen, filePath, content, slot, openPanel, closePanel, setContent }),
    [isOpen, filePath, content, slot, openPanel, closePanel, setContent],
  )
}

/** Side-panel state for the Monaco diff viewer triggered by file-change chips. */
export function useDiffPanel() {
  const [isOpen, setIsOpen] = useState(false)
  const [filePath, setFilePath] = useState('')
  const [original, setOriginal] = useState('')
  const [modified, setModified] = useState('')

  const openDiff = useCallback((fp: string, mod: string, orig: string = '') => {
    setFilePath(fp); setOriginal(orig); setModified(mod); setIsOpen(true)
  }, [])
  const closeDiff = useCallback(() => {
    setIsOpen(false); setFilePath(''); setOriginal(''); setModified('')
  }, [])

  // See note on usePanelState above — same stability guarantee.
  return useMemo(
    () => ({ isOpen, filePath, original, modified, openDiff, closeDiff }),
    [isOpen, filePath, original, modified, openDiff, closeDiff],
  )
}
