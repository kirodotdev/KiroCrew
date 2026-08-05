/**
 * Mochi - Gallery Panel
 *
 * Full gallery UI for browsing, previewing, and managing appearance packs.
 * Renders as the root component of the Gallery BrowserWindow.
 *
 * Tasks 11.1–11.6:
 * - Pack grid with cards (thumbnail, name, author, type badge, active highlight)
 * - Card click expands detail panel (animation thumbnails grid + state labels)
 * - Apply / Export buttons
 * - Custom pack Edit / Delete buttons
 * - Import Pack button (from .mochipack.zip)
 * - Listens to gallery:active-changed and gallery:packs-changed broadcasts
 */
import React, { useCallback, useEffect, useState } from 'react'
import type { PackMeta , SpriteConfig } from '../shared/appearanceTypes'
import { REQUIRED_STATES, OPTIONAL_STATES, ALL_MOODS } from '../shared/appearanceTypes'
import { toDataUri } from './animationResolver'
import { applySvgColorMap, type ColorMap } from '../shared/colorCustomizer'
import { LottieRenderer } from './LottieRenderer'
import { SpriteRenderer } from './SpriteRenderer'
import {
  Cat,
  Check,
  ChevronDown,
  Download,
  Package,
  Palette,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react'
import { resolveActivePackId } from '../../builtinPacks'
import { PackEditor } from './PackEditor'
import { PetdexImporter } from './PetdexImporter'
import { SpriteImporter, type SpritePrefillInput } from './SpriteImporter'
import { ColorCustomizerPanel } from './ColorCustomizer'
import { api } from '../mochiApi'
import { i18nT } from '../../../../i18n/t'
import { moodLabel, stateLabel } from '../../i18nKeys'

// ── Types ──────────────────────────────────────────────────────────────────

interface PackDetail {
  meta: PackMeta
  animations: Record<string, { content: string; format: 'svg' | 'lottie' | 'sprite' }>
  // SpriteConfig rather than an inline `{frameWidth, frameHeight, fps}`: this
  // local shape had drifted from the shared one, so `sprite.flipX` — which the
  // store has always persisted — was invisible here, and `tsc -b` rejected any
  // read of it. One vocabulary, as with the appearanceTypes re-export.
  sprite?: SpriteConfig
  /** The pack's art faces left; see PackManifest.flipX. */
  flipX?: boolean
}

type Mode = 'gallery' | 'editor' | 'sprite' | 'petdex'

// ── Styles ─────────────────────────────────────────────────────────────────

const S = {
  root: {
    width: '100%',
    height: '100%',
    background: 'var(--bg)',
    color: 'var(--text)',
    display: 'flex',
    flexDirection: 'column' as const,
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '14px 20px',
    borderBottom: '1px solid var(--border)',
    background: 'var(--header-bg)',
    backdropFilter: 'blur(12px)',
    flexShrink: 0,
  },
  title: {
    fontSize: 15,
    fontWeight: 600,
    flex: 1,
  },
  headerBtn: {
    padding: '6px 14px',
    borderRadius: 8,
    border: '1px solid var(--border)',
    background: 'var(--bg-input)',
    color: 'var(--text)',
    cursor: 'pointer',
    fontSize: 12,
    whiteSpace: 'nowrap' as const,
    transition: 'background 150ms',
  },
  headerBtnAccent: {
    padding: '6px 14px',
    borderRadius: 8,
    border: 'none',
    background: 'var(--accent)',
    color: 'var(--accent-text)',
    cursor: 'pointer',
    fontSize: 12,
    fontWeight: 600,
    whiteSpace: 'nowrap' as const,
    transition: 'opacity 150ms',
  },
  body: {
    flex: 1,
    overflowY: 'auto' as const,
    padding: 20,
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
    gap: 14,
  },
  card: (isActive: boolean, isSelected: boolean) => ({
    background: 'var(--bg-elevated)',
    borderRadius: 12,
    border: isActive
      ? '2px solid var(--accent)'
      : isSelected
        ? '2px solid var(--border-focus)'
        : '1px solid var(--border)',
    padding: isActive || isSelected ? 11 : 12,
    cursor: 'pointer',
    transition: 'border-color 150ms, box-shadow 150ms',
    boxShadow: isActive ? '0 0 12px var(--accent-glow)' : 'none',
    display: 'flex',
    flexDirection: 'column' as const,
    alignItems: 'center',
    gap: 8,
  }),
  thumbnail: {
    width: 80,
    height: 80,
    objectFit: 'contain' as const,
    borderRadius: 8,
    background: 'var(--bg-input)',
  },
  cardName: {
    fontSize: 13,
    fontWeight: 600,
    textAlign: 'center' as const,
    lineHeight: 1.3,
  },
  cardAuthor: {
    fontSize: 11,
    color: 'var(--text-muted)',
    textAlign: 'center' as const,
  },
  badge: (type: 'built-in' | 'custom') => ({
    fontSize: 10,
    padding: '2px 8px',
    borderRadius: 10,
    background: type === 'built-in' ? 'var(--accent-glow)' : 'var(--bg-input)',
    color: type === 'built-in' ? 'var(--accent)' : 'var(--text-muted)',
    border: `1px solid ${type === 'built-in' ? 'var(--accent)' : 'var(--border)'}`,
    fontWeight: 500,
  }),
  activeBadge: {
    fontSize: 10,
    padding: '2px 8px',
    borderRadius: 10,
    background: 'var(--success)',
    color: '#000',
    fontWeight: 600,
    marginLeft: 6,
  },
  // Detail panel
  detailOverlay: {
    position: 'fixed' as const,
    inset: 0,
    background: 'rgba(0,0,0,0.5)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 100,
  },
  detailPanel: {
    background: 'var(--bg)',
    borderRadius: 16,
    border: '1px solid var(--border)',
    width: '90%',
    maxWidth: 680,
    maxHeight: '85vh',
    overflowY: 'auto' as const,
    padding: 24,
    boxShadow: '0 8px 32px var(--shadow)',
  },
  detailHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    marginBottom: 20,
  },
  detailTitle: {
    fontSize: 16,
    fontWeight: 600,
    flex: 1,
  },
  closeBtn: {
    background: 'none',
    border: 'none',
    color: 'var(--text-muted)',
    cursor: 'pointer',
    fontSize: 20,
    padding: '4px 8px',
    borderRadius: 6,
    lineHeight: 1,
  },
  animGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(90px, 1fr))',
    gap: 10,
    marginBottom: 20,
  },
  animCell: {
    display: 'flex',
    flexDirection: 'column' as const,
    alignItems: 'center',
    gap: 4,
  },
  animThumb: {
    width: 64,
    height: 64,
    objectFit: 'contain' as const,
    borderRadius: 8,
    background: 'var(--bg-input)',
    border: '1px solid var(--border)',
  },
  animLabel: {
    fontSize: 10,
    color: 'var(--text-muted)',
    textAlign: 'center' as const,
  },
  sectionLabel: {
    fontSize: 11,
    color: 'var(--text-muted)',
    textTransform: 'uppercase' as const,
    letterSpacing: 1,
    marginBottom: 8,
    marginTop: 16,
  },
  btnRow: {
    display: 'flex',
    gap: 8,
    marginTop: 16,
    flexWrap: 'wrap' as const,
  },
  actionBtn: {
    padding: '8px 18px',
    borderRadius: 8,
    border: '1px solid var(--border)',
    background: 'var(--bg-input)',
    color: 'var(--text)',
    cursor: 'pointer',
    fontSize: 12,
    transition: 'background 150ms',
  },
  applyBtn: {
    padding: '8px 18px',
    borderRadius: 8,
    border: 'none',
    background: 'var(--accent)',
    color: 'var(--accent-text)',
    cursor: 'pointer',
    fontSize: 12,
    fontWeight: 600,
    transition: 'opacity 150ms',
  },
  dangerBtn: {
    padding: '8px 18px',
    borderRadius: 8,
    border: '1px solid var(--danger)',
    background: 'transparent',
    color: 'var(--danger)',
    cursor: 'pointer',
    fontSize: 12,
    transition: 'background 150ms',
  },
  loading: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    height: '100%',
    color: 'var(--text-muted)',
    fontSize: 13,
  },
  error: {
    padding: '12px 16px',
    borderRadius: 8,
    background: 'rgba(239,83,80,0.1)',
    border: '1px solid rgba(239,83,80,0.3)',
    color: 'var(--danger)',
    fontSize: 12,
    marginBottom: 16,
  },
} as const


