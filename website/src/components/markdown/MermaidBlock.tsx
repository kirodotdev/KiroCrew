import { memo, useEffect, useId, useRef, useState } from 'react'
import { Check, Copy, Download, FileCode, Loader2, Maximize2, MoreHorizontal } from 'lucide-react'
import { mermaidFontCss } from '../mermaidFontCss'
import { downloadBlob } from '../../utils/download'
import { copyCode } from '../../utils/clipboard'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../../utils/touchActions'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../ui/dropdown-menu'
import ErrorNotice from '../ErrorNotice'
import DiagramLightbox from '../DiagramLightbox'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { i18nT } from '../../i18n/t'

function isDarkTheme(): boolean {
  return (document.documentElement.getAttribute('data-theme') || '').includes('dark')
}

/**
 * mermaid, loaded on first use.
 *
 * mermaid plus its eager dependencies are ~90-130 KB gzip, and this module is
 * on the critical path (every chat message renders through it) while a
 * ```mermaid fence is rare. A static import therefore put the whole diagram
 * engine in the entry chunk for every user. `MermaidBlock` already renders
 * asynchronously inside an effect, so deferring the module costs nothing.
 *
 * The promise is cached at module scope so N diagram blocks share one load, and
 * `import()` itself is idempotent regardless.
 */
type MermaidApi = typeof import('mermaid')['default']

let mermaidLoad: Promise<MermaidApi> | null = null

function loadMermaid(): Promise<MermaidApi> {
  if (!mermaidLoad) mermaidLoad = import('mermaid').then(m => m.default)
  return mermaidLoad
}

function initMermaid(mermaid: MermaidApi): void {
  const dark = isDarkTheme()
  mermaid.initialize({
    startOnLoad: false,
    theme: dark ? 'dark' : 'default',
    themeVariables: dark ? {
      primaryColor: '#f59e32',
      primaryTextColor: '#e8e6e3',
      primaryBorderColor: '#3a3a3a',
      lineColor: '#888',
      secondaryColor: '#2a2a2a',
      tertiaryColor: '#1a1a1a',
    } : {
      primaryColor: '#f59e32',
      primaryTextColor: '#1a1a1a',
      primaryBorderColor: '#ccc',
      lineColor: '#666',
      secondaryColor: '#fff3e0',
      tertiaryColor: '#f5f5f5',
    },
    securityLevel: 'strict',
    fontFamily: 'inherit',
    // Throw on parse errors instead of injecting mermaid's error diagram into
    // a temp <div id="dmermaid-*"> on document.body. That temp node is leaked
    // when render() throws (cleanup only runs on success), so failed blocks
    // accumulated orphaned 512px error SVGs in the DOM. With this on, the
    // MermaidBlock .catch() shows a clean inline <pre> and nothing leaks.
    suppressErrorRendering: true,
  })
}

/** Chrome shared by the diagram action row's buttons. The padding is kept an
 *  UNVARIATED base utility on purpose: `HOVER_NONE_ACTIONS_ROW_CLS` grows the
 *  touch target with `[&_button]:p-3`, which wins by Tailwind's
 *  variant-after-base ordering rather than by specificity, so a padding that
 *  itself carried a variant could sort after the override and silently keep the
 *  target below the touch floor. Positioning and the reveal live on the row. */
const MERMAID_ACTION_BTN_CLS =
  'p-1.5 rounded-md bg-bg-elevated/90 border border-border text-muted hover:text-text cursor-pointer'

/** The longest a Mermaid draw waits on `document.fonts.ready` before it draws
 *  in the fallback face anyway and leaves the loaded face to the one late
 *  redraw. Why a cap, and why this number: see `whenFontsReady` in MermaidBlock. */
export const MERMAID_FONTS_READY_CAP_MS = 2500

