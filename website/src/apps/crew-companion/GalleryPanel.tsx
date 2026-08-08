/**
 * CrewCompanion - Gallery Panel
 *
 * Full gallery UI for browsing, previewing, and managing appearance packs.
 * Renders as the root component of the Gallery BrowserWindow.
 *
 * Tasks 11.1–11.6:
 * - Pack grid with cards (thumbnail, name, author, type badge, active highlight)
 * - Card click expands detail panel (animation thumbnails grid + state labels)
 * - Apply / Export buttons
 * - Custom pack Edit / Delete buttons
 * - Import Pack button (from .kiropack.zip)
 * - Listens to gallery:active-changed and gallery:packs-changed broadcasts
 */
import { ChevronDown, MoreHorizontal, Plus, X } from 'lucide-react'
import React, { useCallback, useEffect, useState } from 'react'
import type { PackMeta } from './appearanceTypes'
import { REQUIRED_STATES, STATUS_STATES, RANDOM_STATES, LEGACY_STATES, ALL_MOODS } from './appearanceTypes'
import { toDataUri } from './animationResolver'
import { applySvgColorMap, type ColorMap } from './colorCustomizer'
import { ghostPoseForKey } from './ghostEyes'
import { GhostEyeOverlay } from './GhostEyeOverlay'
import { LottieRenderer } from './LottieRenderer'
import { SpriteRenderer } from './SpriteRenderer'
import { PackEditor } from './PackEditor'
import { SpriteImporter } from './SpriteImporter'
import { buildSpritePackData, firstFramePreview } from './petdexImport'
import { ColorCustomizerPanel } from './ColorCustomizerPanel'
import { GHOST_ACCESSORIES, type GhostAccessory } from './ghostAccessories'
import { i18nT } from '../../i18n/t'
// The built-in ghost's body art. Bundled with the frontend and imported as a URL
// by the bundler — the SAME asset PetAvatar renders — so the gallery card and the
// live companion can never show a different ghost.
import ghostIdleUrl from './assets/kiro_idle.svg'

const api = galleryApi

/**
 * The built-in pack's id, canonical in the backend (appearances.py `DEFAULT_PACK`)
 * and in PetAvatar. This file previously used a stale `'default-kiro'` literal that
 * matches no real pack, which both broke the built-in's active-highlight/colour
 * wiring and made `appearances/detail?id=default-kiro` 400 (no such pack).
 */
const DEFAULT_PACK = 'kiro-ghost'

// ── Types ──────────────────────────────────────────────────────────────────

/**
 * Sprite-sheet metadata as it arrives from the bridge's pack detail. Every field is
 * optional because the manifest may omit any of them; the render sites already guard
 * with `|| <fallback>`. `flipX` mirrors the sprite horizontally.
 */
type SpriteMeta = { frameWidth?: number; frameHeight?: number; fps?: number; flipX?: boolean }

interface PackDetail {
  meta: PackMeta
  animations: Record<string, AnimationEntry>
  sprite?: SpriteMeta
}

/**
 * The shape `petdexFetch` resolves to. The bridge types the call loosely as
 * `Record<string, unknown>` because it just forwards the gateway's JSON; this is the
 * concrete payload the lookup returns, used to read its fields with types.
 */
interface PetdexResult {
  ok?: boolean
  error?: string
  slug: string
  displayName: string
  author: string
  description: string
  spriteBase64: string
}

type Mode = 'gallery' | 'editor' | 'sprite'

import { PANEL_FONT, PANEL_RADIUS } from './panelSkin'
// The transparent gutter the card's shadow lives in. Shared with the window
// manager and gallery.html so the three cannot drift.
import { GALLERY_PAD } from './constants'
import { galleryApi, type GalleryResult } from './petBridge'
import type { CSSProperties } from 'react'
import { entryContent, entryFormat, type AnimationEntry } from './packAnimation'
import { DRAG_REGION, NO_DRAG_REGION } from './appRegion'
import { slotLabel } from './slotLabel'
import { errorText } from './errorText'
import type { SpriteImportResult } from './spriteImportTypes'
import './gallery.css'
import { splitOnPlaceholder } from './splitOnPlaceholder'


// ── Styles ─────────────────────────────────────────────────────────────────

