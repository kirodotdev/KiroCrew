/**
 * `/xr`: the crew's world around you, for XR glasses, with a desktop preview.
 *
 * The page reads the same data as the rest of the dashboard and hands it to a
 * scene that React Three Fiber draws (`XrStage.tsx`). It is read-only in a default build (see `flags.ts`). On a
 * browser with no headset it runs as a preview: the mouse stands in for the
 * finger ray and the pinch, the wheel for the two-hand pull, and a button for
 * turning the left palm to the face.
 */
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Vector3, type PerspectiveCamera } from 'three'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Archive, ArrowLeft, BellRing, ExternalLink, Hand, Keyboard, LayoutDashboard, ListChecks, Map as MapIcon, MessageSquare, Minus,
  MoveDiagonal2, PanelsTopLeft, RectangleGoggles, SendHorizontal, TrendingDown, TrendingUp, X,
} from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { streamingSupported, useStreamingStt } from '../../hooks/useStreamingStt'
import { i18nT } from '../../i18n/t'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { FIXTURE_VOICE_LINE, FIXTURE_VOICE_REPLY } from './fixture'
import { TUNE } from './tune'
import { XR_WRITES_ENABLED } from './flags'
import { parsePlots } from './land'
import { parsePages } from './pages'
import { XR_GL, css, readPalette } from './palette'
import { XrScene, type XrWant } from './scene'
import { XrStage, makeXrStore } from './XrStage'
import { xrView } from './xrView'
import type { ScenePatch } from './sceneDoc'
import { loadImage } from './sceneParts'
import { xrStrings } from './strings'
import { useXrCrew, useXrDetail, xrNow } from './useXrData'
import { useXrSupport } from './xrSupport'

/** The icons the scene paints. They are the dashboard's own icon set, drawn to a canvas. */
const ICONS = {
  next: BellRing, store: Archive, map: MapIcon, full: PanelsTopLeft, close: X,
  resize: MoveDiagonal2, keyboard: Keyboard, send: SendHorizontal, up: TrendingUp, down: TrendingDown, flat: Minus,
  open: ExternalLink, chat: MessageSquare, dash: LayoutDashboard, work: ListChecks,
} as const

/** Where each Bot's plot is, and the pages the person laid out, kept in this browser from one visit to the next. */
const PLOTS_KEY = 'kirocrew:xr-plots'
const PAGES_KEY = 'kirocrew:xr-pages'

async function loadIcons(host: HTMLElement): Promise<Map<string, CanvasImageSource>> {
  const out = new Map<string, CanvasImageSource>()
  const serializer = new XMLSerializer()
  for (const svg of host.querySelectorAll<SVGElement>('svg[data-xr-icon]')) {
    const uri = `data:image/svg+xml;utf8,${encodeURIComponent(serializer.serializeToString(svg))}`
    const img = await loadImage(uri).catch(() => null)
    if (img && svg.dataset.xrIcon) out.set(svg.dataset.xrIcon, img)
  }
  return out
}

const NO_WANT: XrWant = { threads: [], pages: [], bots: [] }
const same = (a: readonly (string | number)[], b: readonly (string | number)[]) => a.length === b.length && a.every((x, i) => x === b[i])

/** The handle the tests use to change the scene's document while the page runs. Present with sample data or `?probe=1` only. */
type TestHook = { patch: (p: ScenePatch) => unknown; doc: () => unknown; key: (k: string) => boolean; say: (text: string) => void; face: (slug: string, deg: number) => void; walk: (slug: string, dx: number, dz: number) => void; hop: (slug: string) => void }

/** The workspace of pages is its own part of the build: a person on the land never loads it. */
const Workspace = lazy(() => import('./workspace/Workspace'))
/** One Bot's 3D dashboard alone (`?view=dash`), its own part of the build too. */
const DashView = lazy(() => import('./workspace/DashView'))

/** `/xr` has two views (`xrView`): the workspace of pages, which opens by default, and the land with `?view=land`. */
export default function XrPage() {
  const [params] = useSearchParams()
  const view = xrView(params)
  if (view === 'land') return <LandPage />
  return <Suspense fallback={null}>{view === 'dash' ? <DashView /> : <Workspace />}</Suspense>
}

function LandPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const fixture = params.get('fixture') === '1'
  // `scene` picks the scene document; `view=aura` frames the preview to what the glasses show.
  const sceneId = params.get('scene') ?? ''
  const view = params.get('view') ?? ''
  const look = Number(params.get('look') ?? '') || 4
  const probe = params.get('probe') === '1'
  const sound = params.get('sound') === '1'
  // Which of the two rules measures two hands on one thing: for trying both on a device.
  const twoHand = params.get('twohand') === 'double' ? 'double' : TUNE.twoHandRule
  // The person uses hands only. `voice=1` brings speech back: pointing at a Bot makes it listen, and a spoken yes counts.
  const voice = params.get('voice') === '1'
  const queryClient = useQueryClient()
  const support = useXrSupport()

  const hostRef = useRef<HTMLDivElement | null>(null)
  const iconsRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<XrScene | null>(null)
  // The framework's handle on the immersive session: one for as long as the page is open.
  const store = useMemo(() => makeXrStore(), [])
  useEffect(() => () => { void store.getState().session?.end().catch(() => undefined) }, [store])
  const [icons, setIcons] = useState<ReadonlyMap<string, CanvasImageSource> | null>(null)
  const [ready, setReady] = useState(0)
  const [want, setWant] = useState<XrWant>(NO_WANT)
  const [said, setSaid] = useState('')
  const [hand, setHand] = useState(false)
  const [late, setLate] = useState(false)
  const [reply, setReply] = useState(0)
  const [failure, setFailure] = useState('')

  const data = useXrCrew(fixture, late, reply)
  const detail = useXrDetail(fixture, want, data.crew)
  const names = useRef(new Map<string, string>())
  useEffect(() => { names.current = new Map(data.crew.bots.map(b => [b.slug, b.name])) }, [data.crew.bots])

  // The dashboard's own speech recogniser. The scene starts it when a Bot listens; the words come back as a caption.
  const stt = useStreamingStt({
    onPartial: text => sceneRef.current?.heard(text),
    onFinal: () => undefined,
    onEndpoint: () => sceneRef.current?.heardEnd(),
    onError: msg => setFailure(msg),
  })
  const sttRef = useRef(stt)
  sttRef.current = stt

  const leave = useCallback((to: string) => {
    if (to.startsWith('dash:')) {
      const name = names.current.get(to.slice(5))
      navigate(name ? `/members?member=${encodeURIComponent(name)}` : '/members')
    } else navigate(to === 'bots' ? '/members' : to === 'notes' ? '/notifications' : '/chat')
  }, [navigate])

  // After a write went through, read again what it changed. The websocket would also say so, a moment later.
  const refresh = useCallback(() => {
    for (const queryKey of [['global-approvals'], ['command-center', 'questions'], ['kirocrew-agents', 'members-roster'], ['xr', 'thread']]) {
      void queryClient.invalidateQueries({ queryKey })
    }
  }, [queryClient])

  useEffect(() => {
    const iconHost = iconsRef.current
    if (!iconHost) return
    let gone = false
    void loadIcons(iconHost).then(drawn => { if (!gone) setIcons(drawn) })
    return () => { gone = true }
  }, [])

  // Makes the scene. The framework calls it when its canvas has a camera, and again whenever one of these changes.
  const make = useCallback((camera: PerspectiveCamera) => {
    const host = hostRef.current
    if (!host || !icons) throw new Error('the xr scene was asked for before its host was drawn')
    {
      const scene = new XrScene({
        host, camera, icons, fixture, probe, scene: sceneId, view, look,
        voiceLine: FIXTURE_VOICE_LINE,
        voiceReply: FIXTURE_VOICE_REPLY,
        twoHand,
        voice,
        strings: xrStrings(),
        palette: readPalette(host),
        reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        now: xrNow(fixture),
        // Sample data starts the same every time: it neither reads nor keeps a layout.
        plots: fixture ? {} : parsePlots(safeGetItem(PLOTS_KEY)),
        pages: fixture ? [] : parsePages(safeGetItem(PAGES_KEY)),
        speech: voice && !fixture && streamingSupported ? { start: () => void sttRef.current.start(), stop: () => sttRef.current.cancel() } : null,
        onWant: w => setWant(prev => (same(prev.threads, w.threads) && same(prev.pages, w.pages) && same(prev.bots, w.bots) ? prev : w)),
        onLeave: leave,
        onSay: setSaid,
        onHand: setHand,
        onWrote: refresh,
        onPlots: plots => { if (!fixture) safeSetItem(PLOTS_KEY, JSON.stringify(plots)) },
        onPages: pages => { if (!fixture) safeSetItem(PAGES_KEY, JSON.stringify(pages)) },
      })
      if (sound) scene.setSound(true)
      return scene
    }
  }, [icons, fixture, probe, sceneId, view, look, sound, twoHand, voice, leave, refresh])

  // The framework made a scene, or took one down.
  const onScene = useCallback((live: XrScene | null) => {
    const hooked = window as unknown as { __xrScene?: TestHook }
    sceneRef.current = live
    if (!live) { delete hooked.__xrScene; return }
    // The test hook: change the scene's document, read it, and press a key of the scene (an emulated session takes the canvas out of the page, so keys cannot reach it there).
    // On sample data it can also say a sentence, as the recogniser would: that is how a test answers a request in words.
    // It can turn a Bot to face another way when it attends to nothing: that is how a film shows a body from every side.
    // It can send a Bot so far from where it stands (in the land's own units) and make it hop: that is how a film shows the body in a glide, a stop and a hop.
    if (fixture || probe) hooked.__xrScene = { patch: p => live.patch(p), doc: () => live.doc, key: k => live.key(new KeyboardEvent('keydown', { key: k })), say: text => { if (fixture) live.heard(text) }, face: (slug, deg) => live.world.nodes.get(slug)?.body?.setRest((deg * Math.PI) / 180), walk: (slug, dx, dz) => { const body = live.world.nodes.get(slug)?.body; body?.walkTo(new Vector3(body.root.position.x + dx, 0, body.root.position.z + dz)) }, hop: slug => live.world.nodes.get(slug)?.body?.hop() }
    setReady(n => n + 1)
    hostRef.current?.querySelector<HTMLElement>('[data-testid="xr-canvas"]')?.focus({ preventScroll: true })
  }, [fixture, probe])

  useEffect(() => { sceneRef.current?.setCrew(data.crew, data.notices) }, [data.crew, data.notices, ready])
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene) return
    for (const [slug, g] of Object.entries(detail.glances)) scene.setGlance(slug, g)
    for (const [slot, t] of Object.entries(detail.threads)) scene.setThread(slot, t)
  }, [detail, ready])

  const enter = useCallback(async () => {
    if (!sceneRef.current || support === 'none') return
    try {
      // The framework asks for the session (`makeXrStore` says with what) and hands it to the renderer.
      await (support === 'ar' ? store.enterAR() : store.enterVR())
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e))
    }
  }, [store, support])

  const loadError = useMemo(() => (data.error instanceof Error ? data.error.message : data.error ? String(data.error) : ''), [data.error])
  const chip = 'inline-flex items-center gap-1 rounded-full border border-border bg-bg-elevated px-2 py-0.5 text-[12px] text-muted'

  return (
    // Works on a narrow screen: the header wraps and the scene widens its lens (see `XrScene.resize`).
    <div className="flex h-full min-h-0 w-full flex-col" data-testid="xr-page">
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-bg-elevated px-3 py-2">
        <Btn onClick={() => navigate('/chat')} aria-label={i18nT('xr.back')} title={i18nT('xr.back')}>
          <ArrowLeft size={15} />
        </Btn>
        <h1 className="m-0 text-[15px] font-semibold text-text">{i18nT('xr.title')}</h1>
        {!XR_WRITES_ENABLED && <span className={chip} data-testid="xr-read-only">{i18nT('xr.read_only')}</span>}
        {fixture && <span className={chip} data-testid="xr-sample">{i18nT('xr.sample_data')}</span>}
        {hand && <span className={chip} data-testid="xr-hand-note"><Hand className="lucide-inline" />{i18nT('xr.hand_experiment')}</span>}
        <span className="flex-1" />
        <Btn onClick={() => sceneRef.current?.setHand(!hand)} aria-pressed={hand} data-testid="xr-hand-toggle">
          <Hand className="lucide-inline" /> {i18nT('xr.left_hand')}
        </Btn>
        {support !== 'none' && (
          <Btn primary onClick={() => void enter()} data-testid="xr-enter">
            <RectangleGoggles className="lucide-inline" /> {i18nT('xr.enter')}
          </Btn>
        )}
      </div>
      {(loadError || failure) && (
        <ErrorNotice
          title={i18nT('xr.load_failed')}
          message={loadError || failure}
          onDismiss={failure ? () => setFailure('') : undefined}
          askAgent
          variant="inline"
        />
      )}
      <div ref={hostRef} className="relative min-h-0 flex-1 overflow-hidden" data-testid="xr-host">
        {icons && (
          <XrStage
            store={store}
            make={make}
            onScene={onScene}
            label={i18nT('xr.canvas_label')}
            onKeyDown={e => {
              // Sample data only: E makes a new ask arrive. R plays one exchange: first your question, then the Bot's answer.
              if (fixture && !e.ctrlKey && !e.metaKey && !sceneRef.current?.isTyping) {
                if (e.key.toLowerCase() === 'e') { setLate(true); return }
                if (e.key.toLowerCase() === 'r') { setReply(n => Math.min(2, n + 1)); return }
              }
              if (sceneRef.current?.key(e.nativeEvent)) e.preventDefault()
            }}
            onKeyUp={e => sceneRef.current?.keyUp(e.nativeEvent)}
          />
        )}
        {!data.loading && !loadError && data.crew.bots.length === 0 && (
          <p className={`${chip} absolute left-1/2 top-1/2 -translate-x-1/2`}>{i18nT('xr.no_bots')}</p>
        )}
        <p className="pointer-events-none absolute bottom-3 right-3 max-w-[30%] rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[12px] text-muted opacity-80">{i18nT('xr.help')} {i18nT('xr.help_more')}</p>
        {/* The scene is a picture. This list gives the same Bots to a screen reader and to Tab. */}
        <ul className="sr-only" aria-label={i18nT('xr.title')}>
          {data.crew.bots.map(b => (
            <li key={b.slug}>
              <Btn onClick={() => sceneRef.current?.open(b.slug)}>
                {b.label}: {b.needs.length ? i18nT('xr.needs_you', { count: b.needs.length }) : b.line}
              </Btn>
            </li>
          ))}
        </ul>
        <div className="sr-only" role="status" aria-live="polite">{said}</div>
        <div ref={iconsRef} hidden aria-hidden="true">
          {Object.entries(ICONS).map(([name, Icon]) => (
            <Icon key={name} data-xr-icon={name} size={48} color={css(XR_GL.ink)} />
          ))}
        </div>
      </div>
    </div>
  )
}