export const MermaidBlock = memo(function MermaidBlock({ code }: { code: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const ref = useRef<HTMLDivElement>(null)
  const id = useId().replace(/:/g, '_')
  const renderedRef = useRef('')
  // Rendered SVG markup, kept for the enlarge viewer. Empty until a successful
  // render and reset on failure, so the enlarge affordance only ever exists
  // for (and targets) the diagram currently on screen.
  const [{ svg, code: renderedCode }, setRendered] = useState({ svg: '', code: '' })
  const [enlarged, setEnlarged] = useState(false)
  const moreRef = useRef<HTMLButtonElement>(null)
  const enlargeAfterMenu = useRef(false)
  const [downloadFailed, setDownloadFailed] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const downloadDiagram = async (format: 'svg' | 'png') => {
    if (!svg || renderedCode !== code) return
    setDownloading(true)
    let snapshotHost: HTMLDivElement | undefined
    try {
      let basename = ref.current?.querySelector(':scope > svg > title')?.textContent?.normalize('NFKC')
        .replace(/[^\p{L}\p{N}_-]+/gu, '-').replace(/^-+|-+$/g, '').slice(0, 80)
      if (!basename || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/i.test(basename)) basename = 'mermaid-diagram'
      let blob: Blob
      if (format === 'svg') {
        blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' })
      } else {
        const node = ref.current
        if (!node) throw new Error('diagram not mounted')
        // Freeze pixels' inputs before any await; a rerender may replace the live SVG.
        const snapshot = node.cloneNode(true) as HTMLDivElement
        const originals = [node, ...node.querySelectorAll<HTMLElement | SVGElement>('*')]
        const copies = [snapshot, ...snapshot.querySelectorAll<HTMLElement | SVGElement>('*')]
        originals.forEach((element, index) => {
          const style = getComputedStyle(element)
          for (const property of Array.from(style)) copies[index].style.setProperty(property, style.getPropertyValue(property))
        })
        const backgroundColor = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
        snapshotHost = document.createElement('div')
        Object.assign(snapshotHost.style, { position: 'absolute', left: '-100000px', top: '0', pointerEvents: 'none' })
        snapshotHost.setAttribute('aria-hidden', 'true')
        // Isolate SVG IDs/styles from Mermaid's next render, while retaining layout.
        snapshotHost.attachShadow({ mode: 'closed' }).appendChild(snapshot)
        document.body.appendChild(snapshotHost)
        const { toBlob } = await import('html-to-image')
        const image = await toBlob(snapshot, {
          pixelRatio: 2,
          fontEmbedCSS: await mermaidFontCss(snapshot),
          backgroundColor,
        })
        if (!image) throw new Error('canvas encoder returned null')
        blob = image
      }
      downloadBlob(blob, `${basename}.${format}`)
      setDownloadFailed(false)
    } catch {
      setDownloadFailed(true)
    } finally {
      snapshotHost?.remove()
      setDownloading(false)
    }
  }
  // Which of the two views is on screen. The diagram host below stays MOUNTED
  // either way and is hidden with the `hidden` ATTRIBUTE rather than unmounted:
  // the render effect is guarded on `renderedRef.current === code`, so a
  // remounted host would be a fresh empty node the effect then declines to fill,
  // and toggling back would show a blank frame. Hiding keeps the already-rendered
  // SVG in the same node, so the switch back is instant and cannot strand an
  // empty host. The attribute rather than a `hidden` utility class because it
  // also takes the diagram out of the accessibility tree, which a class cannot.
  const [showSource, setShowSource] = useState(false)
  // Outcome of the last copy press. `failed` is a refused clipboard write --
  // `copyCode` RESOLVES false when the textarea fallback reports failure and
  // never rejects, so the boolean is the only failure signal; confirming
  // unconditionally would announce "Copied" for a write that never landed.
  //
  // The two outcomes are NOT symmetric, and that asymmetry is the design:
  //   - `ok` is a transient confirmation. It clears itself, because a
  //     confirmation the user has already read is noise.
  //   - `failed` is an ERROR and persists until it is dismissed or until a later
  //     press succeeds. A failure that erased itself after a second and a half
  //     could not be read, let alone acted on -- and it is the outcome the user
  //     most needs, since the text they asked for is NOT on their clipboard.
  //
  // It surfaces through `ErrorNotice` rather than through the button's own icon
  // and label: the value originates in an operation that failed, which is what
  // `errors-use-error-notice` covers -- the rule decides by where the value
  // comes from, not by how it is rendered, so a refusal reported only as a red
  // glyph is the same finding in a smaller font. The notice is the SINGLE error
  // surface for it; the button deliberately keeps its neutral icon while it
  // shows, rather than restating the failure a second time beside it.
  type CopyOutcome = 'idle' | 'ok' | 'failed'
  const [copyState, setCopyState] = useState<CopyOutcome>('idle')
  const copySource = () => {
    copyCode(code).then(ok => {
      setCopyState(ok ? 'ok' : 'failed')
      // Only the confirmation is on a timer. See above.
      if (ok) setTimeout(() => setCopyState('idle'), 1500)
    })
  }
  const copyLabel = copyState === 'ok' ? i18nT('components.markdownRenderer.copied')
    : i18nT('components.markdownRenderer.copy_diagram_source')
  // A render that threw: the raw source stays visible below (it is the only
  // evidence of what failed), and this drives the notice above it.
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const host = ref.current
    if (!host || renderedRef.current === code) return
    renderedRef.current = code
    setFailed(false)
    // Draw only once this element HAS A BOX. mermaid sizes every label by
    // getBoundingClientRect() on a scratch <div> it appends to document.body,
    // so what it needs is a laid-out DOCUMENT, and the one place the two go
    // dark together is the case that bit: a remote-instance pane is a
    // display:none <iframe> while another instance tab is active
    // (InstancesViewport hides, never unmounts), and inside it every rect is
    // 0. A diagram that finishes streaming there comes back as a 16px viewBox
    // with NaN node transforms -- an empty box where the flowchart should be,
    // and nothing redraws it until the block happens to remount.
    //
    // `getClientRects()` is the probe because it is EMPTY when the element has
    // no box at all (display:none anywhere above it, the hidden iframe
    // included) and non-empty, at zero size, whenever layout did run. The
    // obvious signals cannot tell: inside a hidden iframe
    // document.visibilityState stays 'visible' and clientWidth reports the
    // last laid-out value. A ResizeObserver stays silent while the box is
    // absent and fires on the frame it reappears, after layout, which is
    // exactly when mermaid's measurements are trustworthy again. The probe runs
    // before the lazy mermaid load and before mermaid.render(), and the box is
    // watched for the whole of render(), so every async gap around the
    // measurement is covered.
    let live = true
    let settled = false
    let observer: ResizeObserver | undefined
    let watch: ResizeObserver | undefined
    const whenBoxed = () => new Promise<void>(resolve => {
      if (host.getClientRects().length > 0 || typeof ResizeObserver !== 'function') {
        resolve()
        return
      }
      observer = new ResizeObserver(() => {
        if (host.getClientRects().length === 0) return
        observer?.disconnect()
        observer = undefined
        resolve()
      })
      observer.observe(host)
    })
    // One attempt: wait for a box, then render WHILE WATCHING THE BOX. render()
    // is itself async -- it lazy-loads the diagram's own chunk, and image shapes
    // load apart -- so the pane can go hidden after the probe and even come back
    // before render() resolves, with some or all labels measured at 0 in
    // between. A point check at the end would pass on those. The observer
    // reports the box going to 0x0 on the first frame it is gone, so any hide
    // that lasts a frame is caught even when the box is back by the end. A hide
    // that starts AND ends inside one frame (under ~16ms) is not reported; no
    // tab switch is that fast, and waiting a frame to find out would tax every
    // diagram for it. Lost box, or no box at the end: discard that SVG and go
    // round again. Without ResizeObserver there is nothing to wait on, so the
    // result stands as it did before this change rather than rendering in a
    // loop.
    //
    // The box is one precondition of a trustworthy measurement; the FONTS are
    // the other. The body face is swap-loaded (`display=swap` on the Google
    // Fonts stylesheet in index.html), so text already painted -- the SVG's
    // <foreignObject> labels included -- is repainted in the loaded face when it
    // arrives, while the node and edge-label boxes keep the widths mermaid
    // measured in the fallback face: every long label clipped at its right edge
    // (#12480), deterministically for any diagram drawn while that load is in
    // flight, which is every diagram in the transcript on a cold load. So the
    // measurement also waits for `document.fonts.ready`, the same gate
    // CliPanel and useFontOptions use before they measure text. `ready` settles
    // when no load is PENDING, which leaves two ways for a face to land after
    // the measurement, and the set is watched for both: a face whose
    // unicode-range is first exercised by the diagram's own glyphs starts
    // loading only once mermaid lays the label out, inside render(), so a
    // `loadingdone` in flight or a load still pending at the end discards that
    // SVG; and when the swap-loaded STYLESHEET is itself the late arrival (the
    // dashboard is served from a local gateway, the font origin is the one slow
    // resource), no face exists to be pending when `ready` is read, the diagram
    // is drawn in the fallback face and the swap repaints it when the stylesheet
    // lands -- so the watch outlives the draw, and the first `loadingdone` after
    // it redraws the diagram. Either way ONE more attempt, which itself waits
    // for the pending load, and the watch is spent. One, not a loop: a face
    // that keeps loading, or a set that never settles, would otherwise redraw
    // the diagram forever, and the second measurement already saw every glyph
    // the first one exercised. Where `document.fonts` is absent (the test DOM,
    // older engines) there is nothing to wait for and the gate is a no-op.
    //
    // The wait on `ready` is CAPPED at MERMAID_FONTS_READY_CAP_MS. `ready`
    // settles only once no load is pending, and a font file whose packets are
    // dropped -- not refused; a refusal fails fast and settles it -- keeps its
    // FontFace pending for the browser's network timeout, tens of seconds or
    // more. Uncapped, the gate would show NOTHING for every diagram on that cold
    // load for the whole window, where drawing at once showed clipped but
    // readable labels. Past the cap the diagram is drawn in the fallback face
    // and the late-arrival path below takes over: the watch outlives the draw,
    // so when the face does land its `loadingdone` redraws the diagram once in
    // the loaded face -- the same final frame the stylesheet-late case reaches
    // -- so the cap costs one fallback-face frame and no correctness. A load
    // still pending when a CAPPED render ends is the very load the gate gave up
    // on, so it does not count as a face that moved: one more attempt would
    // only wait out the cap again and spend the single redraw that the late
    // `loadingdone` needs. 2.5 s because in the harness's cold loads the face
    // is requested at about +0.35 s and the transcript renders at about
    // +1.3-1.6 s, so a face still pending when the gate is read is already a
    // second into its fetch and 2.5 s more covers a live fetch several times
    // over; it stays under the 3 s block period the CSS Fonts spec grants
    // `font-display: block` before fallback text shows, and under the harness's
    // 4 s hold, so its font-files pass exercises this path.
    const fonts = document.fonts as FontFaceSet | undefined
    let capTimer: ReturnType<typeof setTimeout> | undefined
    /** Resolves when `ready` settles or the cap elapses, whichever is first;
     *  `true` means the cap won and the draw to come measures in the fallback face. */
    const whenFontsReady = (): Promise<boolean> => {
      if (!fonts) return Promise.resolve(false)
      return new Promise<boolean>(resolve => {
        let done = false
        const finish = (capped: boolean) => {
          if (done) return
          done = true
          clearTimeout(capTimer)
          capTimer = undefined
          resolve(capped)
        }
        capTimer = setTimeout(() => finish(true), MERMAID_FONTS_READY_CAP_MS)
        void fonts.ready.then(() => finish(false))
      })
    }
    let fontsRetried = false
    let rendering = false
    let fontLanded = false
    let onFontLoaded: (() => void) | undefined
    const releaseFontWatch = () => {
      if (onFontLoaded) fonts?.removeEventListener('loadingdone', onFontLoaded)
      onFontLoaded = undefined
    }
    const attempt = (mermaid: MermaidApi): Promise<{ svg: string } | null> =>
      whenBoxed()
        .then(whenFontsReady)
        .then(capped => {
          if (!live) return null
          let lostBox = false
          if (typeof ResizeObserver === 'function') {
            watch = new ResizeObserver(() => {
              if (host.getClientRects().length === 0) lostBox = true
            })
            watch.observe(host)
          }
          fontLanded = false
          rendering = true
          // Re-initialized per render so a theme switch between two diagrams is
          // picked up; initialize() is cheap and idempotent.
          initMermaid(mermaid)
          return mermaid.render(`mermaid-${id}`, code)
            .then(result => ({ result, lostBox, fontLanded, capped }))
            .finally(() => {
              rendering = false
              watch?.disconnect()
              watch = undefined
            })
        })
        .then(step => {
          if (!step || !live) return null
          const boxless = step.lostBox || host.getClientRects().length === 0
          if (boxless && typeof ResizeObserver === 'function') return attempt(mermaid)
          const fontMoved = step.fontLanded || (!step.capped && fonts?.status === 'loading')
          if (fontMoved && !fontsRetried) {
            fontsRetried = true
            releaseFontWatch()
            return attempt(mermaid)
          }
          return step.result
        })
    const install = (result: { svg: string } | null) => {
      if (!result || !live || !ref.current) return
      settled = true
      const range = document.createRange()
      range.selectNodeContents(ref.current)
      range.deleteContents()
      ref.current.appendChild(range.createContextualFragment(result.svg))
      setRendered({ svg: result.svg, code })
    }
    const fail = () => {
      if (!live || !ref.current) return
      settled = true
      // The host is EMPTIED rather than filled with a hand-built <pre>. The
      // source is rendered declaratively below for both states that show it
      // (`failed || showSource`), so there is exactly one element -- and one set
      // of styles -- meaning "this diagram's source as text". Building a second
      // one here left two spellings of the same thing, kept in sync by hand,
      // which diverges the first time either is retouched.
      ref.current.textContent = ''
      setRendered({ svg: '', code: '' })
      setEnlarged(false)
      // Reset so the failed state has ONE shape. Not to prevent stranding: the
      // source below now lives OUTSIDE the hidden host, so neither value of
      // `showSource` can strand the reader. It is that a later successful render
      // should show the diagram it just produced rather than silently staying on
      // text, and while no diagram exists neither does the toggle that would
      // bring the reader back.
      setShowSource(false)
      setFailed(true)
    }
    const draw = () => whenBoxed().then(loadMermaid).then(attempt).then(install).catch(fail)
    if (fonts) {
      onFontLoaded = () => {
        // Mid-render: read at the end of that render. After the draw: the late
        // stylesheet case above, or a face the capped gate stopped waiting for
        // -- redraw once, and the watch is spent. While still waiting for a box
        // or for `ready`, the render to come measures in the landed face
        // already, so there is nothing to do.
        if (rendering) {
          fontLanded = true
          return
        }
        if (!settled || fontsRetried || !live) return
        fontsRetried = true
        releaseFontWatch()
        void draw()
      }
      fonts.addEventListener('loadingdone', onFontLoaded)
    }
    void draw()
    return () => {
      // Torn down before anything was drawn: abandon this chain and forget the
      // code too, or the guard above would make the next run (a new code
      // string, or StrictMode's dev-only replay of this effect) skip a diagram
      // that never rendered. Once the SVG or the failure notice is on screen
      // there is nothing to abandon, and the guard keeps doing its job -- but
      // the font watch, and a redraw it may have started, still end here.
      live = false
      observer?.disconnect()
      observer = undefined
      watch?.disconnect()
      watch = undefined
      clearTimeout(capTimer)
      capTimer = undefined
      releaseFontWatch()
      if (!settled) renderedRef.current = ''
    }
  }, [code, id])

  return (
    <div className="relative group my-3">
      {/* No hand-off: this renderer is embedded in hosts that hold unsaved
          drafts and cannot tell which — the file panel's editor buffer and the
          composer's markdown preview among them — so the navigation could
          discard what the user typed. */}
      {failed && (
        <ErrorNotice
          variant="inline"
          className="mb-2"
          message={i18nT('components.markdownRenderer.mermaid_render_failed')}
          testId="mermaid-render-error"
        />
      )}
      {/* Pointer convenience: clicking the rendered diagram opens the viewer.
          Keyboard and AT users reach the same viewer through the real button
          below — the same pairing the image lightbox uses (clickable <img>,
          focusable controls elsewhere). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/click-events-have-key-events */}
      <figure
        hidden={showSource || failed}
        className={`m-0 ${svg ? 'cursor-zoom-in' : ''}`}
        onClick={svg ? () => setEnlarged(true) : undefined}
      >
        <div ref={ref} className="flex justify-center overflow-x-auto min-h-[60px]" />
      </figure>
      {(showSource || failed) && (
        // THE one place this component paints a diagram's source as text, for
        // both states that show it: the toggle, and a render that threw (where
        // the source is the only evidence of what failed, sitting under the
        // notice that reports it). One element rather than two hand-synced ones,
        // so toggling to the source after a failure cannot restyle it and the
        // two cannot drift. Styles are explicit rather than leaning on
        // `.msg-content pre`: this renderer is also mounted in hosts that are
        // not a message body.
        <pre data-testid="mermaid-source" className="text-[13px] font-mono overflow-x-auto text-muted">{code}</pre>
      )}
      {/* One action row rather than three absolutely-positioned buttons:
          `touchActions` documents that the ROW shape is what carries the touch
          overrides for a cluster (it grows the descendants and wraps), while the
          single-button shape this replaces can only override the element it sits
          on. The row stays visible while the source view is on, so the control
          that left the default state is still reachable without hovering.

          AT MOST TWO BUTTONS IN EVERY REACHABLE STATE, by construction rather
          than by counting: the diagram view is toggle + actions, the source view
          is toggle + copy (enlarge would open a viewer for the view just left),
          and a failed render is copy alone, there being no rendered diagram to
          toggle to. Copy rides with the SOURCE for a second reason: on the
          rendered diagram the object of "copy" is ambiguous -- the picture or the
          text behind it -- and beside the source text it is not. */}
      <div className={`absolute top-1.5 right-1.5 flex items-center gap-1 transition-opacity ${showSource || downloading ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'} ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
        {svg && (
          <button
            data-testid="mermaid-source-toggle"
            aria-pressed={showSource}
            aria-label={i18nT('components.markdownRenderer.diagram_source')}
            title={i18nT('components.markdownRenderer.diagram_source')}
            className={MERMAID_ACTION_BTN_CLS}
            onClick={() => setShowSource(v => !v)}
          >
            {/* `FileCode`, not `Code`: the message footer's own raw-markdown
                toggle sits a row below and already uses `Code`, and a first-time
                reader could not tell the two glyphs apart. */}
            <FileCode className="lucide-inline" aria-hidden="true" />
          </button>
        )}
        {/* Copy remains source-only; rendered-image downloads live in the menu. */}
        {(showSource || failed) && (
          <button
            data-testid="mermaid-copy-source"
            aria-label={copyLabel}
            title={copyLabel}
            className={MERMAID_ACTION_BTN_CLS}
            onClick={copySource}
          >
            {copyState === 'ok' ? <Check className="lucide-inline text-ok" aria-hidden="true" />
              : <Copy className="lucide-inline" aria-hidden="true" />}
          </button>
        )}
        {svg && !showSource && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                ref={moreRef}
                aria-busy={downloading}
                aria-disabled={downloading}
                onPointerDown={event => { if (downloading) event.preventDefault() }}
                onKeyDown={event => {
                  if (downloading && ['Enter', ' ', 'ArrowDown'].includes(event.key)) event.preventDefault()
                }}
                data-testid="mermaid-more-actions"
                aria-label={i18nT('components.markdownRenderer.diagram_actions')}
                title={i18nT('components.markdownRenderer.diagram_actions')}
                className={MERMAID_ACTION_BTN_CLS}
              >
                {downloading
                  ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  : <MoreHorizontal className="lucide-inline" aria-hidden="true" />}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" onCloseAutoFocus={event => {
              if (!enlargeAfterMenu.current) return
              event.preventDefault()
              enlargeAfterMenu.current = false
              // Seat focus on the lasting trigger before the viewer captures it.
              // Opening during onSelect would let the menu steal focus back.
              moreRef.current?.focus({ preventScroll: true })
              setEnlarged(true)
            }}>
              <DropdownMenuItem data-testid="mermaid-enlarge" onSelect={() => { enlargeAfterMenu.current = true }}>
                <Maximize2 className="lucide-inline" aria-hidden="true" />
                {i18nT('components.diagramLightbox.enlarge_diagram')}
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="mermaid-download-svg" disabled={downloading || renderedCode !== code} onSelect={() => { void downloadDiagram('svg') }}>
                <Download className="lucide-inline" aria-hidden="true" />
                {i18nT('components.markdownRenderer.download_svg')}
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="mermaid-download-png" disabled={downloading || renderedCode !== code} onSelect={() => { void downloadDiagram('png') }}>
                <Download className="lucide-inline" aria-hidden="true" />
                {i18nT('components.markdownRenderer.download_png')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {/* A SEPARATED REGION below the action row, deliberately NOT a third
          control inside it. `max-two-buttons-per-row` counts action controls
          that are siblings in one horizontal group and exempts "controls in a
          genuinely different row or a separated region", so this notice -- and
          the dismiss affordance it brings with it -- cannot push the row past
          two. The row's cap therefore still holds in this state as well: toggle
          + copy, with the failure reported beneath them rather than among them.

          No hand-off, for exactly the reason given at the render notice above --
          this renderer is embedded in hosts holding unsaved drafts it cannot
          identify, so navigating away could discard what the user typed. */}
      {/* No hand-off: the containing file editor or composer may hold unsaved drafts. */}
      {downloadFailed && (
        <ErrorNotice
          variant="inline"
          className="mt-2"
          message={i18nT('components.markdownRenderer.download_failed')}
          onDismiss={() => setDownloadFailed(false)}
          testId="mermaid-download-error"
        />
      )}
      {copyState === 'failed' && (
        <ErrorNotice
          variant="inline"
          className="mt-2"
          message={i18nT('components.markdownRenderer.copy_failed')}
          onDismiss={() => setCopyState('idle')}
          testId="mermaid-copy-error"
        />
      )}
      {enlarged && svg && <DiagramLightbox svg={svg} onClose={() => setEnlarged(false)} />}
    </div>
  )
})