const S = {
  root: {
    width: '100%',
    height: '100%',
    // The card, not the window: frameless + transparent host, so the rounded corners
    // and shadow live here. Radius shared with the panel.
    borderRadius: PANEL_RADIUS.card,
    overflow: 'hidden',
    // Reach (6 + 18 = 24) equals GALLERY_PAD, so the blur is never clipped into a
    // seam by the window edge — the same bound the panel's shadow test enforces.
    boxShadow: 'var(--cc-card-shadow)',
    background: 'var(--bg)',
    color: 'var(--text)',
    display: 'flex',
    flexDirection: 'column' as const,
    // Same typeface as the pet's panel — see panelSkin.PANEL_FONT.
    fontFamily: PANEL_FONT,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    // No traffic-light inset: the window is frameless, so there are none to clear.
    padding: '13px 14px 12px 18px',
    // The hidden bar takes the OS drag area with it, so the header provides one.
    ...DRAG_REGION,
    userSelect: 'none' as const,
    borderBottom: '1px solid var(--border)',
    /**
     * Transparent, not `--header-bg`. With the title bar hidden the header IS the top
     * edge, so it should read as part of the page. It also removes a real risk: the
     * app's `--header-bg` resolves to `var(--chrome, #211d25)`, and that dark literal
     * would paint a dark strip across a light theme if the dashboard does not define
     * `--chrome`. The bottom border still separates it from the grid.
     */
    background: 'transparent',
    flexShrink: 0,
  },
  title: {
    fontSize: 15,
    fontWeight: 600,
    flex: 1,
  },
  /**
   * The window's own ✕. Named distinctly from `closeBtn` below, which is the detail
   * panel's — an earlier version of this reused that name and, being an object
   * literal, the later key silently won and dropped the `no-drag` needed to keep
   * this button clickable inside the header's drag region.
   */
  windowCloseBtn: {
    ...NO_DRAG_REGION,
    // 28px for the same reason as the panel's ✕: a 22px box with a 50% radius is
    // both under the 24px minimum target and has dead corners.
    width: 28,
    height: 28,
    borderRadius: '50%',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    border: 'none',
    background: 'transparent',
    color: 'var(--text-muted)',
    fontSize: 14,
    lineHeight: 1,
    cursor: 'pointer',
    flexShrink: 0,
  } as const,
  headerBtn: {
    ...NO_DRAG_REGION,
    padding: '6px 14px',
    borderRadius: PANEL_RADIUS.pill,
    border: '1px solid var(--border)',
    background: 'var(--cc-input-bg)',
    color: 'var(--text)',
    cursor: 'pointer',
    fontSize: 12,
    whiteSpace: 'nowrap' as const,
    transition: 'background 150ms',
  },
  headerBtnAccent: {
    ...NO_DRAG_REGION,
    padding: '6px 14px',
    // Fully rounded, like the panel's "Let's breathe" CTA.
    borderRadius: PANEL_RADIUS.pill,
    border: 'none',
    background: 'var(--accent)',
    color: 'var(--cc-accent-text)',
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
    // Panel's card radius, so the two surfaces read as one product.
    borderRadius: PANEL_RADIUS.card,
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
    borderRadius: PANEL_RADIUS.thumb,
    background: 'var(--cc-input-bg)',
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
  // FILLED, matching the panel's pills. The old form was accent text on an
  // accent tint, which measures 2.58:1 in the fallback theme — well below AA.
  badge: (type: 'built-in' | 'custom') => ({
    fontSize: 10,
    padding: '2px 8px',
    borderRadius: PANEL_RADIUS.pill,
    background: type === 'built-in' ? 'var(--accent)' : 'var(--cc-input-bg)',
    color: type === 'built-in' ? 'var(--cc-accent-text)' : 'var(--text-muted)',
    border: type === 'built-in' ? 'none' : '1px solid var(--border)',
    fontWeight: 600,
  }),
  activeBadge: {
    fontSize: 10,
    padding: '2px 8px',
    borderRadius: PANEL_RADIUS.pill,
    background: 'var(--success)',
    // Was a hardcoded #000, which ignored the theme entirely.
    color: 'var(--cc-accent-text)',
    fontWeight: 600,
    marginLeft: 6,
  },
  modal: {
    background: 'var(--bg)',
    borderRadius: PANEL_RADIUS.card,
    border: '1px solid var(--border)',
    width: '90%',
    maxWidth: 420,
    boxShadow: 'var(--cc-modal-shadow)',
    overflow: 'hidden' as const,
  },
  /**
   * The editor is a modal like the import dialog, but nearly card-sized: it holds a
   * form plus several slot grids, so it fills the card and scrolls internally rather
   * than being centred at dialog width.
   */
  editorModal: {
    background: 'var(--bg)',
    borderRadius: PANEL_RADIUS.card,
    border: '1px solid var(--border)',
    width: '100%',
    height: '100%',
    display: 'flex',
    flexDirection: 'column' as const,
    overflow: 'hidden' as const,
  },
  modalHeader: {
    display: 'flex',
    alignItems: 'center',
    padding: '12px 16px',
    borderBottom: '1px solid var(--border)',
  },
  // Detail panel
  /**
   * Dim layer for the detail / import dialogs.
   *
   * Inset by GALLERY_PAD rather than `inset: 0`: the window is frameless and
   * transparent, and that pad is the gutter the card's shadow occupies. A
   * viewport-anchored overlay painted straight across it, so the dim appeared to
   * spill outside the window and squared off the card's rounded corners. Matching
   * the card's radius keeps it inside the same silhouette.
   */
  detailOverlay: {
    position: 'fixed' as const,
    inset: GALLERY_PAD,
    borderRadius: PANEL_RADIUS.card,
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
    background: 'var(--cc-input-bg)',
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
    background: 'var(--cc-input-bg)',
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
    color: 'var(--cc-accent-text)',
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
} as const satisfies Record<
  string,
  | CSSProperties
  | ((isActive: boolean, isSelected: boolean) => CSSProperties)
  | ((type: 'built-in' | 'custom') => CSSProperties)
>


// ── Helper: render animation thumbnail ─────────────────────────────────────

function AnimThumbnail({ content, format, size = 64, spriteConfig, colorMap, eyePose }: {
  content: string
  format: 'svg' | 'lottie' | 'sprite'
  size?: number
  spriteConfig?: SpriteMeta | null
  colorMap?: ColorMap | null
  /** Set for the built-in ghost only: its body SVGs are eyeless because the live
   *  pet draws the eyes as an overlay, so a raw thumbnail renders a blank face.
   *  Pass the pose whose eye positions this state uses. */
  eyePose?: string
}) {
  if (format === 'svg' && content) {
    const processed = colorMap && Object.keys(colorMap).length > 0
      ? applySvgColorMap(content, colorMap) : content
    const img = (
      <img
        src={toDataUri(processed)}
        alt=""
        style={{ ...S.animThumb, width: size, height: size }}
      />
    )
    if (!eyePose) return img
    // The eye percentages are relative to the same letterboxed square the pet
    // uses, so they land correctly at any size as long as object-fit is contain.
    return (
      <div style={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
        {img}
        <GhostEyeOverlay pose={eyePose} size={size} />
      </div>
    )
  }
  if (format === 'lottie' && content) {
    return (
      <div style={{ ...S.animThumb, width: size, height: size, overflow: 'hidden' }}>
        <LottieRenderer animationData={content} width={size} height={size} />
      </div>
    )
  }
  if (format === 'sprite' && content) {
    const src = content.startsWith('data:') ? content : `data:image/png;base64,${content}`
    const fw = spriteConfig?.frameWidth || 32
    const fh = spriteConfig?.frameHeight || 32
    return (
      <div style={{ ...S.animThumb, width: size, height: size, overflow: 'hidden', imageRendering: 'pixelated', transform: spriteConfig?.flipX ? 'scaleX(-1)' : 'none' }}>
        <SpriteRenderer src={src} frameWidth={fw} frameHeight={fh} fps={spriteConfig?.fps || 6} displaySize={size} />
      </div>
    )
  }
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
      <Plus size={20} aria-hidden="true" />
    </div>
  )
}

// ── Helper: built-in ghost thumbnail ───────────────────────────────────────

/**
 * The built-in "Kiro" pack's card art.
 *
 * The built-in ghost is NOT a file the backend can serve — its body is bundled with
 * the frontend and its detail payload comes back with empty `animations`, so the
 * generic thumbnail path had nothing to draw and fell back to a placeholder. This
 * renders the bundled body exactly the way PetAvatar does: the same `kiro_idle.svg`
 * asset, the user's colour map applied to its text when present, and the eyes drawn
 * as a separate overlay (the body is deliberately eyeless). That keeps the gallery
 * card and the live companion in lock-step.
 */
function BuiltinThumbnail({ size, colorMap }: { size: number; colorMap?: ColorMap | null }) {
  // The SVG is a bundler URL, so a colour map has to be applied to its fetched text.
  // With no map we point straight at the URL — the same branch PetAvatar takes.
  const [recolouredUri, setRecolouredUri] = useState<string | null>(null)
  useEffect(() => {
    if (!colorMap || Object.keys(colorMap).length === 0) { setRecolouredUri(null); return }
    let alive = true
    fetch(ghostIdleUrl)
      .then((r) => r.text())
      .then((raw) => { if (alive) setRecolouredUri(toDataUri(applySvgColorMap(raw, colorMap))) })
      .catch(() => {})
    return () => { alive = false }
  }, [colorMap])

  return (
    <div style={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <img
        src={recolouredUri ?? ghostIdleUrl}
        alt=""
        style={{ ...S.thumbnail, width: size, height: size }}
      />
      <GhostEyeOverlay pose="primary" size={size} />
    </div>
  )
}

// ── Pack Card ──────────────────────────────────────────────────────────────

function PackCard({ pack, isActive, isSelected, onClick, onManage, thumbnailContent, spriteConfig, colorMap }: {
  pack: PackMeta
  isActive: boolean
  isSelected: boolean
  onClick: () => void
  onManage?: () => void
  thumbnailContent?: string
  spriteConfig?: SpriteMeta
  i18nT: (key: string, vars?: Record<string, string>) => string
  colorMap?: ColorMap | null
}) {
  return (
    <div role="button" tabIndex={0} aria-pressed={isActive} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }} style={{ ...S.card(isActive, isSelected), position: 'relative' }} onClick={onClick} title={isActive ? undefined : i18nT('apps.crewCompanion.gallery.useNamed', { name: pack.name })}>
      {onManage && (
        <button
          onClick={(e) => { e.stopPropagation(); onManage() }}
          title={i18nT('apps.crewCompanion.gallery.manage')}
          aria-label={i18nT('apps.crewCompanion.gallery.manage')}
          style={{
            position: 'absolute', top: 6, right: 6, width: 22, height: 22, lineHeight: '20px',
            borderRadius: 6, border: '1px solid var(--border)', background: 'var(--cc-input-bg)',
            color: 'var(--text-muted)', cursor: 'pointer', fontSize: 13, padding: 0,
          }}
        ><MoreHorizontal size={13} className="lucide-inline" aria-hidden="true" /></button>
      )}
      {pack.id === DEFAULT_PACK ? (
        // Built-in ghost: bundled art, drawn like the live companion (see above).
        <BuiltinThumbnail size={80} colorMap={colorMap} />
      ) : thumbnailContent ? (
        // Custom packs bake their own eyes in and are never recoloured through the
        // built-in colour map, so no eye overlay or colour map here.
        <AnimThumbnail content={thumbnailContent} format={entryFormat(pack)} size={80} spriteConfig={spriteConfig} />
      ) : (
        // A pack whose idle art could not be read: a neutral, empty thumbnail box —
        // never an emoji stand-in.
        <div style={S.thumbnail} />
      )}
      <div style={S.cardName}>{pack.name}</div>
      <div style={S.cardAuthor}>{pack.author}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, minHeight: 20 }}>
        {isActive
          ? <span style={S.activeBadge}>{i18nT('apps.crewCompanion.gallery.active')}</span>
          : <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{i18nT('apps.crewCompanion.gallery.tapToUse')}</span>}
      </div>
    </div>
  )
}

