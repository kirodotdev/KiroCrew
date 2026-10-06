/**
 * Isolated capture entry for #11732: the image and diagram viewers' top-right
 * controls on a frameless desktop window.
 *
 * WHY ISOLATED: the defect is geometry against the OS caption buttons, which
 * only a real layout engine shows, and the live dashboard is token-gated with a
 * long path to an open image. The faithful part is the COMPONENTS: the real
 * `Lightbox` (opened through the production `lightbox` event) and the real
 * `DiagramLightbox` are mounted, with `window.kirocrew` set to what the desktop
 * preload exposes BEFORE they are imported, so `src/lib/electron.ts` reads the
 * same platform flags it reads in the shell.
 *
 * What is NOT real: the caption buttons themselves. Windows paints them outside
 * the page and frameless Linux injects them from the main process, so a
 * stand-in of the same geometry (138x42 / 108x42, top-right, above everything)
 * is drawn here, outlined, so a still shows whether the controls clear it.
 *
 * Query: ?shell=win|linux|browser  &viewer=image|diagram  &clipboard=ok
 */
import { useEffect, type ComponentType } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const shell = params.get('shell') || 'win'
const viewer = params.get('viewer') || 'image'

if (shell === 'win') window.kirocrew = { isElectron: true, platform: 'win32' } as typeof window.kirocrew
if (shell === 'linux') window.kirocrew = { isElectron: true, platform: 'linux', linuxFrameless: true } as typeof window.kirocrew
document.documentElement.setAttribute('data-theme', 'kiro-light')

// Headless Chromium refuses an image write to the clipboard, so the copy action
// would always show its failure notice. A resolved write lets a capture show the
// "copied" pill, which is the element whose position this entry is about.
if (params.get('clipboard') === 'ok' && navigator.clipboard) {
  navigator.clipboard.write = async () => {}
}

const CAPTION_W = shell === 'win' ? 138 : shell === 'linux' ? 108 : 0

const SUBJECT = `data:image/svg+xml;utf8,${encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800">
  <rect width="1200" height="800" fill="#0e1116"/>
  <rect x="60" y="60" width="1080" height="160" rx="18" fill="#1b212b"/>
  <rect x="60" y="260" width="520" height="480" rx="18" fill="#151a22"/>
  <rect x="620" y="260" width="520" height="480" rx="18" fill="#151a22"/>
  <circle cx="320" cy="500" r="120" fill="#7c5cff"/>
  <rect x="680" y="320" width="400" height="24" rx="12" fill="#3d4756"/>
  <rect x="680" y="370" width="320" height="24" rx="12" fill="#2b333f"/>
</svg>`)}`

const DIAGRAM = (() => {
  const NS = 'http://www.w3.org/2000/svg'
  const el = document.createElementNS(NS, 'svg')
  el.setAttribute('viewBox', '0 0 400 200')
  el.setAttribute('width', '400')
  el.setAttribute('height', '200')
  for (const [x, label] of [[20, 'Request'], [150, 'Gateway'], [280, 'Agent']] as const) {
    const r = document.createElementNS(NS, 'rect')
    for (const [k, v] of Object.entries({ x: String(x), y: '70', width: '100', height: '60', rx: '8', fill: '#ede9fe', stroke: '#7c5cff' })) r.setAttribute(k, v)
    const t = document.createElementNS(NS, 'text')
    for (const [k, v] of Object.entries({ x: String(x + 50), y: '105', 'text-anchor': 'middle', 'font-size': '14' })) t.setAttribute(k, v)
    t.textContent = label
    el.append(r, t)
  }
  return el.outerHTML
})()

/** Stand-in for the OS caption buttons, drawn above the viewers. */
function CaptionStandIn() {
  if (!CAPTION_W) return null
  return (
    <div
      data-testid="caption-stand-in"
      style={{
        position: 'fixed', top: 0, right: 0, width: CAPTION_W, height: 42, zIndex: 100001,
        display: 'flex', alignItems: 'stretch', color: '#fff',
        background: 'rgba(220, 38, 38, 0.55)', outline: '2px solid #dc2626',
      }}
    >
      {['–', '☐', '✕'].map(g => (
        <span key={g} style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14 }}>{g}</span>
      ))}
    </div>
  )
}

async function main() {
  const { Lightbox } = await import('../src/components/MarkdownRenderer')
  const { default: DiagramLightbox } = await import('../src/components/DiagramLightbox')
  const Diagram = DiagramLightbox as ComponentType<{ svg: string; onClose: () => void }>

  function Scene() {
    useEffect(() => {
      if (viewer !== 'image') return
      window.dispatchEvent(new CustomEvent('lightbox', {
        detail: { images: [{ src: SUBJECT, alt: 'A dashboard screenshot' }], index: 0 },
      }))
    }, [])
    return (
      <div className="bg-bg text-text min-h-screen">
        <div className="h-[42px] bg-chrome border-b border-border flex items-center px-4 text-sm">Kiro Crew</div>
        <div className="p-4 space-y-3">
          <div className="h-4 w-2/3 rounded bg-chrome" />
          <div className="h-40 w-full rounded-lg bg-chrome" />
        </div>
        {viewer === 'image' ? <Lightbox /> : <Diagram svg={DIAGRAM} onClose={() => {}} />}
        <CaptionStandIn />
      </div>
    )
  }

  initI18n('en')
  createRoot(document.getElementById('root')!).render(<Scene />)
}

void main()
