/**
 * Isolated capture entry for the Mochi label-association a11y fixes.
 *
 * WHY THESE THREE: of the ~190 files in the eslint burndown, only a handful change
 * rendered markup rather than adding an attribute, and these are the ones where a
 * regression would be VISIBLE rather than merely announced. Each swaps a styled
 * `<div>`/`<span>`/`<img>` for a real form element so the control gets a name:
 *
 *   - NumberField      caption `<span>`  -> `<label htmlFor>`
 *   - PackInfoHeader   caption `<span>`s -> `<label htmlFor>`
 *   - PendingAttachments  clickable `<img>` -> `<button>` wrapping the `<img>`
 *
 * A `<label>` and a `<button>` are INLINE by default where a `<div>` is block and
 * an `<img>` carries no button chrome, so each swap had to re-state the layout it
 * replaced (`display: 'block'`, a reset `<button>` style). That is exactly the
 * class of change a screenshot can falsify and a test cannot, which is why these
 * frames exist. Capture the same URLs at HEAD~1 and diff: identical pixels mean
 * the swap is name-only.
 *
 * WHY ISOLATED: these live in Mochi's Electron renderer windows
 * (settings.html / avatar.html / panel.html), which load neither Tailwind nor the
 * theme tokens through the dashboard shell and need a live bridge to boot. The
 * components themselves are the real ones, imported unmodified; only the props are
 * fixtures. `theme` comes from the query string: ?theme=dark
 */
import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import { NumberField } from '../src/apps/mochi/src/renderer/NumberField'
import { PackInfoHeader } from '../src/apps/mochi/src/renderer/PackInfoHeader'
import { PendingAttachments } from '../src/apps/mochi/panel/PendingAttachments'
// The captions under test ARE catalog strings, so an uninitialised i18n would
// render them empty and the frame would document nothing.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** The three NumberFields sit in one row upstream (SpriteImporter), which is the
 *  case `useId` exists for — a shared id would cross the label wires. */
function Fields() {
  const [w, setW] = useState(64)
  const [h, setH] = useState(64)
  const [fps, setFps] = useState(12)
  return (
    <div style={{ display: 'flex', gap: 12 }}>
      <NumberField label="Frame width" value={w} min={1} max={512} onChange={setW} />
      <NumberField label="Frame height" value={h} min={1} max={512} onChange={setH} />
      <NumberField label="FPS" value={fps} min={1} max={60} onChange={setFps} />
    </div>
  )
}

function Header() {
  const [name, setName] = useState('Sunset Mochi')
  const [author, setAuthor] = useState('bolichen')
  const [description, setDescription] = useState('A warmer palette for the evening.')
  const [flipX, setFlipX] = useState(false)
  return (
    <PackInfoHeader
      title="Pack details"
      name={name}
      author={author}
      description={description}
      flipX={flipX}
      onNameChange={setName}
      onAuthorChange={setAuthor}
      onDescriptionChange={setDescription}
      onFlipXChange={setFlipX}
    />
  )
}

/** The chip's `<img src>` is `localFileUrl(path)` = `/api/file-raw?path=…`, which
 *  only Electron's own host answers. The capture script fulfils that route with a
 *  real bitmap, so the thumbnail resolves here rather than rendering the
 *  broken-image glyph — a frame of the fallback would document nothing about the
 *  `<img>` -> `<button><img>` box. `isImage` decides the branch under test. */
function Chips() {
  const [items, setItems] = useState([
    { path: '/tmp/shot-1.png', name: 'shot-1.png', isImage: true },
    { path: '/tmp/notes.md', name: 'notes.md', isImage: false },
  ])
  return (
    <PendingAttachments
      items={items}
      onRemove={p => setItems(prev => prev.filter(i => i.path !== p))}
    />
  )
}

const Section = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div style={{ marginBottom: 20 }}>
    <div style={{ fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 6 }}>
      {label}
    </div>
    {children}
  </div>
)

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      padding: 20, width: 620, background: 'var(--bg)', color: 'var(--text)',
      font: '13px system-ui, -apple-system, sans-serif',
    }}
  >
    <Section label="NumberField — caption bound to its input">
      <Fields />
    </Section>
    <Section label="PackInfoHeader — captions bound to their inputs">
      <Header />
    </Section>
    <Section label="PendingAttachments — thumbnail is a real button">
      <Chips />
    </Section>
  </div>,
)
