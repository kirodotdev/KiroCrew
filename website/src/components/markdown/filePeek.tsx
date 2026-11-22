import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileCode } from 'lucide-react'
import { queryClient } from '../../api/queryClient'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { i18nT } from '../../i18n/t'
import { pathBasename } from '../../utils/pathBasename'
import { PREVIEW_LINK_PEEKS } from '../../utils/previewFlags'
import { keyboardFocused, TOUCH_REPLAY_WINDOW_MS } from '../InstantTip'
import { OverlayPortal, useCloseWhenCrewHidden } from '../overlayRoot'
import { LinkUnfurlCtx } from './contexts'
import { fetchFilePeek, filePeekQueryKey, filePeekWindow, FILE_PEEK_STALE_MS } from './filePeekRead'

/** How long a mouse rests on a file link before its peek opens: the pause the
 *  address bubble and an app's link-preview card wait, so every link pop-up
 *  means the same thing by "resting". */
export const FILE_PEEK_OPEN_MS = 300
/** How long the peek stays after the mouse leaves the link, so the pointer can
 *  cross into the panel to read it. */
export const FILE_PEEK_CLOSE_MS = 200
/** The panel's width; the frame also keeps it inside the window. */
const PANEL_W = 340
/** The longest line drawn whole; past it the line is cut, so one minified line
 *  cannot turn the panel into a horizontal scroll. */
const LINE_CAP = 160

/** The confirmed local file a link points at, with the line it references. */
export interface PeekFile { path: string; line: number; endLine?: number }

type PeekHandlers = Pick<React.DOMAttributes<HTMLAnchorElement>,
  'onPointerEnter' | 'onPointerLeave' | 'onPointerDown' | 'onFocus' | 'onBlur'>

interface Placement { left: number; top: number; below: boolean }

function placeUnder(el: HTMLElement): Placement {
  const r = el.getBoundingClientRect()
  const below = window.innerHeight - r.bottom >= 160 || r.top < 160
  return { left: r.left, top: below ? r.bottom + 4 : r.top - 4, below }
}

/** Whether `el` still shows, in the window and in `scroller` when that is the
 *  element that scrolled and it holds `el`. */
function stillInView(el: HTMLElement, scroller: EventTarget | null): boolean {
  const r = el.getBoundingClientRect()
  if (r.bottom < 0 || r.top > window.innerHeight) return false
  if (!(scroller instanceof Element) || scroller === el || !scroller.contains(el)) return true
  const box = scroller.getBoundingClientRect()
  return r.bottom >= box.top && r.top <= box.bottom
}

/**
 * The hover peek of one file link: a read-only panel the pointer can cross
 * into. The rules are the redaction markers' (`RedactionCards.tsx`): it opens
 * after a mouse rests on the link, or at once on keyboard focus; a press
 * (mouse, touch or pen) never opens it, so a tap still goes straight to the
 * click. A peek keyboard focus opened stays while the mouse passes over and
 * away; blur, Escape, or the link scrolling out of view closes it. One the
 * mouse opened keeps a short close delay so the pointer can reach the panel.
 *
 * Not `useInstantTip`: that bubble closes on mouse leave, so the pointer
 * cannot cross into it, and a focus-opened tip would close under a passing
 * mouse. The touch-replay window is the shared one, so the two cannot drift on
 * what counts as a tap.
 *
 * `enabled` false hands back inert handlers and keeps the peek closed, so the
 * caller can switch it per render without changing its hook order.
 */
