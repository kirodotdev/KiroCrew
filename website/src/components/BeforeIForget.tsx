/**
 * "Before I Forget" — a global floating scratchpad accessible from the topbar.
 *
 * Persists content to localStorage so it survives refreshes and restarts.
 * Designed as a quick capture surface: click, type, close. No formatting
 * toolbar, no file tree — just a textarea that remembers.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { StickyNote, X, Trash2 } from 'lucide-react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { i18nT } from '../i18n/t'
import Clickable from './Clickable'

const STORAGE_KEY = 'kirocrew:before-i-forget'
const SAVE_DEBOUNCE_MS = 400

export default function BeforeIForget() {
  const [open, setOpen] = useState(false)
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingText = useRef<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLDivElement>(null)

  // Load persisted content on mount
  useEffect(() => {
    const saved = safeGetItem(STORAGE_KEY)
    if (saved) setContent(saved)
  }, [])

  // Cross-window sync: another window writing this key must not be silently
  // overwritten by this one's mount-time copy. The `storage` event fires only
  // in OTHER windows, never the writer, so adopting newValue cannot loop.
  // An edit in flight HERE wins (saveTimer pending): adopting the remote text
  // mid-keystroke would discard what the user is typing right now — the
  // conflict then resolves last-writer-wins, which is the semantic a single
  // shared localStorage key can honestly offer.
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key !== STORAGE_KEY || e.newValue === null) return
      if (saveTimer.current !== null) return
      setContent(e.newValue)
      setDirty(false)
      setSaveFailed(false)
    }
    window.addEventListener('storage', handler)
    return () => window.removeEventListener('storage', handler)
  }, [])

  /** Drop any queued write. Callers that persist NOW must do this first, or the
   *  queued timer lands afterwards and re-writes the text it was meant to
   *  replace. */
  const cancelPendingSave = useCallback(() => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current)
      saveTimer.current = null
    }
    pendingText.current = null
  }, [])

  /** Land any queued write NOW, without touching state. Used where the debounce
   *  window is about to be cut short (page teardown, unmount): the text must
   *  reach storage, but a setState there would land on a dying tree. */
  const flushPendingSave = useCallback(() => {
    if (saveTimer.current === null || pendingText.current === null) return
    clearTimeout(saveTimer.current)
    saveTimer.current = null
    const text = pendingText.current
    pendingText.current = null
    safeSetItem(STORAGE_KEY, text)
  }, [])

  // Debounced save. The footer only reports "saved" when the write actually
  // landed — safeSetItem returns false on quota exhaustion / unavailable
  // storage, and that must surface rather than masquerade as saved.
  const persist = useCallback((text: string) => {
    cancelPendingSave()
    pendingText.current = text
    saveTimer.current = setTimeout(() => {
      saveTimer.current = null
      pendingText.current = null
      const ok = safeSetItem(STORAGE_KEY, text)
      setDirty(false)
      setSaveFailed(!ok)
    }, SAVE_DEBOUNCE_MS)
  }, [cancelPendingSave])

  // A queued write must not be LOST when the debounce window is cut short:
  // flush (not drop) on unmount and on page teardown. Refreshing within 400ms
  // of the last keystroke used to discard the newest text. The flush performs
  // no setState, so it is safe against an unmounting tree.
  useEffect(() => flushPendingSave, [flushPendingSave])
  useEffect(() => {
    window.addEventListener('pagehide', flushPendingSave)
    return () => window.removeEventListener('pagehide', flushPendingSave)
  }, [flushPendingSave])

  const handleChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const text = e.target.value
    setContent(text)
    setDirty(true)
    persist(text)
  }, [persist])

  const handleClear = useCallback(() => {
    // Cancel first: a debounced write queued by the keystroke that preceded the
    // click is still pending, and it would restore the text the user just
    // cleared.
    cancelPendingSave()
    setContent('')
    setDirty(false)
    const ok = safeSetItem(STORAGE_KEY, '')
    setSaveFailed(!ok)
    textareaRef.current?.focus()
  }, [cancelPendingSave])

  // Focus textarea when panel opens
  useEffect(() => {
    if (open) {
      // Small delay to let animation start
      const t = setTimeout(() => textareaRef.current?.focus(), 100)
      return () => clearTimeout(t)
    }
  }, [open])

  // Close on Escape
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        e.stopPropagation()
      }
    }
    document.addEventListener('keydown', handler, true)
    return () => document.removeEventListener('keydown', handler, true)
  }, [open])

  // Close on click outside. The trigger is excluded: its own mousedown landing
  // here would close the panel, and the click that follows would toggle it
  // straight back open — making toggle-to-close dead in a real browser.
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target)) return
      if (panelRef.current && !panelRef.current.contains(target)) {
        setOpen(false)
      }
    }
    // Use timeout so the opening click doesn't immediately close
    const t = setTimeout(() => document.addEventListener('mousedown', handler), 0)
    return () => {
      clearTimeout(t)
      document.removeEventListener('mousedown', handler)
    }
  }, [open])

  const charCount = content.length

  return (
    <>
      <Clickable
        ref={triggerRef}
        className="relative flex items-center justify-center w-7 h-7 rounded-md hover:bg-card-hl transition-colors"
        onClick={() => setOpen(o => !o)}
        title={i18nT('app.before_i_forget')}
        aria-label={i18nT('app.before_i_forget')}
        aria-expanded={open}
      >
        <StickyNote size={15} className={open ? 'text-accent' : ''} />
        {/* Dot indicator when scratchpad has content */}
        {content.length > 0 && !open && (
          <span className="absolute top-0.5 right-0.5 w-1.5 h-1.5 rounded-full bg-accent" />
        )}
      </Clickable>

      <AnimatePresence>
        {open && (
          <motion.div
            ref={panelRef}
            initial={{ opacity: 0, y: -8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.96 }}
            transition={{ duration: 0.15 }}
            // Narrow-first geometry: the width tracks the viewport (capped at
            // the former fixed 20rem) and the right inset shrinks, so the panel
            // stays fully on-screen down to 320px instead of clipping its left
            // edge off-screen.
            className="fixed top-12 right-4 sm:right-14 z-[90] w-[calc(100vw-2rem)] max-w-80 bg-card border border-border rounded-xl shadow-xl flex flex-col overflow-hidden"
            role="dialog"
            aria-modal="false"
            aria-label={i18nT('app.before_i_forget')}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-3 py-2 border-b border-border">
              <span className="text-[13px] font-semibold text-text-strong">
                {i18nT('app.before_i_forget')}
              </span>
              <div className="flex items-center gap-1">
                {content.length > 0 && (
                  <Clickable
                    className="flex items-center justify-center w-6 h-6 rounded hover:bg-danger/10 transition-colors"
                    onClick={handleClear}
                    aria-label={i18nT('app.before_i_forget_clear')}
                    title={i18nT('app.before_i_forget_clear')}
                  >
                    <Trash2 size={13} className="text-muted hover:text-danger" />
                  </Clickable>
                )}
                <Clickable
                  className="flex items-center justify-center w-6 h-6 rounded hover:bg-card-hl transition-colors"
                  onClick={() => setOpen(false)}
                  aria-label={i18nT('app.close')}
                >
                  <X size={14} className="text-muted" />
                </Clickable>
              </div>
            </div>

            {/* Textarea */}
            <textarea
              ref={textareaRef}
              value={content}
              onChange={handleChange}
              placeholder={i18nT('app.before_i_forget_placeholder')}
              aria-label={i18nT('app.before_i_forget')}
              // The cue for `outline-none` is an INSET ring, not the global
              // `:focus-visible` outline: that one is drawn at
              // `outline-offset:2px`, and this textarea runs edge-to-edge inside
              // a panel with `overflow-hidden`, so an outset ring would be
              // clipped on exactly the sides it needs to appear on. Inset also
              // avoids the layout shift a focus border would cause on a
              // `border-none` element.
              className="flex-1 min-h-[200px] max-h-[400px] resize-y px-3 py-2.5 text-[13px] leading-relaxed text-text bg-transparent border-none outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent placeholder:text-muted/60 font-mono"
              spellCheck={false}
            />

            {/* Footer */}
            <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-[11px] text-muted">
              <span>{charCount > 0 ? i18nT('app.before_i_forget_chars', { count: charCount }) : ''}</span>
              <span className={!dirty && saveFailed ? 'text-danger' : undefined}>
                {dirty
                  ? i18nT('app.before_i_forget_saving')
                  : saveFailed
                    ? i18nT('app.before_i_forget_save_failed')
                    : charCount > 0
                      ? i18nT('app.before_i_forget_saved')
                      : ''}
              </span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}