/**
 * Button/label content: a lucide glyph beside the text.
 *
 * The glyph used to live INSIDE the translated string as an emoji, which made it
 * a property of the text — it could not follow the theme, sat on the font's
 * baseline at the font's size, and had to be repeated in every locale. Drawn here
 * in `currentColor` at a fixed size so it inherits the button's colour, including
 * the accent buttons where the text is dark on yellow.
 */
function WithIcon({ icon: Icon, children, size = 13 }: {
  icon: React.ComponentType<{ size?: number | string }>
  children: React.ReactNode
  size?: number
}) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <Icon size={size} />
      {children}
    </span>
  )
}

// ── Helper: render animation thumbnail ─────────────────────────────────────

function AnimThumbnail({ content, format, size = 64, spriteConfig, colorMap, flipX }: {
  content: string
  format: 'svg' | 'lottie' | 'sprite'
  size?: number
  spriteConfig?: SpriteConfig | null
  colorMap?: ColorMap | null
  /**
   * Mirror the art. ONE prop for every format: this used to be read straight off
   * `spriteConfig` inside the sprite branch, so a mirrored SVG or Lottie pack
   * (the built-in Kiro Ghost) faced the wrong way in the gallery while the pet
   * — which has always XOR'd a pack-level baseline — faced the right way.
   */
  flipX?: boolean
}) {
  const mirror = flipX === true ? 'scaleX(-1)' : 'none'
  if (format === 'svg' && content) {
    const processed = colorMap && Object.keys(colorMap).length > 0
      ? applySvgColorMap(content, colorMap) : content
    return (
      <img
        src={toDataUri(processed)}
        alt=""
        style={{ ...S.animThumb, width: size, height: size, transform: mirror }}
      />
    )
  }
  if (format === 'lottie' && content) {
    return (
      <div style={{ ...S.animThumb, width: size, height: size, overflow: 'hidden', transform: mirror }}>
        <LottieRenderer animationData={content} width={size} height={size} />
      </div>
    )
  }
  if (format === 'sprite' && content) {
    const src = content.startsWith('data:') ? content : `data:image/png;base64,${content}`
    const fw = spriteConfig?.frameWidth || 32
    const fh = spriteConfig?.frameHeight || 32
    return (
      <div style={{ ...S.animThumb, width: size, height: size, overflow: 'hidden', imageRendering: 'pixelated', transform: mirror }}>
        <SpriteRenderer src={src} frameWidth={fw} frameHeight={fh} fps={spriteConfig?.fps || 6} displaySize={size} />
      </div>
    )
  }
  // Reaching here means the slot had NO usable art, which draws as an empty box
  // indistinguishable from a slot the pack legitimately omits. That ambiguity is
  // what made "the ghost shows nothing" undiagnosable, so say which it is.
  // eslint-disable-next-line no-console
  console.warn('[mochi] animation thumbnail has no usable art', {
    format,
    contentLength: content?.length ?? 0,
    contentHead: typeof content === 'string' ? content.slice(0, 24) : String(content),
  })
  return (
    <div style={{
      ...S.animThumb,
      width: size,
      height: size,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      fontSize: size * 0.35,
      color: 'var(--text-faint)',
    }}>
      <Plus size={16} />
    </div>
  )
}