function useFilePeekHover(enabled: boolean) {
  const [open, setOpen] = useState(false)
  const [place, setPlace] = useState<Placement | null>(null)
  const linkRef = useRef<HTMLAnchorElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  // A press focuses the link too, and on touch that focus lands only after the
  // tap ends; a focus this soon after a press must not open the peek.
  const pressedAt = useRef(-Infinity)
  // Set while keyboard focus holds the peek open; a pointer leave ignores it.
  const heldByFocus = useRef(false)
  const shown = open && enabled

  const schedule = (fn: () => void, ms: number) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(fn, ms)
  }
  const close = useCallback(() => {
    clearTimeout(timer.current)
    heldByFocus.current = false
    setOpen(false)
  }, [])
  const reveal = (viaFocus: boolean) => {
    clearTimeout(timer.current)
    if (viaFocus) heldByFocus.current = true
    if (linkRef.current) setPlace(placeUnder(linkRef.current))
    setOpen(true)
  }
  useEffect(() => { if (!enabled) close() }, [enabled, close])
  useEffect(() => () => clearTimeout(timer.current), [])
  // Hide the peek when the local Crew is hidden, so the window key listener is
  // unregistered before the remote Crew receives key events.
  useCloseWhenCrewHidden(shown, close)
  // Keep the panel on its link while the transcript scrolls under it, close it
  // once the link scrolls out of view, and close it on Escape. While focus is
  // on the link the Escape is caught on the way down and stopped, so a dialog
  // or drawer holding the transcript stays open and the first Escape closes
  // only the peek; one opened by hover alone lets the Escape through to the
  // focused composer or dialog.
  useEffect(() => {
    if (!shown) return
    const track = (e: Event) => {
      const el = linkRef.current
      if (!el) return
      if (stillInView(el, e.type === 'scroll' ? e.target : null)) setPlace(placeUnder(el))
      else close()
    }
    const escape = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (document.activeElement === linkRef.current) e.stopPropagation()
      close()
    }
    window.addEventListener('scroll', track, true)
    window.addEventListener('resize', track)
    window.addEventListener('keydown', escape, true)
    return () => {
      window.removeEventListener('scroll', track, true)
      window.removeEventListener('resize', track)
      window.removeEventListener('keydown', escape, true)
    }
  }, [shown, close])

  const mouseLeft = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse' && !heldByFocus.current) schedule(close, FILE_PEEK_CLOSE_MS)
  }
  const handlers: PeekHandlers = enabled ? {
    onPointerEnter: (e) => { if (e.pointerType === 'mouse') schedule(() => reveal(false), FILE_PEEK_OPEN_MS) },
    onPointerLeave: mouseLeft,
    onPointerDown: () => { pressedAt.current = performance.now(); close() },
    // Focus counts only when the browser would ring it, as the address bubble
    // decides, so the focus a mouse click leaves on the link paints nothing.
    onFocus: (e) => {
      if (performance.now() - pressedAt.current < TOUCH_REPLAY_WINDOW_MS) return
      if (keyboardFocused(e.currentTarget)) reveal(true)
    },
    onBlur: close,
  } : {}
  const panelHandlers = {
    onPointerEnter: (e: React.PointerEvent) => { if (e.pointerType === 'mouse') clearTimeout(timer.current) },
    onPointerLeave: mouseLeft,
  }
  return { shown, place, linkRef, panelRef, handlers, panelHandlers }
}

/**
 * The hover peek `MdAnchor` gives a link to a confirmed local file with a line
 * reference: the file's name and the reference as header, a dozen lines around
 * that line as plain text with the referenced line tinted, the file's full
 * path and "Click to open" as the footer. The click itself is untouched.
 *
 * Off unless the owner turned on `dashboard.feature_previews.link_peeks`, and
 * off while the message is still streaming (`LinkUnfurlCtx.live`): a link
 * whose text is still arriving is not one the reader meant to rest on. The
 * read (`filePeekRead.ts`) starts only once the peek is open, so a pointer
 * crossing the link costs nothing, and it is the viewer's own owner-gated,
 * redacted `/api/file-read`, asked for the lines shown and no more. A read
 * the gateway refuses, a binary file, or a gateway without the window leaves
 * the link exactly as it is today: no panel, and never a prompt.
 *
 * `file` null means the link gets no peek and the hook hands back inert
 * handlers.
 */
