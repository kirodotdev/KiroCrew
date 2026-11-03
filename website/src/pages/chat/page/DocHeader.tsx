/**
 * A doc's page header (PoC), after Notion and Loop: an optional cover across the
 * top of the page and an optional emoji icon sitting on its lower edge. With
 * neither, the header is only space, and "Add icon" / "Add cover" appear when the
 * pointer is over it. Stored in the doc's front matter (`docMeta`). Labels are
 * English like the rest of the doc conductor layer.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ImagePlus, Shuffle, Smile } from 'lucide-react'
import { Btn } from '../../../components/ui'
import { COLORS, EMOJI, GRADIENTS, coverBackground, isImageUrl, randomGradient, type DocMeta } from './docMeta'

/** The page column the editor sits in, matched so the icon lines up with the text. */
const COLUMN = { margin: '0 auto', width: '100%', maxWidth: 'var(--mc-content-width)', boxSizing: 'border-box' as const, padding: '0 20px' }

const GHOST = 'border-transparent text-muted hover:text-text hover:border-transparent'

/** A small floating panel that closes on Escape or a click outside it. */
function Popover({ onClose, children, label, className = '' }: { onClose: () => void; children: ReactNode; label: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const down = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose() }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('mousedown', down)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('mousedown', down); document.removeEventListener('keydown', key) }
  }, [onClose])
  return (
    <div ref={ref} role="dialog" aria-label={label}
      className={`absolute z-30 rounded-xl border border-border bg-bg-elevated text-text shadow-[0_12px_32px_-6px_rgba(0,0,0,.25)] ${className}`}>
      {children}
    </div>
  )
}

function IconPicker({ onPick, onRemove, onClose }: { onPick: (e: string) => void; onRemove: () => void; onClose: () => void }) {
  return (
    <Popover onClose={onClose} label="Choose an icon" className="left-0 top-full mt-2 w-[328px] p-2">
      <div className="mb-1 flex items-center justify-between px-1">
        <span className="text-[12px] text-muted">Icon</span>
        <Btn className={GHOST} onClick={onRemove} data-testid="doc-icon-remove">Remove</Btn>
      </div>
      <div className="grid grid-cols-8 gap-0.5" data-testid="doc-icon-grid">
        {EMOJI.map(e => (
          <button key={e} type="button" aria-label={`Icon ${e}`} onClick={() => onPick(e)}
            className="flex h-9 w-9 items-center justify-center rounded-md text-[22px] hover:bg-bg-hover">{e}</button>
        ))}
      </div>
    </Popover>
  )
}

function CoverPicker({ current, onPick, onRemove, onClose }: { current?: string; onPick: (c: string) => void; onRemove: () => void; onClose: () => void }) {
  const [tab, setTab] = useState<'gallery' | 'link'>(current && isImageUrl(current) ? 'link' : 'gallery')
  const [link, setLink] = useState(current && isImageUrl(current) ? current : '')
  const swatch = (value: string, background: string, label: string) => (
    <button key={value} type="button" aria-label={label} aria-pressed={current === value} onClick={() => onPick(value)}
      className={`h-12 rounded-lg ring-offset-2 ring-offset-bg-elevated hover:opacity-90 ${current === value ? 'ring-2 ring-accent' : ''}`}
      style={{ background }} />
  )
  return (
    <Popover onClose={onClose} label="Choose a cover" className="right-0 top-full mt-2 w-[380px]">
      <div className="flex items-center gap-1 border-b border-border px-2 pt-1.5">
        {(['gallery', 'link'] as const).map(t => (
          <button key={t} type="button" onClick={() => setTab(t)} aria-pressed={tab === t}
            className={`-mb-px border-b-2 px-2 pb-1.5 text-[13px] ${tab === t ? 'border-text text-text' : 'border-transparent text-muted hover:text-text'}`}>
            {t === 'gallery' ? 'Gallery' : 'Link'}
          </button>
        ))}
        <span className="flex-1" />
        <Btn className={`${GHOST} mb-1`} onClick={onRemove} data-testid="doc-cover-remove">Remove</Btn>
      </div>
      {tab === 'gallery' ? (
        <div className="max-h-[340px] overflow-y-auto p-3" data-testid="doc-cover-gallery">
          <div className="mb-1.5 text-[12px] text-muted">Gradients</div>
          <div className="mb-3 grid grid-cols-4 gap-2">{GRADIENTS.map((g, i) => swatch(`gradient:${i}`, g, `Gradient ${i + 1}`))}</div>
          <div className="mb-1.5 text-[12px] text-muted">Colours</div>
          <div className="grid grid-cols-4 gap-2">{COLORS.map((c, i) => swatch(`color:${i}`, c, `Colour ${i + 1}`))}</div>
        </div>
      ) : (
        <form className="flex flex-col gap-2 p-3" onSubmit={e => { e.preventDefault(); if (isImageUrl(link)) onPick(link.trim()) }}>
          <input value={link} onChange={e => setLink(e.target.value)} placeholder="Paste an image link (https://…)" aria-label="Image link"
            data-testid="doc-cover-link" autoFocus
            className="rounded-md border border-border bg-bg px-2.5 py-2 text-sm text-text outline-none focus:border-accent" />
          <Btn primary type="submit" disabled={!isImageUrl(link)} className="self-start">Use this image</Btn>
          <div className="text-[12px] text-muted">Works with any https image. It is loaded from that address each time.</div>
        </form>
      )}
    </Popover>
  )
}

