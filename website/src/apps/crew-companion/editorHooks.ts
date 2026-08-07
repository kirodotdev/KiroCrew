/**
 * Shared hooks for pack editors.
 */
import { useState, useCallback } from 'react'
import { galleryApi } from './petBridge'
import { i18nT } from '../../i18n/t'

const api = galleryApi

/** Hook: fetch language from config */
export function useLang() {
  const [lang, setLang] = useState('en')
  // Use useState initializer to avoid re-fetching
  useState(() => {
    api?.getCrewCompanionConfig?.().then((c: any) => { if (c?.language) setLang(c.language) })
  })
  return { i18nT, lang }
}

/** Hook: save-with-dialog logic for editing existing packs.
 *  Only shows dialog when isDirty is true and editing an existing pack.
 *  If not dirty, saves directly (overwrite). */
export function useSaveWithDialog(existingPack: { id: string } | undefined, isDirty: boolean) {
  const [showSaveDialog, setShowSaveDialog] = useState(false)

  const triggerSave = useCallback((doSave: (asNew: boolean) => void) => {
    if (existingPack && isDirty) {
      setShowSaveDialog(true)
    } else {
      doSave(false)
    }
  }, [existingPack, isDirty])

  const confirmOverwrite = useCallback((doSave: (asNew: boolean) => void) => {
    setShowSaveDialog(false)
    doSave(false)
  }, [])

  const confirmSaveNew = useCallback((doSave: (asNew: boolean) => void) => {
    setShowSaveDialog(false)
    doSave(true)
  }, [])

  const cancelDialog = useCallback(() => setShowSaveDialog(false), [])

  return { showSaveDialog, triggerSave, confirmOverwrite, confirmSaveNew, cancelDialog }
}
