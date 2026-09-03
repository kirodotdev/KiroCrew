import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import type { ExcalidrawImperativeAPI } from '@excalidraw/excalidraw/types'
import type { AppState as ExcalidrawAppState } from '@excalidraw/excalidraw/types'
import type * as ExcalidrawModuleType from '@excalidraw/excalidraw'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { Dialog, DialogContent, DialogTitle } from './ui/dialog'
import ErrorBoundary from './ErrorBoundary'
import { i18nT } from '../i18n/t'
import { useLanguage } from '../i18n/LanguageProvider'

/**
 * Sketch pad: an Excalidraw whiteboard in a modal, opened from the composer's
 * pencil button. "Insert" exports the scene as a PNG (what a human reads) plus
 * the .excalidraw JSON sidecar (what an agent reads — element geometry and
 * labels beat pixels) and hands both to the composer's regular attachment
 * pipeline, so server-side validation, resizing, and attachment chips are all
 * reused unchanged.
 *
 * Excalidraw is ~1MB, so the component AND its stylesheet load lazily on first
 * open; the main bundle carries only this wrapper. The last scene is kept in a
 * ref for the lifetime of the composer, so reopening the dialog restores the
 * previous drawing instead of a blank canvas.
 */

/** The loaded Excalidraw module, captured when the lazy chunk resolves. The
 *  persistence path needs `serializeAsJSON`/`restore` — the LIBRARY's own
 *  round-trip — because a raw `JSON.stringify(getAppState())` snapshot is
 *  fragile: live appState holds non-JSON-safe fields (collaborators is a Map)
 *  and its schema shifts across Excalidraw versions, and a scene that fails
 *  to restore would crash the mount with no way to clear the storage from
 *  the UI. `restore()` exists precisely to normalize scenes of any vintage. */
let excalidrawModule: typeof ExcalidrawModuleType | null = null

const Excalidraw = lazy(() => {
  // MUST be set before the module executes: Excalidraw's font machinery
  // resolves its lazily-loaded canvas fonts against this base, and without it
  // falls back to a third-party CDN (esm.sh) — which an air-gapped dashboard
  // can't reach and a private one shouldn't. The path is emitted into the
  // built dist (and served in dev) by vite.config's excalidrawFontsPlugin.
  ;(window as { EXCALIDRAW_ASSET_PATH?: string }).EXCALIDRAW_ASSET_PATH = '/vendor/excalidraw/'
  return Promise.all([
    import('@excalidraw/excalidraw'),
    // Vite splits the stylesheet into the same lazy chunk group; Excalidraw
    // renders unstyled without it.
    import('@excalidraw/excalidraw/index.css'),
  ]).then(([mod]) => {
    excalidrawModule = mod
    return { default: mod.Excalidraw }
  })
})

/** Languages Excalidraw ships translations for, keyed loosely by our tags.
 *  Anything unmapped falls back to English inside Excalidraw itself. */
const EXCALIDRAW_LANG: Record<string, string> = {
  'zh-CN': 'zh-CN',
  de: 'de-DE',
  es: 'es-ES',
  fr: 'fr-FR',
  hi: 'hi-IN',
  it: 'it-IT',
  ja: 'ja-JP',
  ko: 'ko-KR',
  pt: 'pt-PT',
  ru: 'ru-RU',
}

interface SketchDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Receives the exported files (PNG + .excalidraw source) on insert. */
  onInsert: (files: File[]) => void
  /** Focused when the dialog closes — the launcher row unmounts with the menu,
   *  so Radix's default restore target is `body`. */
  returnFocusRef?: React.RefObject<HTMLElement | null>
}

/** Where the last scene survives a reload or session switch — the drawing
 *  peer of `chatDrafts.ts`'s text-draft persistence. One key for the whole
 *  dashboard: split panes share it, last write wins, matching how a single
 *  physical sketch pad would behave. */
const SCENE_STORAGE_KEY = 'mc-sketch-scene'
/** Scenes with embedded images can outgrow localStorage's quota; past this
 *  size the scene stays in memory only rather than risking a quota throw. */
const SCENE_PERSIST_MAX_CHARS = 1_500_000

type StoredScene = {
  elements: readonly unknown[]
  appState: Record<string, unknown>
  files: unknown
}

/** Read the persisted scene THROUGH the library's `restore()`, which
 *  normalizes scenes of any vintage (including ones written by an older
 *  Excalidraw). A scene that fails to restore is dropped from storage right
 *  here — never handed to the mount, where a throw would leave the pad
 *  permanently broken for this browser profile (Suspense catches lazy
 *  loading, not render errors). Callable only once the lazy module is in;
 *  before that there is nothing to render a scene with anyway. */