export default function DocHeader({ meta, onChange }: { meta: DocMeta; onChange: (next: DocMeta) => void }) {
  const [picker, setPicker] = useState<'icon' | 'cover' | null>(null)
  const cover = coverBackground(meta.cover)
  const set = (patch: Partial<DocMeta>) => { onChange({ ...meta, ...patch }); setPicker(null) }

  return (
    <div className="group/header relative" data-testid="doc-header">
      {cover && (
        <div className="group/cover relative h-[200px] w-full" style={{ background: cover }} data-testid="doc-cover">
          <div style={COLUMN} className="relative h-full">
            <div className="absolute bottom-3 right-5 flex gap-1 opacity-0 transition-opacity group-hover/cover:opacity-100 focus-within:opacity-100">
              <Btn className="border-transparent bg-bg-elevated/90 shadow-sm backdrop-blur" onClick={() => onChange({ ...meta, cover: randomGradient() })}
                aria-label="Shuffle cover"><Shuffle size={14} aria-hidden /></Btn>
              <Btn className="border-transparent bg-bg-elevated/90 shadow-sm backdrop-blur" onClick={() => setPicker(p => (p === 'cover' ? null : 'cover'))}
                data-testid="doc-cover-change">Change cover</Btn>
              {picker === 'cover' && (
                <CoverPicker current={meta.cover} onPick={c => set({ cover: c })} onRemove={() => set({ cover: undefined })} onClose={() => setPicker(null)} />
              )}
            </div>
          </div>
        </div>
      )}
      <div style={COLUMN}>
        {/* Lifted over the cover's edge, so only its own controls take clicks:
            the cover's buttons underneath stay reachable. */}
        <div className={`pointer-events-none relative ${cover ? (meta.icon ? '-mt-[42px]' : 'pt-6') : meta.icon ? 'pt-14' : 'pt-12'}`}>
          {meta.icon && (
            <div className="pointer-events-auto relative mb-1 w-fit">
              <button type="button" aria-label="Change icon" data-testid="doc-icon" onClick={() => setPicker(p => (p === 'icon' ? null : 'icon'))}
                className="flex h-[84px] w-[84px] items-center justify-center rounded-xl text-[68px] leading-none hover:bg-bg-hover/70">
                {meta.icon}
              </button>
              {picker === 'icon' && (
                <IconPicker onPick={e => set({ icon: e })} onRemove={() => set({ icon: undefined })} onClose={() => setPicker(null)} />
              )}
            </div>
          )}
          {/* Shown on hover, as Notion does: what the page does not have yet. */}
          <div className="pointer-events-auto relative flex h-8 w-fit items-center gap-1 opacity-0 transition-opacity group-hover/header:opacity-100 focus-within:opacity-100">
            {!meta.icon && (
              <Btn className={GHOST} data-testid="doc-add-icon" onClick={() => onChange({ ...meta, icon: EMOJI[Math.floor(Math.random() * 12)] })}>
                <Smile size={15} aria-hidden /> Add icon
              </Btn>
            )}
            {!cover && (
              <Btn className={GHOST} data-testid="doc-add-cover" onClick={() => onChange({ ...meta, cover: randomGradient() })}>
                <ImagePlus size={15} aria-hidden /> Add cover
              </Btn>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