export function useFilePeek(file: PeekFile | null, panelId: string): {
  /** Whether this link has a peek: the caller then wires `handlers` and `ref`. */
  active: boolean
  handlers: PeekHandlers
  /** For the anchor's `ref`: the element the pointer rests on and focus lands on. */
  ref: (el: HTMLAnchorElement | null) => void
  panel: React.ReactNode
} {
  const flagOn = usePreviewFlag(PREVIEW_LINK_PEEKS)
  const { live } = useContext(LinkUnfurlCtx)
  const active = flagOn && !live && file !== null
  const hover = useFilePeekHover(active)
  const range = filePeekWindow(file?.line ?? 1)
  // The shared client is named on every query here: the renderer also mounts
  // outside a `QueryClientProvider` (file previews, artifact pages, specs).
  const read = useQuery({
    queryKey: filePeekQueryKey(file?.path ?? '', range),
    queryFn: ({ signal }) => fetchFilePeek(file?.path ?? '', range, file?.line ?? 1, signal),
    enabled: active && hover.shown,
    staleTime: FILE_PEEK_STALE_MS,
    retry: false,
  }, queryClient)
  const ref = (el: HTMLAnchorElement | null) => { hover.linkRef.current = el }
  if (!active || !file) return { active: false, handlers: {}, ref, panel: null }
  const data = read.data
  return {
    active: true,
    handlers: hover.handlers,
    ref,
    panel: hover.shown && hover.place && data?.ok ? (
      <FilePeekPanel
        id={panelId}
        file={file}
        lines={data.lines}
        start={data.start}
        place={hover.place}
        panelRef={hover.panelRef}
        handlers={hover.panelHandlers}
      />
    ) : null,
  }
}

/**
 * The panel: header (the file's name), the lines as plain text in a `<pre>`
 * with a line gutter and the referenced line(s) tinted, footer (the full path
 * and "Click to open"). Fixed under (or above) the link, clamped inside the
 * window, portalled to the overlay root so no `overflow: hidden` ancestor
 * clips it. Plain text only, never markdown: the body is whatever the file
 * holds. No `aria-describedby` on the link and no control in here: the body
 * is content the click opens in full, not a description of the link, and
 * assistive tech already reports a link's destination from its `href`.
 */
function FilePeekPanel({ id, file, lines, start, place, panelRef, handlers }: {
  id: string
  file: PeekFile
  lines: string[]
  start: number
  place: Placement
  panelRef: React.MutableRefObject<HTMLDivElement | null>
  handlers: Pick<React.DOMAttributes<HTMLDivElement>, 'onPointerEnter' | 'onPointerLeave'>
}) {
  // Clamp against the panel's real width, so a short panel near the right
  // edge stays under its link instead of moving by the full width.
  const [width, setWidth] = useState(PANEL_W)
  useLayoutEffect(() => {
    const w = panelRef.current?.offsetWidth
    if (w && w !== width) setWidth(w)
  }, [place, panelRef, width])
  const last = file.endLine ?? file.line
  const gutter = String(start + lines.length - 1).length
  // The header names the file and the reference the link carries, as the
  // link's own `:line` does; the footer keeps the full path.
  const title = `${pathBasename(file.path)}:${file.line}${last !== file.line ? `-${last}` : ''}`
  return (
    <OverlayPortal>
      <div
        ref={panelRef}
        id={id}
        role="tooltip"
        data-testid="file-peek-panel"
        {...handlers}
        className="fixed z-[130] w-[340px] rounded-lg border border-border bg-bg-elevated text-[12px] leading-[18px] text-card-fg shadow-lg"
        style={{
          maxWidth: `min(${PANEL_W}px, calc(100vw - 16px))`,
          left: Math.max(8, Math.min(place.left, window.innerWidth - width - 8)),
          top: place.below ? place.top : undefined,
          bottom: place.below ? undefined : window.innerHeight - place.top,
        }}
      >
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5 font-medium text-text-strong">
          <FileCode size={12} className="shrink-0" aria-hidden="true" />
          <span className="min-w-0 truncate" data-testid="file-peek-title">{title}</span>
        </div>
        <pre
          data-testid="file-peek-lines"
          className="m-0 overflow-hidden px-2.5 py-1.5 font-mono text-[11px] leading-[16px] text-text"
        >
          {lines.map((text, i) => {
            const n = start + i
            const hit = n >= file.line && n <= last
            return (
              <div key={n} data-peek-line={hit ? 'ref' : undefined} className={hit ? '-mx-2.5 bg-accent-subtle px-2.5' : undefined}>
                <span className="select-none text-muted" aria-hidden="true">{`${String(n).padStart(gutter, ' ')} `}</span>
                {text.length > LINE_CAP ? `${text.slice(0, LINE_CAP)}…` : text}
              </div>
            )
          })}
        </pre>
        <div className="border-t border-border px-2.5 py-1.5 text-[11px] leading-4 text-muted">
          <div className="break-all font-mono" data-testid="file-peek-path">{file.path}</div>
          <div className="mt-0.5">{i18nT('components.markdownRenderer.file_peek_click_to_open')}</div>
        </div>
      </div>
    </OverlayPortal>
  )
}
