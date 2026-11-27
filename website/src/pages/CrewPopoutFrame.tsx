import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import { PANE_FRAME_ALLOW } from '../components/InstancesViewport'
import { useTranslation } from 'react-i18next'
import { CREW_POPOUT_VERSION, framedCrewPopoutSrc } from '../lib/crewPopout'
import { HOST_CLOSE_POPOUT } from '../lib/hostRelay'
import { paneCredentialFor } from '../lib/paneCredential'

type Frame = { src: string; origin: string; name: string }
type Refusal = 'not_connected' | 'cannot_open'
// A rejected instances lookup, connect or credential mint.
type Failure = { error: string }

/**
 * `path` on the connected Remote Crew whose tunnel is on `port`, with the
 * credential the viewport's warm path gets: it never brings a tunnel up.
 */
async function resolveFrame(port: number, path: string): Promise<Frame | Refusal> {
  const crewOrigin = (p: number) => `http://${window.location.hostname}:${p}`
  if (framedCrewPopoutSrc(crewOrigin(port), path, '') === null) return 'cannot_open'
  const { instances } = await api.listInstances()
  const crew = instances.find(i => i.status.state === 'connected' && i.status.local_port === port)
  if (!crew) return 'not_connected'
  const st = await api.connectInstance(crew.id, { onlyIfConnected: true })
  if (st.state !== 'connected' || !st.local_port || !st.token) return 'not_connected'
  const conn = await paneCredentialFor(crew.id, { port: st.local_port, token: st.token })
  const origin = crewOrigin(conn.port)
  const src = framedCrewPopoutSrc(origin, path, conn.token)
  return src === null ? 'cannot_open' : { src, origin, name: crew.name }
}

/**
 * The hub's window for a Remote Crew pane's pop-out in the desktop app
 * (`/popout/crew?port=…&path=…`, opened by `lib/crewPopout.ts`): the crew's own
 * pop-out page, framed full-bleed. It answers that page's embed handshake with a
 * host model naming the desktop app, so the page's own pop-outs come back here,
 * and closes this window when the page asks to.
 */
export default function CrewPopoutFrame() {
  const { t } = useTranslation()
  const [params] = useSearchParams()
  const port = Number(params.get('port'))
  const path = params.get('path') ?? ''
  const [frame, setFrame] = useState<Frame | Refusal | Failure | null>(null)
  const ref = useRef<HTMLIFrameElement>(null)

  useEffect(() => {
    let live = true
    resolveFrame(port, path).then(
      f => { if (live) setFrame(f) },
      (e: unknown) => { if (live) setFrame({ error: e instanceof Error ? e.message : String(e) }) },
    )
    return () => { live = false }
  }, [port, path])

  useEffect(() => {
    if (frame === null || typeof frame === 'string' || 'error' in frame) return
    document.title = frame.name
    const onMessage = (e: MessageEvent) => {
      const target = ref.current?.contentWindow
      if (!target || e.source !== target || e.origin !== frame.origin) return
      const type = (e.data as { type?: unknown } | null)?.type
      if (type === 'mc-embedded-ready') {
        target.postMessage({ type: 'mc-embedded-ack', v: 1 }, frame.origin)
        // Only a desktop-app pane opens this page (utils/popoutController), and
        // the desktop shell gives this child window no preload to say so.
        // `crewPopout`: this hub frames that page's own pop-outs too.
        target.postMessage(
          { type: 'mc-host-model', v: 1, tabs: [], electron: true, crewPopout: CREW_POPOUT_VERSION },
          frame.origin,
        )
      } else if (type === HOST_CLOSE_POPOUT) {
        window.close()
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [frame])

  if (frame === null) return <div className="h-screen w-screen bg-bg" />
  if (typeof frame === 'string') {
    return (
      <div className="h-screen w-screen bg-bg flex items-center justify-center p-6 text-sm text-muted" role="status">
        {frame === 'not_connected'
          ? t('pages.crewPopoutFrame.this_crew_is_not_connected')
          : t('pages.crewPopoutFrame.this_pop_out_cannot_be_opened')}
      </div>
    )
  }
  if ('error' in frame) {
    return (
      <div className="h-screen w-screen bg-bg p-6">
        <ErrorNotice message={frame.error} askAgent />
      </div>
    )
  }
  return (
    <iframe
      ref={ref}
      title={frame.name}
      src={frame.src}
      allow={PANE_FRAME_ALLOW}
      allowFullScreen
      className="block h-screen w-screen border-0 bg-bg"
    />
  )
}