function readStoredScene(): StoredScene | null {
  try {
    const raw = safeGetItem(SCENE_STORAGE_KEY)
    if (!raw || !excalidrawModule) return null
    const parsed = JSON.parse(raw)
    const restored = excalidrawModule.restore(parsed, null, null)
    if (!restored.elements.length) return null
    return {
      elements: restored.elements,
      appState: restored.appState as unknown as Record<string, unknown>,
      files: restored.files,
    }
  } catch {
    try {
      localStorage.removeItem(SCENE_STORAGE_KEY)
    } catch {
      // Storage unavailable — nothing to drop.
    }
    return null
  }
}

export default function SketchDialog({ open, onOpenChange, onInsert, returnFocusRef }: SketchDialogProps) {
  const { resolved: uiLanguage } = useLanguage()
  const apiRef = useRef<ExcalidrawImperativeAPI | null>(null)
  /** Last scene — elements, appState AND the file map (embedded images live
   *  there, not in elements) — kept in a ref between opens. localStorage
   *  seeding does NOT happen here: at first render the lazy module (whose
   *  `restore()` the read path requires) is not in yet, so the stored scene
   *  is loaded through the promise form of `initialData` instead. */
  const sceneRef = useRef<StoredScene | null>(null)
  const persistTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [hasElements, setHasElements] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [exportFailed, setExportFailed] = useState(false)
  /** Two-step confirm for "New sketch": first click arms (button restates the
   *  act as "Discard drawing?"), second click within the window executes.
   *  One click must never destroy the only copy of a drawing the pad
   *  otherwise teaches users always survives. */
  const [discardArmed, setDiscardArmed] = useState(false)
  const discardTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** True between a successful insert and the next canvas change: the reopened
   *  pad then says the drawing is already on the message instead of offering
   *  to attach it again with no cue. */
  const [attached, setAttached] = useState(false)

  /** The wrapper stays mounted while the dialog is closed, so a failure from
   *  the previous session would otherwise greet the next open as a stale
   *  red alert. */
  useEffect(() => {
    if (open) setExportFailed(false)
  }, [open])

  // The resolved display mode ('dark' | 'light') is what to paint; reading it
  // at render time keeps this component free of the theme hook's re-renders.
  const mode = typeof document !== 'undefined' && document.documentElement.dataset.mode === 'light' ? 'light' : 'dark'

  const handleApi = useCallback((api: ExcalidrawImperativeAPI) => {
    apiRef.current = api
    setHasElements(api.getSceneElements().length > 0)
  }, [])

  const handleChange = useCallback(() => {
    const api = apiRef.current
    if (!api) return
    const elements = api.getSceneElements()
    sceneRef.current = {
      elements,
      appState: api.getAppState() as unknown as Record<string, unknown>,
      files: api.getFiles(),
    }
    setHasElements(elements.length > 0)
    setAttached(false)
    // Debounced localStorage write: Excalidraw fires onChange per pointer
    // move, and a synchronous serialize of a large scene on every event
    // would jank the stroke being drawn. The payload is `serializeAsJSON`
    // output — the library's own scene-file format — so the read path's
    // `restore()` accepts it across Excalidraw versions.
    if (persistTimer.current) clearTimeout(persistTimer.current)
    persistTimer.current = setTimeout(() => {
      try {
        const liveApi = apiRef.current
        const mod = excalidrawModule
        if (!liveApi || !mod) return
        const live = liveApi.getSceneElements()
        if (!live.length) {
          localStorage.removeItem(SCENE_STORAGE_KEY)
          return
        }
        const raw = mod.serializeAsJSON(live, liveApi.getAppState(), liveApi.getFiles(), 'local')
        if (raw.length <= SCENE_PERSIST_MAX_CHARS) {
          // safeSetItem, not bare setItem: under quota pressure it reclaims
          // lower-tier keys before giving up, where a bare catch drops the scene.
          safeSetItem(SCENE_STORAGE_KEY, raw)
        } else {
          // An oversized scene cannot be persisted — but leaving the previous
          // snapshot in place would make a reload silently roll the drawing
          // back to that stale state. No stored scene beats a wrong one; the
          // in-memory ref still covers reopen within this page's lifetime.
          localStorage.removeItem(SCENE_STORAGE_KEY)
        }
      } catch {
        // Quota or serialization failure: in-memory restore still works.
      }
    }, 500)
  }, [])

  const handleInsert = useCallback(async () => {
    const api = apiRef.current
    if (!api || exporting) return
    const elements = api.getSceneElements()
    if (!elements.length) return
    setExporting(true)
    setExportFailed(false)
    try {
      // The module is necessarily loaded once Insert is clickable (the button
      // lives behind the lazy boundary), so no second dynamic import.
      const mod = excalidrawModule
      if (!mod) return
      const appState = api.getAppState()
      const files = api.getFiles()
      const blob = await mod.exportToBlob({
        elements,
        appState: { ...appState, exportBackground: true },
        files,
        mimeType: 'image/png',
      })
      // Local-time stamp, mirroring nameClipboardImage's pasted-image naming.
      const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      const png = new File([blob], `sketch-${ts}.png`, { type: 'image/png' })
      // ".excalidraw" is the scene's own extension: the dashboard's read-only
      // scene renderer (FileRenderers) routes on it, so the chip renders as a
      // drawing instead of a wall of raw JSON, and external Excalidraw tooling
      // opens it directly. The agent still reads it fine — file readers are
      // extension-agnostic for text, and the structured scene (element
      // geometry, labels) is what lets a crew read the drawing as data.
      const json = mod.serializeAsJSON(elements, appState, files, 'local')
      const sidecar = new File([json], `sketch-${ts}.excalidraw`, { type: 'application/json' })
      onInsert([png, sidecar])
      setAttached(true)
      // Deliberately NOT clearing the scene here: onInsert hands the files to
      // the upload pipeline and returns before the server accepts them, so a
      // clear at this point would discard the only copy of a drawing whose
      // upload then fails. The visible "New sketch" button in the header is
      // the explicit path to a blank canvas.
      onOpenChange(false)
    } catch {
      // Offline chunk fetch or an oversized-canvas export rejection: without
      // this line the spinner just stops and Insert looks like a dead end.
      setExportFailed(true)
    } finally {
      setExporting(false)
    }
  }, [exporting, onInsert, onOpenChange])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        maxWidth={1100}
        className="w-[min(1100px,94vw)] h-[min(720px,88vh)] p-0 gap-0 flex flex-col overflow-hidden"
        data-testid="sketch-dialog"
        // Radix's DismissableLayer catches Escape in the CAPTURE phase — before
        // Excalidraw's own handlers — so Escape meant to finish a text label or
        // cancel a shape would dismiss the whole modal. Yield Escape to the
        // canvas whenever an element is being edited or is selected; a second
        // Escape (idle canvas) still closes the dialog.
        onEscapeKeyDown={e => {
          const api = apiRef.current
          if (!api) return
          // Typed access on purpose (no `as unknown` escape hatch): an
          // Excalidraw upgrade that renames these appState fields must fail
          // tsc here, not silently resume dismissing the modal mid-edit.
          const s: ExcalidrawAppState = api.getAppState()
          const busy = Boolean(s.editingTextElement) || Boolean(s.newElement)
            || Object.keys(s.selectedElementIds ?? {}).length > 0
          if (busy) e.preventDefault()
        }}
        // Radix records `body` as the restore target because the Sketch menu
        // row unmounts in the same commit that mounts this dialog — so without
        // this, Escape/close strands keyboard focus at the top of the document
        // (same fix as AgentSelector's onCloseAutoFocus).
        onCloseAutoFocus={e => {
          e.preventDefault()
          returnFocusRef?.current?.focus()
        }}
      >
        {/* flex-wrap + min-h (not a fixed h-12): on a phone-width window the
            hint and buttons wrap onto a second header line instead of the
            no-wrap row clipping the accent CTA under the close X. The hint
            stays visible at every width — it is the only place the two-chip
            outcome is explained, and mobile is where the unexplained chip
            confuses most. */}
        <div className="flex items-center flex-wrap gap-x-3 gap-y-1 px-4 py-2 min-h-12 shrink-0 border-b border-border">
          <DialogTitle className="text-sm font-semibold m-0 truncate min-w-0">
            {i18nT('components.sketchDialog.title')}
          </DialogTitle>
          <div className="flex-1 min-w-0" />
          {exportFailed && (
            <span role="alert" className="text-[12px] text-danger">
              {i18nT('components.sketchDialog.export_failed')}
            </span>
          )}
          {!exportFailed && (
            // Wraps (no truncate): this is the only place the two-chip outcome
            // is explained, and phone width is where it matters most — the
            // wrapping header absorbs the extra line.
            <span className="text-[11.5px] text-muted min-w-0">
              {!hasElements
                ? i18nT('components.sketchDialog.draw_something_first')
                : attached
                  ? i18nT('components.sketchDialog.already_attached')
                  : i18nT('components.sketchDialog.insert_hint')}
            </span>
          )}
          <button
            // mr-10, not mr-6: the dialog's built-in close X occupies the last
            // 44px before the right edge and renders on top — a narrower
            // margin lets an edge-click on the primary CTA hit Close instead.
            className="text-[13px] px-3.5 py-1.5 rounded-lg font-semibold bg-accent text-accent-fg border-none cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed hover:bg-accent-hover transition-colors mr-10 shrink-0"
            onClick={handleInsert}
            disabled={!hasElements || exporting}
            title={hasElements ? undefined : i18nT('components.sketchDialog.draw_something_first')}
            aria-label={i18nT('components.sketchDialog.attach_to_message')}
          >
            {exporting
              ? <Loader2 size={14} className="animate-spin lucide-inline" />
              : i18nT('components.sketchDialog.attach_to_message')}
          </button>
        </div>
        <div className="flex-1 min-h-0">
          {open && (
            // A rejected lazy import (offline with an uncached chunk) throws
            // through Suspense at render time — without this boundary it
            // climbs to the chat route's ErrorBoundary and replaces the whole
            // composer. Keyed by `open` so closing and reopening the dialog
            // retries the load once the network is back.
            <ErrorBoundary
              key={String(open)}
              fallback={
                <div className="w-full h-full flex items-center justify-center gap-2 text-muted text-[13px]">
                  {i18nT('components.sketchDialog.load_failed')}
                </div>
              }
            >
              <Suspense
              fallback={
                <div className="w-full h-full flex items-center justify-center gap-2 text-muted text-[13px]">
                  <Loader2 size={16} className="animate-spin lucide-inline" />
                  {i18nT('components.sketchDialog.loading')}
                </div>
              }
            >
              <Excalidraw
                excalidrawAPI={handleApi}
                onChange={handleChange}
                theme={mode}
                langCode={EXCALIDRAW_LANG[uiLanguage] ?? 'en'}
                // Function form: evaluated after the lazy module resolves, so
                // the localStorage fallback can run through `restore()`. The
                // in-memory scene wins (it is newer than or equal to storage);
                // `restore()` output needs no collaborators patch — it
                // normalizes appState itself.
                initialData={() => {
                  const scene = sceneRef.current ?? readStoredScene()
                  if (!scene) return null
                  return {
                    elements: scene.elements as never,
                    appState: { ...(scene.appState as object), collaborators: new Map() } as never,
                    files: scene.files as never,
                  }
                }}
                // New sketch lives in the CANVAS's own top-right slot, not
                // the dialog header: the header already carries the primary
                // CTA plus the built-in close X, and a third peer action
                // there breaks the two-button row cap. The canvas toolbar
                // is a structurally separate region, and the discard action
                // sits next to the drawing it discards.
                renderTopRightUI={() => (
                  <button
                    className={`text-[12px] px-2.5 py-1 rounded-lg bg-transparent border cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0 ${
                      discardArmed
                        ? 'text-danger border-danger hover:bg-danger/10'
                        : 'text-muted border-border hover:text-text hover:bg-bg-hover'
                    }`}
                    onClick={() => {
                      if (!discardArmed) {
                        setDiscardArmed(true)
                        if (discardTimer.current) clearTimeout(discardTimer.current)
                        // Arm window: long enough to read the restated label, short
                        // enough that a stale armed state can't ambush a later click.
                        discardTimer.current = setTimeout(() => setDiscardArmed(false), 4000)
                        return
                      }
                      if (discardTimer.current) clearTimeout(discardTimer.current)
                      setDiscardArmed(false)
                      const api = apiRef.current
                      if (!api) return
                      api.resetScene()
                      if (persistTimer.current) clearTimeout(persistTimer.current)
                      sceneRef.current = null
                      setHasElements(false)
                      try {
                        localStorage.removeItem(SCENE_STORAGE_KEY)
                      } catch {
                        // Storage unavailable — the in-memory clear already covers reopen.
                      }
                    }}
                    disabled={!hasElements || exporting}
                    aria-label={discardArmed
                      ? i18nT('components.sketchDialog.confirm_discard')
                      : i18nT('components.sketchDialog.new_sketch')}
                  >
                    {discardArmed
                      ? i18nT('components.sketchDialog.confirm_discard')
                      : i18nT('components.sketchDialog.new_sketch')}
                  </button>
                )}
                UIOptions={{
                  canvasActions: {
                    // The dialog's Insert button is the one export path; hiding
                    // Excalidraw's own save/export menu avoids two competing
                    // "save" affordances in one modal.
                    export: false,
                    saveToActiveFile: false,
                    loadScene: false,
                  },
                }}
              />
            </Suspense>
            </ErrorBoundary>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