// ── Pack Card ──────────────────────────────────────────────────────────────

function PackCard({ pack, isActive, isSelected, onClick, thumbnailContent, spriteConfig, colorMap, flipX }: {
  pack: PackMeta
  isActive: boolean
  isSelected: boolean
  onClick: () => void
  thumbnailContent?: string
  spriteConfig?: { frameWidth: number; frameHeight: number; fps: number }
  /** The pack's art faces left; see PackManifest.flipX. */
  flipX?: boolean
  colorMap?: ColorMap | null
}) {
  return (
    <div role="button" tabIndex={0} aria-pressed={isSelected} style={S.card(isActive, isSelected)} onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}>
      {thumbnailContent ? (
        <AnimThumbnail content={thumbnailContent} format={pack.format} size={80} spriteConfig={spriteConfig} colorMap={pack.id === 'default-mochi' ? colorMap : null} flipX={flipX} />
      ) : (
        <div style={{ ...S.thumbnail, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)' }}>
          <Cat size={28} />
        </div>
      )}
      <div style={S.cardName}>{pack.name}</div>
      <div style={S.cardAuthor}>{pack.author}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span style={S.badge(pack.type)}>
          {pack.type === 'built-in' ? i18nT('apps.mochi.gallery.built_in') : i18nT('apps.mochi.gallery.custom')}
        </span>
        {isActive && <span style={S.activeBadge}><WithIcon icon={Check} size={11}>{i18nT('apps.mochi.gallery.active')}</WithIcon></span>}
      </div>
    </div>
  )
}