// ── Import from PetDex dialog ──────────────────────────────────────────────

function ImportPetDialog({ input, onInput, resolving, resolved, importing, error, onConfirm, onClose, onBrowse }: {
  input: string
  onInput: (v: string) => void
  resolving: boolean
  resolved: { displayName: string; author: string; description: string; preview: string } | null
  importing: boolean
  error: string | null
  onConfirm: () => void
  onClose: () => void
  onBrowse: () => void
}) {
  // Esc closes, like any dialog
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div role="presentation" style={S.detailOverlay} onClick={onClose}>
      <div role="dialog" aria-modal="true" style={S.modal} onClick={(e) => e.stopPropagation()}>
        <div style={S.modalHeader}>
          <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>{i18nT('apps.crewCompanion.gallery.importPet')}</span>
          <button onClick={onClose} aria-label={i18nT('apps.crewCompanion.gallery.close')}
            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 15, padding: 0 }}>
            <X className="lucide-inline" aria-hidden="true" />
          </button>
        </div>

        <div style={{ padding: 16 }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.5, marginBottom: 12 }}>
            {/*
              One key, one sentence, split on a {{link}} placeholder — so a translator
              can put the link wherever their language needs it.
            */}
            {splitOnPlaceholder(i18nT('apps.crewCompanion.gallery.petdexIntro'), 'link').map((part, i) =>
              part === null ? (
                /*
                 * A real <button>, not a click-handling span. This sits mid-sentence,
                 * so it has to stay in the text flow -- but a span takes no focus and
                 * answers no key, which left PetDex browsing unreachable without a
                 * mouse. A button is focusable and Enter/Space-activatable natively,
                 * and stripping its chrome keeps it looking like the link it reads as.
                 */
                <button
                  key="link"
                  type="button"
                  onClick={onBrowse}
                  style={{
                    display: 'inline', padding: 0, border: 'none', background: 'none',
                    font: 'inherit', color: 'var(--accent)', cursor: 'pointer',
                    textDecoration: 'underline',
                  }}
                >
                  {i18nT('apps.crewCompanion.gallery.petdexLinkLabel')}
                </button>
              ) : (
                <span key={i}>{part}</span>
              ),
            )}
          </div>

          <input
            value={input}
            onChange={(e) => onInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && resolved) onConfirm() }}
            placeholder={i18nT('apps.crewCompanion.gallery.petdexPlaceholder')}
            autoFocus
            disabled={importing}
            style={{
              width: '100%', boxSizing: 'border-box', background: 'var(--cc-input-bg)',
              border: `1px solid ${error ? 'var(--danger)' : 'var(--border)'}`, borderRadius: 8,
              color: 'var(--text)', fontSize: 12, padding: '9px 11px', outline: 'none',
            }}
          />

          {/* Status: looking up / found / not found */}
          <div style={{ minHeight: 58, marginTop: 10 }}>
            {resolving && (
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{i18nT('apps.crewCompanion.gallery.lookingUp')}</div>
            )}
            {!resolving && error && (
              <div style={{ fontSize: 11, color: 'var(--danger)' }}>{error}</div>
            )}
            {!resolving && !error && resolved && (
              <div style={{
                border: '1px solid var(--border)', borderRadius: 10, padding: 10,
                display: 'flex', alignItems: 'center', gap: 10, background: 'var(--cc-input-bg)',
              }}>
                {resolved.preview
                  ? <img src={resolved.preview} alt="" style={{ width: 44, height: 44, objectFit: 'contain', imageRendering: 'pixelated', borderRadius: 8, background: 'var(--bg)' }} />
                  : <div style={{ width: 44, height: 44, borderRadius: 8, background: 'var(--bg)' }} />}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 600 }}>{resolved.displayName}</div>
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {i18nT('apps.crewCompanion.gallery.foundBy', { author: resolved.author })}
                  </div>
                </div>
              </div>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
            <span style={{ flex: 1, fontSize: 10, color: 'var(--text-faint)' }}>{i18nT('apps.crewCompanion.gallery.fanArt')}</span>
            <button style={S.headerBtn} onClick={onClose}>{i18nT('apps.crewCompanion.gallery.cancel')}</button>
            <button
              style={{ ...S.headerBtnAccent, opacity: !resolved || importing ? 0.5 : 1, cursor: !resolved || importing ? 'default' : 'pointer' }}
              disabled={!resolved || importing}
              onClick={onConfirm}
            >{importing ? i18nT('apps.crewCompanion.gallery.adding') : i18nT('apps.crewCompanion.gallery.usePet')}</button>
          </div>
        </div>
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
  i18nT: (key: string, vars?: Record<string, string>) => string
  colorMap?: ColorMap | null
  lang?: string
}) {
  const { meta, animations } = detail
  const sc = detail.sprite
  const [showColorCustomizer, setShowColorCustomizer] = useState(false)
  const isDefaultKiro = meta.id === DEFAULT_PACK
  const thumbColorMap = isDefaultKiro ? colorMap : null

  // Dress-up selection, read from and written back to config so the pet overlay
  // (a separate window) picks the change up via the config:updated broadcast.
  const [accessory, setAccessory] = useState<GhostAccessory>('none')
  useEffect(() => {
    if (!isDefaultKiro) return
    api?.getCrewCompanionConfig?.().then((c) => {
      // Nested, matching what this panel WRITES two hundred lines below and what
      // the overlay reads. Reading a flat `accessory` here meant the picker always
      // opened on "none" however the ghost was actually dressed — the write went
      // one place and the read looked in another.
      const worn = (c as { kiro?: { accessory?: unknown } })?.kiro?.accessory
      if (typeof worn === 'string') setAccessory(worn as GhostAccessory)
    }).catch(() => {})
  }, [isDefaultKiro])

  const stateEntries = REQUIRED_STATES.map((s) => ({
    key: s,
    label: slotLabel(s),
    anim: animations[s],
    required: true,
  }))

  // Same categories as the editor (shared/appearanceTypes): what a pack SHOWS has to
  // match what the editor asks you to draw. This view previously listed every
  // optional state plus a separate "Mood animations" grid, neither of which lined up
  // with the editor's Status / Random sections.
  const label = (k: string) => slotLabel(k)
  const statusEntries = STATUS_STATES
    .filter((s) => animations[s])
    .map((s) => ({ key: s, label: label(s), anim: animations[s], required: false }))

  const randomEntries = [...RANDOM_STATES, ...ALL_MOODS]
    .filter((s) => animations[s])
    .map((s) => ({ key: s, label: label(s), anim: animations[s], required: false }))

  const legacyEntries = LEGACY_STATES
    .filter((s) => animations[s])
    .map((s) => ({ key: s, label: label(s), anim: animations[s], required: false }))



  return (
    <div role="presentation" style={S.detailOverlay} onClick={onClose}>
      <div role="dialog" aria-modal="true" style={S.detailPanel} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={S.detailHeader}>
          <div style={{ flex: 1 }}>
            <div style={S.detailTitle}>{meta.name}</div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              {meta.author} · {entryFormat(meta).toUpperCase()}
            </div>
            {meta.description && (
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4, fontStyle: 'italic' }}>
                {meta.description}
              </div>
            )}
          </div>
          <span style={S.badge(meta.type)}>
            {meta.type === 'built-in' ? i18nT('apps.crewCompanion.gallery.builtIn') : i18nT('apps.crewCompanion.gallery.custom')}
          </span>
          {isActive && <span style={S.activeBadge}>{i18nT('apps.crewCompanion.gallery.active')}</span>}
          <button style={S.closeBtn} onClick={onClose}
            aria-label={i18nT('apps.crewCompanion.gallery.close')}>
            <X className="lucide-inline" aria-hidden="true" />
          </button>
        </div>

        {/* Color customize toggle — pill button below header */}
        {isDefaultKiro && (
          <button
            onClick={() => setShowColorCustomizer(!showColorCustomizer)}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              margin: '0 0 12px', padding: '6px 14px',
              borderRadius: 20, border: showColorCustomizer ? '1px solid var(--accent)' : '1px solid var(--border)',
              background: showColorCustomizer ? 'var(--accent-glow)' : 'var(--cc-input-bg)',
              color: showColorCustomizer ? 'var(--accent)' : 'var(--text)',
              cursor: 'pointer', fontSize: 12, fontWeight: 500,
              transition: 'all 200ms ease',
            }}
          >
            <span style={{ transition: 'transform 200ms', transform: showColorCustomizer ? 'rotate(180deg)' : 'none', display: 'inline-flex' }}><ChevronDown size={13} className="lucide-inline" aria-hidden="true" /></span>
            {showColorCustomizer ? i18nT('apps.crewCompanion.color.hideBtn') : i18nT('apps.crewCompanion.color.customizeBtn')}
          </button>
        )}

        {/* Color customizer panel with slide animation */}
        {isDefaultKiro && animations.idle && (
          <div style={{
            maxHeight: showColorCustomizer ? 2000 : 0,
            opacity: showColorCustomizer ? 1 : 0,
            overflow: 'hidden',
            transition: 'max-height 350ms ease, opacity 250ms ease',
          }}>
            <ColorCustomizerPanel idleSvgContent={entryContent(animations.idle)} />
          </div>
        )}

        {/* Dress up — moved here from the pet's click menu. Only the default
            ghost has props; custom packs bring their own art. */}
        {isDefaultKiro && (
          <>
            <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.gallery.dressUp')}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
              {GHOST_ACCESSORIES.map((acc) => {
                const active = accessory === acc.id
                return (
                  <button
                    key={acc.id}
                    onClick={() => {
                      setAccessory(acc.id)
                      api?.updateConfig?.({ kiro: { accessory: acc.id } })
                    }}
                    style={{
                      font: '600 12px -apple-system, BlinkMacSystemFont, sans-serif',
                      padding: '6px 12px', borderRadius: 8, cursor: 'pointer',
                      border: `1.5px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                      background: active ? 'var(--accent-subtle, var(--bg-hover))' : 'transparent',
                      color: 'var(--text)',
                    }}
                  >{i18nT(acc.labelKey)}</button>
                )
              })}
            </div>
          </>
        )}

        {/* Required */}
        <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.gallery.states')}</div>
        <div style={S.animGrid}>
          {stateEntries.map(({ key, label, anim }) => (
            <div key={key} style={S.animCell}>
              {anim ? (
                <AnimThumbnail content={entryContent(anim)} format={entryFormat(anim)} spriteConfig={sc} colorMap={thumbColorMap}
                  eyePose={isDefaultKiro ? ghostPoseForKey(key) : undefined} />
              ) : (
                <div style={{ ...S.animThumb, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-faint)', fontSize: 11 }}>—</div>
              )}
              <div style={S.animLabel}>{label}</div>
            </div>
          ))}
        </div>

        {/* Status / Random / Legacy — only the groups this pack actually provides */}
        {([[i18nT('apps.crewCompanion.state.groupStatus'), statusEntries], [i18nT('apps.crewCompanion.state.groupRandom'), randomEntries], [i18nT('apps.crewCompanion.state.groupLegacy'), legacyEntries]] as const)
          .filter(([, entries]) => entries.length > 0)
          .map(([title, entries]) => (
          <React.Fragment key={title}>
            <div style={S.sectionLabel}>{title}</div>
            <div style={S.animGrid}>
              {entries.map(({ key, label, anim }) => (
                <div key={key} style={S.animCell}>
                  {anim ? (
                    <AnimThumbnail content={entryContent(anim)} format={entryFormat(anim)} spriteConfig={sc} colorMap={thumbColorMap}
                  eyePose={isDefaultKiro ? ghostPoseForKey(key) : undefined} />
                  ) : (
                    <div style={{ ...S.animThumb, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-faint)', fontSize: 11 }}>—</div>
                  )}
                  <div style={S.animLabel}>{label}</div>
                </div>
              ))}
            </div>
          </React.Fragment>
        ))}

        {/* Action buttons */}
        <div style={S.btnRow}>
          {!isActive && (
            <button style={S.applyBtn} onClick={onApply}>
              {i18nT('apps.crewCompanion.gallery.apply')}
            </button>
          )}
          {meta.type === 'custom' && (
            <>
              <button style={S.actionBtn} onClick={onExport}>
                {i18nT('apps.crewCompanion.gallery.export')}
              </button>
              <button style={S.actionBtn} onClick={onEdit}>
                {i18nT('apps.crewCompanion.gallery.edit')}
              </button>
              <button style={S.dangerBtn} onClick={onDelete}>
                {i18nT('apps.crewCompanion.gallery.delete')}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}


// ── Main Gallery Panel ─────────────────────────────────────────────────────

/**
 * The avatar gallery — its own window (renderer/galleryEntry.tsx).
 *
 * It was briefly embedded inside Settings. Reverted: picking an avatar is a
 * preference, but importing and authoring packs are creation flows, and squeezing a
 * multi-step editor into a 420px preferences column meant shrinking the list and
 * floating the editor over the panel. The window is 800px because that's what these
 * tools need.
 */
export const GalleryPanel: React.FC = () => {
  const [packs, setPacks] = useState<PackMeta[]>([])
  const [activePackId, setActivePackId] = useState<string>('')
  const [selectedPackId, setSelectedPackId] = useState<string | null>(null)
  const [detail, setDetail] = useState<PackDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [petdexInput, setPetdexInput] = useState('')
  const [importing, setImporting] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [resolved, setResolved] = useState<{ slug: string; displayName: string; author: string; description: string; spriteBase64: string; preview: string } | null>(null)
  const [importError, setImportError] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('gallery')
  const [editingPack, setEditingPack] = useState<PackMeta | undefined>(undefined)
  const [lang, setLang] = useState('en')
  // Thumbnail content cache: packId → SVG/Lottie content for the thumbnail
  const [thumbs, setThumbs] = useState<Record<string, string>>({})
  const [spriteConfigs, setSpriteConfigs] = useState<Record<string, SpriteMeta>>({})
  // Color map for the built-in ghost's thumbnail
  const [crewCompanionColorMap, setCrewCompanionColorMap] = useState<ColorMap | null>(null)
  // ── Data fetching ──────────────────────────────────────────────────────

  const fetchPacks = useCallback(async () => {
    try {
      const list: PackMeta[] = await api.galleryListPacks()
      setPacks(list)

      // Fetch thumbnail content for each pack via get-pack-detail
      // (the detail response includes animation content keyed by state name)
      const thumbMap: Record<string, string> = {}
      const scMap: Record<string, SpriteMeta> = {}
      for (const p of list) {
        try {
          const d = await api.galleryGetPackDetail(p.id)
          if (d?.animations?.idle) {
            thumbMap[p.id] = entryContent(d.animations.idle)
          }
          if (d?.sprite) scMap[p.id] = d.sprite
        } catch {}
      }
      setThumbs(thumbMap)
      setSpriteConfigs(scMap)
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.gallery.loadPacksFailed'))
    }
  }, [])

  const fetchActiveId = useCallback(async () => {
    try {
      const cfg = await api.getCrewCompanionConfig()
      setActivePackId(cfg?.activeAppearance || DEFAULT_PACK)
      if (typeof cfg?.language === 'string') setLang(cfg.language)
      // Load colorMap for the built-in ghost's thumbnail. Keyed by the real pack id
      // (kiro-ghost); the old `'default-kiro'` matched no pack, so the detail lookup
      // behind this returned 400 on every gallery open.
      const cm = await api?.presetsGetColorMap?.(DEFAULT_PACK)
      setCrewCompanionColorMap(cm && Object.keys(cm).length > 0 ? cm : null)
    } catch {
      setActivePackId(DEFAULT_PACK)
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
    const offActive = api?.onGalleryActiveChanged?.((data?: { packId?: string }) => {
      if (data?.packId) {
        setActivePackId(data.packId)
      } else {
        fetchActiveId()
      }
    })
    const offPacks = api?.onGalleryPacksChanged?.(() => {
      fetchPacks()
    })
    // Listen for color map changes to update the built-in ghost's thumbnail
    const offColor = api.onColorMapChanged?.((data: { packId: string; colorMap: Record<string, string> }) => {
      if (data.packId === DEFAULT_PACK) {
        setCrewCompanionColorMap(data.colorMap && Object.keys(data.colorMap).length > 0 ? data.colorMap : null)
      }
    })
    // Listen for config changes (language, theme) broadcast from settings save
    const offConfig = api.onConfigUpdated?.((kiro?: { language?: string }) => {
      if (kiro?.language && kiro.language !== lang) setLang(kiro.language)
    })
    return () => {
      offActive?.()
      offPacks?.()
      offColor?.()
      offConfig?.()
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
      if (!d) { setDetail(null); return }
      setDetail({ meta: d.meta as PackMeta, animations: d.animations ?? {}, sprite: d.sprite })
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.gallery.loadDetailFailed'))
      setDetail(null)
    }
  }, [selectedPackId])

  // ── Actions ────────────────────────────────────────────────────────────

  const handleApply = useCallback(async () => {
    if (!detail) return
    try {
      const result = await api.gallerySetActive(detail.meta.id)
      if (result && !result.ok) {
        setError(result.error || i18nT('apps.crewCompanion.gallery.applyFailed'))
        return
      }
      setActivePackId(detail.meta.id)
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.gallery.applyFailed'))
    }
  }, [detail])

  const [toast, setToast] = useState<string | null>(null)
  const [toastFading, setToastFading] = useState(false)
  // Sprite-save refusals render INSIDE the still-open importer (the gallery's
  // own error banner is unreachable while mode==='sprite').
  const [spriteSaveError, setSpriteSaveError] = useState<string | null>(null)

  const showToast = (msg: string) => {
    setToast(msg); setToastFading(false)
    setTimeout(() => setToastFading(true), 2000)
    setTimeout(() => { setToast(null); setToastFading(false) }, 2500)
  }

  const handleExport = useCallback(async () => {
    if (!detail) return
    try {
      const result = (await api.galleryExport(detail.meta.id)) as GalleryResult | null
      if (!result) return // cancelled
      if (result.ok) {
        showToast(i18nT('apps.crewCompanion.gallery.exportSuccess'))
      } else {
        setError(result.error || i18nT('apps.crewCompanion.gallery.exportFailed'))
      }
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.gallery.exportFailed'))
    }
  }, [detail])

  const handleEdit = useCallback(() => {
    if (!detail) return
    setEditingPack(detail.meta)
    setSelectedPackId(null)
    setDetail(null)
    setMode(entryFormat(detail.meta) === 'sprite' ? 'sprite' : 'editor')
  }, [detail])

  const handleDelete = useCallback(async () => {
    if (!detail) return
    if (!confirm(i18nT('apps.crewCompanion.gallery.deleteConfirm', { name: detail.meta.name }))) return
    try {
      const wasActive = detail.meta.id === activePackId
      if (wasActive) {
        // Repair the reference BEFORE the delete, not after: with the delete
        // first, a failed reset or a gateway restart in between left the
        // persisted config pointing at a pack that no longer exists. Switching
        // first is safe in every failure order — if the delete then fails, the
        // active pack is the built-in and the doomed pack simply still exists.
        // gallerySetActive also broadcasts the change to the overlay window.
        const switched = await api.gallerySetActive?.(DEFAULT_PACK)
        if (!switched?.ok) {
          setError(i18nT('apps.crewCompanion.gallery.deleteFailed'))
          return
        }
        setActivePackId(DEFAULT_PACK)
      }
      const result = await api.galleryDelete(detail.meta.id)
      if (!result) {
        setError(i18nT('apps.crewCompanion.gallery.deleteFailed'))
        return
      }
      setSelectedPackId(null)
      setDetail(null)
      fetchPacks()
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.gallery.deleteFailed'))
    }
  }, [detail, fetchPacks, activePackId])

  const handleCreateNew = useCallback(() => {
    setEditingPack(undefined)
    setMode('editor')
  }, [])

  // ── Apply a pack directly from its grid card ─────────────────────────────
  const applyPack = useCallback(async (packId: string) => {
    if (packId === activePackId) return
    setError(null)
    try {
      const result = await api.gallerySetActive(packId)
      if (result && !result.ok) { setError(result.error || i18nT('apps.crewCompanion.gallery.applyFailed')); return }
      setActivePackId(packId)
      showToast(i18nT('apps.crewCompanion.gallery.switched'))
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.gallery.applyFailed'))
    }
  }, [activePackId])

  // ── Import from PetDex: resolve (look it up) then confirm (save + apply) ──
  const closeImport = useCallback(() => {
    setShowImport(false); setPetdexInput(''); setResolved(null); setImportError(null); setResolving(false)
  }, [])

  const resolvePetdex = useCallback(async (raw: string) => {
    const input = raw.trim()
    setImportError(null)
    setResolved(null)
    if (input.length < 2) return
    setResolving(true)
    try {
      const res = (await api?.petdexFetch?.(input)) as unknown as PetdexResult | undefined
      if (!res?.ok) { setImportError(res?.error || i18nT('apps.crewCompanion.gallery.petNotFound')); return }
      let preview = ''
      try { preview = await firstFramePreview(res.spriteBase64) } catch { /* preview optional */ }
      setResolved({
        slug: res.slug, displayName: res.displayName, author: res.author,
        description: res.description, spriteBase64: res.spriteBase64, preview,
      })
    } catch (err) {
      setImportError(errorText(err) || i18nT('apps.crewCompanion.gallery.lookupFailed'))
    } finally {
      setResolving(false)
    }
  }, [])

  // Look up shortly after typing/pasting stops, so pasting a link just works.
  // A frameless window has no OS close button, so Escape must work — the same
  // affordance the panel provides.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') api?.closeGallery?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (!showImport) return
    const v = petdexInput.trim()
    if (v.length < 2) { setResolved(null); return }
    const t = setTimeout(() => { void resolvePetdex(v) }, 450)
    return () => clearTimeout(t)
  }, [petdexInput, showImport, resolvePetdex])

  const confirmImport = useCallback(async () => {
    if (!resolved || importing) return
    setImporting(true)
    setImportError(null)
    try {
      const data = await buildSpritePackData(resolved.spriteBase64, {
        displayName: resolved.displayName, author: resolved.author, description: resolved.description,
      })
      const save = await api?.gallerySaveSpritePack?.(data)
      if (!save?.ok) { setImportError(save?.error || i18nT('apps.crewCompanion.gallery.savePetFailed')); return }
      if (save.packId) { api?.gallerySetActive?.(save.packId); setActivePackId(save.packId) }
      showToast(i18nT('apps.crewCompanion.gallery.nowYourPet', { name: resolved.displayName }))
      closeImport()
      fetchPacks()
    } catch (err) {
      setImportError(errorText(err) || i18nT('apps.crewCompanion.gallery.importFailed'))
    } finally {
      setImporting(false)
    }
  }, [resolved, importing, fetchPacks, closeImport])


  // ── Editor mode → render PackEditor ──────────────────────────────────────

  // An overlay rather than a replacement: the gallery stays mounted behind it, the
  // card keeps its rounded silhouette, and the editor carries its own back control
  // and drag region (the gallery header's drag region is covered while it is open).
  const closeEditor = () => { setMode('gallery'); setEditingPack(undefined) }
  const editorOverlay = mode === 'editor' ? (
    <div style={S.detailOverlay}>
      <div style={S.editorModal}>
        <PackEditor
          existingPack={editingPack}
          onSave={() => {
            showToast(i18nT(editingPack ? 'apps.crewCompanion.editor.saveSuccess' : 'apps.crewCompanion.editor.createSuccess'))
            closeEditor()
            fetchPacks()
          }}
          onCancel={closeEditor}
        />
      </div>
    </div>
  ) : null

  if (mode === 'sprite') {
    return (
      <SpriteImporter
        existingPack={editingPack}
        onDone={async (result: SpriteImportResult) => {
          // Save sprite pack via IPC
          const res = await api.gallerySaveSpritePack(result)
          if (res?.ok) {
            showToast(i18nT(editingPack ? 'apps.crewCompanion.editor.saveSuccess' : 'apps.crewCompanion.editor.createSuccess'))
            // If overwriting the active pack, re-apply it
            const packId = result.overwriteId || res.packId
            if (packId && packId === activePackId) {
              api?.gallerySetActive?.(packId)
            }
            fetchPacks()
            // Leave the editor ONLY on success. Unmounting on a refusal threw
            // away the user's unsaved sprite edits and hid the error (the
            // gallery's error banner doesn't render in sprite mode).
            setSpriteSaveError(null)
            setMode('gallery')
          } else {
            setSpriteSaveError(res?.error || i18nT('apps.crewCompanion.gallery.spritePackFailed'))
          }
        }}
        saveError={spriteSaveError}
        onCancel={() => { setSpriteSaveError(null); setMode('gallery') }}
      />
    )
  }

  // ── Gallery view ───────────────────────────────────────────────────────

  return (
    <div style={S.root}>
      {/* Header — title + the two ways to add an avatar */}
      <div style={S.header}>
        <span style={S.title}>{i18nT('apps.crewCompanion.gallery.title')}</span>
        <button style={S.headerBtnAccent} onClick={() => { setShowImport(true); setImportError(null) }}>
          {i18nT('apps.crewCompanion.gallery.importFromPetdex')}
        </button>
        <button style={S.headerBtn} onClick={handleCreateNew}>
          {i18nT('apps.crewCompanion.gallery.makeYourOwn')}
        </button>
        <button
          style={S.windowCloseBtn}
          onClick={() => api?.closeGallery?.()}
          title={i18nT('apps.crewCompanion.gallery.close')}
          aria-label={i18nT('apps.crewCompanion.gallery.close')}
        ><X size={14} aria-hidden="true" /></button>
      </div>

      {/* Body */}
      <div style={S.body}>
        {toast && (
          <div style={{ padding: '8px 14px', borderRadius: 8, background: 'rgba(76,175,80,0.15)', border: '1px solid rgba(76,175,80,0.3)', color: 'var(--text)', fontSize: 12, marginBottom: 8, animation: toastFading ? 'toastOut 0.5s forwards' : 'toastIn 0.3s ease-out' }}>{toast}</div>
        )}
        {error && (
          <div style={S.error}>
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              style={{ float: 'right', background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', fontSize: 14 }}
              aria-label={i18nT('apps.crewCompanion.panel.close')}
            ><X size={14} aria-hidden="true" /></button>
          </div>
        )}

        {loading ? (
          <div style={S.loading}>{i18nT('apps.crewCompanion.gallery.loading')}</div>
        ) : (
          <div style={S.grid}>
            {packs.map((pack) => (
              <PackCard
                key={pack.id}
                pack={pack}
                isActive={pack.id === activePackId}
                isSelected={pack.id === selectedPackId}
                onClick={() => applyPack(pack.id)}
                onManage={pack.type === 'custom' ? () => handleCardClick(pack.id) : undefined}
                thumbnailContent={thumbs[pack.id]}
                spriteConfig={spriteConfigs[pack.id]}
                i18nT={i18nT}
                colorMap={crewCompanionColorMap}
              />
            ))}
          </div>
        )}

        {/* Footer — quiet pointer to the PetDex gallery */}
        {!loading && (
          <div style={{ marginTop: 18, textAlign: 'center', fontSize: 12, color: 'var(--text-muted)' }}>
            {i18nT('apps.crewCompanion.gallery.moreAvatarsOn')}{' '}
            <button
              type="button"
              onClick={() => api?.openExternal?.('https://petdex.dev')}
              style={{
                display: 'inline', padding: 0, border: 'none', background: 'none',
                font: 'inherit', color: 'var(--accent)', cursor: 'pointer',
                textDecoration: 'underline',
              }}
            >petdex.dev</button>
          </div>
        )}
      </div>

      {/* Editor overlay — same treatment as the import dialog */}
      {editorOverlay}

      {/* Import popup */}
      {showImport && (
        <ImportPetDialog
          input={petdexInput}
          onInput={setPetdexInput}
          resolving={resolving}
          resolved={resolved}
          importing={importing}
          error={importError}
          onConfirm={confirmImport}
          onClose={closeImport}
          onBrowse={() => api?.openExternal?.('https://petdex.dev')}
        />
      )}

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
          i18nT={i18nT}
          colorMap={crewCompanionColorMap}
          lang={lang}
        />
      )}
    </div>
  )
}