// ── Detail Panel ───────────────────────────────────────────────────────────

function DetailPanel({ detail, isActive, onClose, onApply, onExport, onEdit, onDelete, colorMap }: {
  detail: PackDetail
  isActive: boolean
  onClose: () => void
  onApply: () => void
  onExport: () => void
  onEdit: () => void
  onDelete: () => void
  colorMap?: ColorMap | null
}) {
  const { meta, animations } = detail
  const sc = detail.sprite
  // `flipX ?? sprite?.flipX`: the pack-level flag is authoritative, and the
  // sprite one is the pre-existing form an imported sheet still uses.
  const mirror = detail.flipX ?? sc?.flipX
  const [showColorCustomizer, setShowColorCustomizer] = useState(false)
  const isDefaultMochi = meta.id === 'default-mochi'
  const thumbColorMap = isDefaultMochi ? colorMap : null

  const stateEntries = REQUIRED_STATES.map((s) => ({
    key: s,
    label: stateLabel(s),
    anim: animations[s],
    required: true,
  }))

  const optionalEntries = OPTIONAL_STATES
    .filter((s) => animations[s])
    .map((s) => ({
      key: s,
      label: stateLabel(s),
      anim: animations[s],
      required: false,
    }))

  const moodEntries = ALL_MOODS
    .filter((m) => animations[m])
    .map((m) => ({
      key: m,
      label: moodLabel(m),
      anim: animations[m],
      required: false,
    }))

  return (
    <div role="presentation" style={S.detailOverlay} onClick={onClose}>
      <div role="presentation" style={S.detailPanel} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={S.detailHeader}>
          <div style={{ flex: 1 }}>
            <div style={S.detailTitle}>{meta.name}</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              {meta.author} · {meta.format.toUpperCase()}
            </div>
            {meta.description && (
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4, fontStyle: 'italic' }}>
                {meta.description}
              </div>
            )}
          </div>
          <span style={S.badge(meta.type)}>
            {meta.type === 'built-in' ? i18nT('apps.mochi.gallery.built_in') : i18nT('apps.mochi.gallery.custom')}
          </span>
          {isActive && <span style={S.activeBadge}><WithIcon icon={Check} size={11}>{i18nT('apps.mochi.gallery.active')}</WithIcon></span>}
          <button
            style={S.closeBtn}
            onClick={onClose}
            aria-label={i18nT('apps.mochi.watchPanel.close')}
          ><X size={13} /></button>
        </div>

        {/* Color customize toggle — pill button below header */}
        {isDefaultMochi && (
          <button
            onClick={() => setShowColorCustomizer(!showColorCustomizer)}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              margin: '0 0 12px', padding: '6px 14px',
              borderRadius: 20, border: showColorCustomizer ? '1px solid var(--accent)' : '1px solid var(--border)',
              background: showColorCustomizer ? 'var(--accent-glow)' : 'var(--bg-input)',
              color: showColorCustomizer ? 'var(--accent)' : 'var(--text)',
              cursor: 'pointer', fontSize: 12, fontWeight: 500,
              transition: 'all 200ms ease',
            }}
          >
            <Palette size={13} />
            <ChevronDown size={13} style={{ transition: 'transform 200ms', transform: showColorCustomizer ? 'rotate(180deg)' : 'none' }} />
            {showColorCustomizer ? i18nT('apps.mochi.color.hide_btn') : i18nT('apps.mochi.color.customize_btn')}
          </button>
        )}

        {/* Color customizer panel with slide animation */}
        {isDefaultMochi && animations.idle && (
          <div style={{
            maxHeight: showColorCustomizer ? 2000 : 0,
            opacity: showColorCustomizer ? 1 : 0,
            overflow: 'hidden',
            transition: 'max-height 350ms ease, opacity 250ms ease',
          }}>
            <ColorCustomizerPanel idleSvgContent={animations.idle.content} />
          </div>
        )}

        {/* State animations */}
        <div style={S.sectionLabel}>{i18nT('apps.mochi.gallery.states')}</div>
        <div style={S.animGrid}>
          {[...stateEntries, ...optionalEntries].map(({ key, label, anim }) => (
            <div key={key} style={S.animCell}>
              {anim ? (
                <AnimThumbnail content={anim.content} format={anim.format} spriteConfig={sc} colorMap={thumbColorMap} flipX={mirror} />
              ) : (
                <div style={{ ...S.animThumb, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-faint)', fontSize: 11 }}>—</div>
              )}
              <div style={S.animLabel}>{label}</div>
            </div>
          ))}
        </div>

        {/* Mood animations (only if any exist) */}
        {moodEntries.length > 0 && (
          <>
            <div style={S.sectionLabel}>{i18nT('apps.mochi.gallery.moods')}</div>
            <div style={S.animGrid}>
              {moodEntries.map(({ key, label, anim }) => (
                <div key={key} style={S.animCell}>
                  {anim ? (
                    <AnimThumbnail content={anim.content} format={anim.format} spriteConfig={sc} colorMap={thumbColorMap} flipX={mirror} />
                  ) : (
                    <div style={{ ...S.animThumb, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-faint)', fontSize: 11 }}>—</div>
                  )}
                  <div style={S.animLabel}>{label}</div>
                </div>
              ))}
            </div>
          </>
        )}

        {/* Action buttons */}
        <div style={S.btnRow}>
          {!isActive && (
            <button style={S.applyBtn} onClick={onApply}>
              <WithIcon icon={Sparkles}>{i18nT('apps.mochi.gallery.apply')}</WithIcon>
            </button>
          )}
          {meta.type === 'custom' && (
            <>
              <button style={S.actionBtn} onClick={onExport}>
                <WithIcon icon={Package}>{i18nT('apps.mochi.gallery.export')}</WithIcon>
              </button>
              <button style={S.actionBtn} onClick={onEdit}>
                <WithIcon icon={Pencil}>{i18nT('apps.mochi.gallery.edit')}</WithIcon>
              </button>
              <button style={S.dangerBtn} onClick={onDelete}>
                <WithIcon icon={Trash2}>{i18nT('apps.mochi.gallery.delete')}</WithIcon>
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}


// ── Main Gallery Panel ─────────────────────────────────────────────────────

export const GalleryPanel: React.FC = () => {
  const [packs, setPacks] = useState<PackMeta[]>([])
  const [activePackId, setActivePackId] = useState<string>('')
  const [selectedPackId, setSelectedPackId] = useState<string | null>(null)
  const [detail, setDetail] = useState<PackDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Kept separate from `error`: that one renders in the gallery view's banner,
  // while this one has to reach the importer's own footer so a failed save can
  // be reported WITHOUT navigating away from the form it would discard.
  const [packSaveError, setPackSaveError] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('gallery')
  const [editingPack, setEditingPack] = useState<PackMeta | undefined>(undefined)
  // Thumbnail content cache: packId → SVG/Lottie content for the thumbnail
  const [thumbs, setThumbs] = useState<Record<string, string>>({})
  // Carried from the Petdex screen into the sprite importer, which owns the
  // mapping step. Cleared on leaving 'sprite' so a later plain import is blank.
  const [prefill, setPrefill] = useState<SpritePrefillInput | undefined>(undefined)
  const [spriteConfigs, setSpriteConfigs] = useState<Record<string, { frameWidth: number; frameHeight: number; fps: number }>>({})
  // packId -> "this pack's art faces left". Cached with the thumbnails because
  // the card grid only holds PackMeta, which does not carry the flag.
  const [flips, setFlips] = useState<Record<string, boolean>>({})
  // Color map for default-mochi thumbnails
  const [mochiColorMap, setMochiColorMap] = useState<ColorMap | null>(null)

  // ── Data fetching ──────────────────────────────────────────────────────

  const fetchPacks = useCallback(async () => {
    try {
      const list: PackMeta[] = await api.galleryListPacks()
      setPacks(list)

      // Fetch thumbnail content for each pack via get-pack-detail
      // (the detail response includes animation content keyed by state name)
      const thumbMap: Record<string, string> = {}
      const scMap: Record<string, { frameWidth: number; frameHeight: number; fps: number }> = {}
      const flipMap: Record<string, boolean> = {}
      for (const p of list) {
        try {
          const d = await api.galleryGetPackDetail(p.id)
          if (d?.animations?.idle) {
            thumbMap[p.id] = d.animations.idle.content
          }
          if (d?.sprite) scMap[p.id] = d.sprite
          const mirrored = d?.flipX ?? d?.sprite?.flipX
          if (mirrored === true) flipMap[p.id] = true
        } catch {}
      }
      setThumbs(thumbMap)
      setSpriteConfigs(scMap)
      setFlips(flipMap)
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.load_packs'))
    }
  }, [])

  const fetchActiveId = useCallback(async () => {
    try {
      const cfg = await api.getMochiConfig()
      setActivePackId(resolveActivePackId(cfg))
      // Load colorMap for default-mochi thumbnails
      const cm = await api?.presetsGetColorMap?.('default-mochi')
      setMochiColorMap(cm && Object.keys(cm).length > 0 ? cm : null)
    } catch {
      setActivePackId('default-mochi')
    }
  }, [])

  // Initial load
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      await Promise.all([fetchPacks(), fetchActiveId()])
      if (!cancelled) setLoading(false)
    })()
    return () => { cancelled = true }
  }, [fetchPacks, fetchActiveId])

  // ── Broadcast listeners (task 11.6) ────────────────────────────────────

  useEffect(() => {
    const offActive = api.onGalleryActiveChanged((data: any) => {
      if (data?.packId) {
        setActivePackId(data.packId)
      } else {
        fetchActiveId()
      }
    })
    const offPacks = api.onGalleryPacksChanged(() => {
      fetchPacks()
    })
    // Listen for color map changes to update default-mochi thumbnails
    const offColor = api.onColorMapChanged?.((data: { packId: string; colorMap: Record<string, string> }) => {
      if (data.packId === 'default-mochi') {
        setMochiColorMap(data.colorMap && Object.keys(data.colorMap).length > 0 ? data.colorMap : null)
      }
    })
    return () => {
      offActive()
      offPacks()
      offColor?.()
    }
  }, [fetchActiveId, fetchPacks])

  // ── Card click → fetch detail ──────────────────────────────────────────

  const handleCardClick = useCallback(async (packId: string) => {
    if (selectedPackId === packId) {
      // Toggle off
      setSelectedPackId(null)
      setDetail(null)
      return
    }
    setSelectedPackId(packId)
    setError(null)
    try {
      const d = await api.galleryGetPackDetail(packId)
      if (d === null) {
        setError(i18nT('apps.mochi.errors.load_pack_detail'))
        setDetail(null)
        return
      }
      setDetail({ meta: d.meta, animations: d.animations, sprite: d.sprite, flipX: d.flipX })
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.load_pack_detail'))
      setDetail(null)
    }
  }, [selectedPackId])

  // ── Actions ────────────────────────────────────────────────────────────

  const handleApply = useCallback(async () => {
    if (!detail) return
    try {
      const result = await api.gallerySetActive(detail.meta.id)
      if (result && !result.ok) {
        setError(result.error || i18nT('apps.mochi.errors.apply_pack'))
        return
      }
      // Only AFTER the write is confirmed. Setting this first made the card show
      // "Active" for a pack that had not been stored, which is what made a failed
      // apply look like a successful one that the pet ignored.
      setActivePackId(detail.meta.id)
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.apply_pack'))
    }
  }, [detail])

  const [toast, setToast] = useState<string | null>(null)
  const [toastFading, setToastFading] = useState(false)

  const showToast = (msg: string) => {
    setToast(msg); setToastFading(false)
    setTimeout(() => setToastFading(true), 2000)
    setTimeout(() => { setToast(null); setToastFading(false) }, 2500)
  }

  const handleExport = useCallback(async () => {
    if (!detail) return
    try {
      const result = await api.galleryExport(detail.meta.id)
      if (!result) return // cancelled
      if (result.ok) {
        showToast(i18nT('apps.mochi.gallery.export_success'))
      } else {
        setError(result.error || i18nT('apps.mochi.errors.export'))
      }
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.export'))
    }
  }, [detail])

  const handleEdit = useCallback(() => {
    if (!detail) return
    setEditingPack(detail.meta)
    setSelectedPackId(null)
    setDetail(null)
    setMode(detail.meta.format === 'sprite' ? 'sprite' : 'editor')
  }, [detail])

  const handleDelete = useCallback(async () => {
    if (!detail) return
    if (!confirm(i18nT('apps.mochi.gallery.delete_confirm', { name: detail.meta.name }))) return
    try {
      const result = await api.galleryDelete(detail.meta.id)
      if (result && !result.ok) {
        setError(result.error || i18nT('apps.mochi.errors.delete'))
        return
      }
      setSelectedPackId(null)
      setDetail(null)
      fetchPacks()
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.delete'))
    }
  }, [detail, fetchPacks])

  const handleImportBundle = useCallback(async () => {
    try {
      const result = await api.galleryImportBundle()
      if (!result) return // cancelled
      if (!result.ok) {
        setError(result.error || i18nT('apps.mochi.errors.import'))
        return
      }
      showToast(i18nT('apps.mochi.gallery.import_success'))
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.import'))
    }
  }, [])

  const handleCreateNew = useCallback(() => {
    setEditingPack(undefined)
    setMode('editor')
  }, [])

  // ── Editor mode → render PackEditor ──────────────────────────────────────

  if (mode === 'editor') {
    return (
      <PackEditor
        existingPack={editingPack}
        onSave={() => {
          showToast(i18nT(editingPack ? 'apps.mochi.editor.save_success' : 'apps.mochi.editor.create_success'))
          setMode('gallery')
          setEditingPack(undefined)
          fetchPacks()
        }}
        onCancel={() => {
          setMode('gallery')
          setEditingPack(undefined)
        }}
      />
    )
  }

  if (mode === 'petdex') {
    return (
      <PetdexImporter
        onReady={(next) => { setPrefill(next); setEditingPack(undefined); setMode('sprite') }}
        onUseFile={() => { setPrefill(undefined); setEditingPack(undefined); setMode('sprite') }}
        onCancel={() => setMode('gallery')}
      />
    )
  }

  if (mode === 'sprite') {
    return (
      <SpriteImporter
        existingPack={editingPack}
        prefill={prefill}
        saveError={packSaveError}
        onDone={async (result: any) => {
          setPackSaveError(null)
          // Save sprite pack via IPC
          const res = await api?.gallerySaveSpritePack?.(result)
          if (!res?.ok) {
            // STAY in the importer. Resetting `prefill` and leaving for the
            // gallery (which is where the page's error banner lives) discarded
            // every frame, row and name the user had configured — the work is
            // unrecoverable, and re-importing the sheet is the only way back.
            setPackSaveError(res?.error || i18nT('apps.mochi.errors.create_sprite_pack'))
            return
          }
          showToast(i18nT(editingPack ? 'apps.mochi.editor.save_success' : 'apps.mochi.editor.create_success'))
          // If overwriting the active pack, re-apply it
          const packId = (result as any).overwriteId || res.packId
          if (packId && packId === activePackId) {
            api?.gallerySetActive?.(packId)
          }
          fetchPacks()
          setPrefill(undefined)
          setMode('gallery')
        }}
        onCancel={() => { setPackSaveError(null); setPrefill(undefined); setMode('gallery') }}
      />
    )
  }

  // ── Gallery view ───────────────────────────────────────────────────────

  return (
    <div style={S.root}>
      {/* Header with title + action buttons */}
      <div style={S.header}>
        <span style={S.title}><WithIcon icon={Palette} size={15}>{i18nT('apps.mochi.gallery.title')}</WithIcon></span>
        <button style={S.headerBtn} onClick={handleImportBundle}>
          <WithIcon icon={Download}>{i18nT('apps.mochi.gallery.import_bundle')}</WithIcon>
        </button>
        <button style={S.headerBtnAccent} onClick={() => setMode('petdex')}>
          <WithIcon icon={Plus}>{i18nT('apps.mochi.petdex.title')}</WithIcon>
        </button>
        <button style={S.headerBtnAccent} onClick={() => { setEditingPack(undefined); setPrefill(undefined); setMode('sprite') }}>
          <WithIcon icon={Plus}>{i18nT('apps.mochi.sprite.title')}</WithIcon>
        </button>
        <button style={S.headerBtnAccent} onClick={handleCreateNew}>
          <WithIcon icon={Plus}>{i18nT('apps.mochi.gallery.create_new')}</WithIcon>
        </button>
      </div>

      {/* Body */}
      <div style={S.body}>
        {/* Error banner */}
        {toast && (
          <div style={{ padding: '8px 14px', borderRadius: 8, background: 'rgba(76,175,80,0.15)', border: '1px solid rgba(76,175,80,0.3)', color: 'var(--text)', fontSize: 12, marginBottom: 8, animation: toastFading ? 'toastOut 0.5s forwards' : 'toastIn 0.3s ease-out' }}>{toast}</div>
        )}
        {error && (
          <div style={S.error}>
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              aria-label={i18nT('apps.mochi.chatPanel.dismiss')}
              style={{ float: 'right', background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', fontSize: 14 }}
            ><X size={13} /></button>
          </div>
        )}

        {loading ? (
          <div style={S.loading}>{i18nT('apps.mochi.gallery.loading')}</div>
        ) : packs.length === 0 ? (
          <div style={S.loading}>{i18nT('apps.mochi.gallery.empty')}</div>
        ) : (
          <div style={S.grid}>
            {packs.map((pack) => (
              <PackCard
                key={pack.id}
                pack={pack}
                isActive={pack.id === activePackId}
                isSelected={pack.id === selectedPackId}
                onClick={() => handleCardClick(pack.id)}
                thumbnailContent={thumbs[pack.id]}
                spriteConfig={spriteConfigs[pack.id]}

                colorMap={mochiColorMap}
                flipX={flips[pack.id]}
              />
            ))}
          </div>
        )}
      </div>

      {/* Detail panel overlay */}
      {detail && selectedPackId && (
        <DetailPanel
          detail={detail}
          isActive={detail.meta.id === activePackId}
          onClose={() => { setSelectedPackId(null); setDetail(null) }}
          onApply={handleApply}
          onExport={handleExport}
          onEdit={handleEdit}
          onDelete={handleDelete}

          colorMap={mochiColorMap}

        />
      )}
    </div>
  )
}
