/**
 * The per-crew avatar builder — a nested dialog opened from the crew editor.
 *
 * Three identity tiers are offered: ICON (the primary tier new crews are built
 * with — a shipped ghost pose over a solid background), PICTURE (an uploaded,
 * client-cropped image), and LIBRARY (an appearance pack somebody else drew,
 * served per state by the gateway).
 *
 * The game-style ghost "捏脸" builder and its per-state reaction layer are no
 * longer AUTHORABLE here: a new or edited crew cannot pick or set a ghost face.
 * Crews that already wear one keep rendering it exactly as before (see
 * `CrewAvatar.tsx` and `kiroGhostAvatar.ts` — the data model, the readers and
 * the renderer are all untouched), and editing such a crew opens on the Icon
 * tier with its stored ghost preserved: it stays unchanged on Apply unless the
 * user actively picks a pose or colour here.
 *
 * ICON composition goes through `poseDataUri` from `avatarPoses`; PICTURE is
 * cropped and downscaled entirely client-side; LIBRARY lives in its own
 * component (`CrewAvatarLibraryTab`) which owns the listing, the import and the
 * delete. Each tier renders as an `<img>` carrying a data URI or a served URL —
 * no `dangerouslySetInnerHTML`, and a data URI is its own document so several on
 * one page cannot collide on internal ids.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
// `Image` is ALIASED deliberately: `cropToSquareDataUri` calls `new Image()`
// for the decode, and a bare import of the icon binds that identifier at
// module scope — the constructor would then build a React component and every
// picture upload would throw.
import { Image as ImageIcon, ImageUp, LayoutGrid, LibraryBig } from 'lucide-react'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { Btn } from './ui'
import SegmentedControl from './SegmentedControl'
import { useIsMobile } from '../hooks/useIsMobile'
import { BRAND_PURPLE, TILES } from '../lib/kiroGhostAvatar'
import CrewAvatar, { type CrewAvatarOverride } from './CrewAvatar'
import {
  DEFAULT_POSE_BG,
  POSE_BG_RE,
  POSE_IDS,
  poseDataUri,
  resolvePoseBg,
  seedIconPose,
} from '../lib/avatarPoses'
import CrewAvatarLibraryTab from './CrewAvatarLibraryTab'
import ErrorNotice from './ErrorNotice'

/**
 * Human color names for the icon background swatches, keyed by hex. Screen
 * readers get "Sky blue", not "#21a5de" (a raw hex is noise to a person).
 * Literal catalog keys, indexed — a key built at runtime is invisible to the
 * extractor and the dead-key gate. A hex missing here (a future palette entry)
 * falls back to the hex itself: still announceable, just not friendly.
 */
const TILE_LABEL_KEYS: Record<string, string> = {
  '#9046ff': 'components.avatarBuilder.tile_purple',
  '#de2121': 'components.avatarBuilder.tile_red',
  '#21de21': 'components.avatarBuilder.tile_green',
  '#21a5de': 'components.avatarBuilder.tile_sky',
  '#3d259d': 'components.avatarBuilder.tile_indigo',
  '#eeae4f': 'components.avatarBuilder.tile_amber',
  '#ee4fee': 'components.avatarBuilder.tile_magenta',
  '#259d85': 'components.avatarBuilder.tile_teal',
  '#9d2561': 'components.avatarBuilder.tile_plum',
  '#9d6725': 'components.avatarBuilder.tile_bronze',
  '#25679d': 'components.avatarBuilder.tile_steel',
  '#979d25': 'components.avatarBuilder.tile_olive',
  '#ee4f7e': 'components.avatarBuilder.tile_rose',
  '#21d4de': 'components.avatarBuilder.tile_cyan',
  '#ee7e4f': 'components.avatarBuilder.tile_coral',
}

/** ICON tier: the background colours offered as swatches. Brand purple first
 *  (the art's own shipped colour), then the ghost tile palette — a set already
 *  chosen for separation and for keeping a white silhouette legible (every
 *  entry is held below L* 78, see `kiroGhostAvatar`). Reusing it means the two
 *  tiers cannot drift apart on which colours read well behind the ghost. */
const ICON_BG_OPTIONS = [BRAND_PURPLE, ...TILES]

/**
 * The theme's own accent colour as `#rrggbb`, or null when it cannot be read as
 * one (SSR, jsdom, a theme whose `--accent` is a non-hex form). Read live off
 * the document so it tracks the ACTIVE theme — a crew created under a teal theme
 * starts teal, under purple starts purple — rather than a build-time constant.
 *
 * Only a `#rrggbb` is accepted: the value is interpolated into SVG markup
 * through `poseDataUri`, and `--accent` is authored as a hex in every shipped
 * theme (see `index.css`), so a non-hex reading means "cannot seed from theme"
 * rather than something to coerce.
 */
function themeAccentHex(): string | null {
  if (typeof window === 'undefined' || typeof getComputedStyle !== 'function') return null
  try {
    const raw = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()
    return POSE_BG_RE.test(raw) ? raw.toLowerCase() : null
  } catch {
    return null
  }
}

/** The background a fresh icon draft starts on: the active theme's accent when
 *  it reads as a hex, else the art's shipped purple. Seeding from the theme is
 *  the point — the first thing the user sees already matches their dashboard. */
function seedIconBg(): string {
  return themeAccentHex() ?? DEFAULT_POSE_BG
}

/** Longest source file the picker accepts BEFORE crop/downscale. Generous —
 *  the output is re-encoded regardless — but bounds the decode of a
 *  mis-picked 200MB TIFF-in-a-.png. */
const MAX_SOURCE_BYTES = 20 * 1024 * 1024
/** Output edge — matches the design spec and keeps the upload tens of KB. */
const OUTPUT_PX = 512
/** Client-side output budget, under the server's 1 MB cap with headroom. */
const MAX_OUTPUT_BYTES = 900 * 1024

/** Approximate byte size of a data URI's payload (base64 → bytes). */
const dataUriBytes = (uri: string) => Math.floor(((uri.length - uri.indexOf(',') - 1) * 3) / 4)

/**
 * Center-crop to square and downscale, returning a data URI within the
 * upload budget. PNG first (lossless, keeps transparency); a high-entropy
 * photo whose 512px PNG overflows the budget falls back to JPEG on a white
 * ground, then to a smaller JPEG — so any decodable pick yields something
 * the server will accept. Runs entirely client-side: the server only ever
 * sees the small square the user previewed, never the original photo.
 */
async function cropToSquareDataUri(file: File): Promise<string> {
  const url = URL.createObjectURL(file)
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const i = new Image()
      i.onload = () => resolve(i)
      i.onerror = () => reject(new Error('undecodable image'))
      // `i` is an HTMLImageElement (decode-only, cannot execute script) and
      // `url` is a same-origin blob: URL minted two lines up from the user's
      // own file pick — no externally controlled input reaches the sink.
      // nosemgrep: semgrep.kirocrew.frontend-external-script-inject
      i.src = url
    })
    const side = Math.min(img.naturalWidth, img.naturalHeight)
    if (!side) throw new Error('empty image')
    const draw = (out: number, jpegGround: boolean) => {
      const canvas = document.createElement('canvas')
      canvas.width = out
      canvas.height = out
      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('no canvas context')
      if (jpegGround) {
        // JPEG has no alpha channel; without a ground, transparent source
        // pixels encode as black.
        ctx.fillStyle = '#ffffff'
        ctx.fillRect(0, 0, out, out)
      }
      ctx.drawImage(
        img,
        (img.naturalWidth - side) / 2,
        (img.naturalHeight - side) / 2,
        side,
        side,
        0,
        0,
        out,
        out,
      )
      return canvas
    }
    const px = Math.min(side, OUTPUT_PX)
    const png = draw(px, false).toDataURL('image/png')
    if (dataUriBytes(png) <= MAX_OUTPUT_BYTES) return png
    const jpeg = draw(px, true).toDataURL('image/jpeg', 0.85)
    if (dataUriBytes(jpeg) <= MAX_OUTPUT_BYTES) return jpeg
    return draw(Math.min(side, 384), true).toDataURL('image/jpeg', 0.8)
  } finally {
    URL.revokeObjectURL(url)
  }
}

/** The identity tier: a shipped icon pose, an uploaded picture, or an
 *  appearance pack. `icon` is the primary tier new crews are built with. A crew
 *  already wearing a hand-built ghost face is no longer authorable here, so it
 *  opens on `icon` and keeps its stored face unless the user picks a pose. */
type Tier = 'icon' | 'picture' | 'pack'

/** The tier a stored override selects when the dialog opens.
 *
 * A crew with NO override (a brand-new crew, or one still on its name-derived
 * face), and a crew that pinned a ghost face, both open on `icon` — the ghost
 * builder is gone, so there is nowhere else for a ghost record to land. An
 * uploaded picture opens on `picture`, a worn pack on `pack`. */
const tierOf = (value: CrewAvatarOverride | null): Tier =>
  value?.kind === 'image'
    ? 'picture'
    : value?.kind === 'pack'
      ? 'pack'
      : 'icon'

export default function CrewAvatarBuilder({
  open,
  name,
  value,
  onCancel,
  onSave,
}: {
  open: boolean
  /** Crew name — the dialog subtitle and the seed of the default icon pose. */
  name: string
  /** The pinned override currently held by the editor, or null for default. */
  value: CrewAvatarOverride | null
  onCancel: () => void
  /** null = reset to the name-derived default. */
  onSave: (next: CrewAvatarOverride | null) => void
}) {
  const { t } = useTranslation()
  // Drives the tier strip's compact form on narrow widths.
  const isMobile = useIsMobile()
  const [tier, setTier] = useState<Tier>(tierOf(value))
  /** The pack the draft wears, or null. A pack override is nothing BUT this id,
   *  so the Library pane needs no draft of its own. */
  const [packId, setPackId] = useState<string | null>(value?.kind === 'pack' ? value.id : null)
  /** The ICON draft: which pose, and the background colour behind it. Seeded
   *  from the stored icon record when editing one, else the default pose on a
   *  theme-derived background — so a fresh crew opens on a face that already
   *  matches the dashboard. Held even while another tier is selected, so a trip
   *  through another tab never discards it. */
  const [iconPose, setIconPose] = useState<string>(value?.kind === 'icon' ? value.pose : seedIconPose(name))
  const [iconBg, setIconBg] = useState<string>(
    value?.kind === 'icon' ? resolvePoseBg(value.bg) : seedIconBg(),
  )
  /**
   * Did the user actually TOUCH the icon controls this opening?
   *
   * It gates whether an Apply on the Icon tier commits a NEW icon override or
   * preserves whatever the crew already wore. A crew that stored a hand-built
   * ghost face opens here (the ghost builder is gone), and the stored ghost must
   * stay exactly as it was unless the user actively picks a pose or colour — so
   * an untouched Apply hands the stored record straight back. False at every
   * (re)open; a pose or background pick sets it, Reset clears it again.
   */
  const [iconTouched, setIconTouched] = useState(false)
  /** The cropped-and-scaled picture chosen THIS opening (data URI), not yet
   *  uploaded — upload happens on the editor's Save, keeping Apply free of
   *  side effects for pictures exactly as it is for traits. */
  const [pending, setPending] = useState<string | null>(
    value?.kind === 'image' ? (value.pendingData ?? null) : null,
  )
  const [pickError, setPickError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  /** An explicit Reset — "everything back to the name-derived default" — which
   *  Apply turns into a `null` override regardless of the tier showing. Cleared
   *  by any pick that states an intent (a pose, a colour, a picture, a pack). */
  const [reset, setReset] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  /** Monotonic pick generation: only the LATEST pick (of this dialog
   *  opening) may land its decode result, so a slow decode of pick A cannot
   *  overwrite a faster pick B, and a decode outliving a closed dialog
   *  cannot resurrect into the next opening. */
  const pickGen = useRef(0)
  // Re-arm when the dialog (re)opens: it stays mounted while closed (Radix
  // layer-stack requirement, see WorkspaceModal), so state must not leak from
  // the previous opening.
  useEffect(() => {
    if (open) {
      setTier(tierOf(value))
      setPackId(value?.kind === 'pack' ? value.id : null)
      setIconPose(value?.kind === 'icon' ? value.pose : seedIconPose(name))
      setIconBg(value?.kind === 'icon' ? resolvePoseBg(value.bg) : seedIconBg())
      setIconTouched(false)
      setPending(value?.kind === 'image' ? (value.pendingData ?? null) : null)
      setPickError('')
      setDragOver(false)
      setReset(false)
      pickGen.current += 1
    }
  }, [open, value, name])

  // While the Picture pane is showing, a file dropped ANYWHERE but the small
  // dashed zone (the preview image above it, the dialog body, the page) would
  // take the browser's default — navigate to the file — and unmount the whole
  // editor with every unsaved crew edit in it. Cancel the default at the
  // window for the pane's lifetime; the drop zone's own handlers still run
  // first (bubbling) and stop propagation is not needed because both listen
  // for the same default-cancelling outcome.
  useEffect(() => {
    if (!open || tier !== 'picture') return
    const swallow = (e: DragEvent) => { e.preventDefault() }
    window.addEventListener('dragover', swallow)
    window.addEventListener('drop', swallow)
    return () => {
      window.removeEventListener('dragover', swallow)
      window.removeEventListener('drop', swallow)
    }
  }, [open, tier])

  const pickIconPose = (pose: string) => { setIconPose(pose); setIconTouched(true); setReset(false) }
  const pickIconBg = (bg: string) => { setIconBg(bg); setIconTouched(true); setReset(false) }

  const pickFile = async (file: File | undefined | null) => {
    setPickError('')
    // Invalidate any in-flight decode FIRST: a rejected pick must not leave
    // an older slow decode able to complete and install itself as pending.
    const gen = ++pickGen.current
    if (!file) return
    if (file.size > MAX_SOURCE_BYTES) {
      setPickError(t('components.avatarBuilder.upload_too_large'))
      return
    }
    try {
      const uri = await cropToSquareDataUri(file)
      if (gen === pickGen.current) { setPending(uri); setReset(false) }
    } catch {
      if (gen === pickGen.current) setPickError(t('components.avatarBuilder.upload_bad_image'))
    }
  }

  /** What Apply hands the editor for the picture tier: a fresh pick carries
   *  its data URI (uploaded on the editor's Save); reopening over an already
   *  saved picture with no new pick keeps the stored value verbatim. */
  const pictureResult: CrewAvatarOverride | null = pending
    ? { kind: 'image', pendingData: pending }
    : value?.kind === 'image'
      ? value
      : null

  const applyDisabled =
    !reset && ((tier === 'picture' && pictureResult === null) || (tier === 'pack' && packId === null))

  /** True when the Icon tier is showing a crew that stored a HAND-BUILT GHOST
   *  face and the user has not picked a pose or colour yet. In that state an
   *  untouched Apply keeps the stored ghost (see `buildResult`), so the preview
   *  must show that ghost — not a name-seeded icon pose the save would not
   *  produce — and say that the current face is kept until a pick replaces it.
   *  A Reset clears it (the preview then shows the default icon). */
  const showStoredGhost = tier === 'icon' && !reset && !iconTouched && value?.kind === 'ghost'

  /**
   * The override Apply commits.
   *
   * An explicit Reset always wins — a `null` override, the reset the backend
   * honours on every tier. On the ICON tier: a stored GHOST face is preserved
   * on an UNTOUCHED Apply (returned verbatim, traits and reactions intact), so
   * merely opening the editor on a ghost crew and saving never rewrites its
   * hand-built face — only an active pose/colour pick replaces it. A new crew
   * (no override) or an existing icon crew commits the icon draft, which is the
   * name-seeded SVG + colour default new mates are built with. Picture and pack
   * tiers commit their own result.
   */
  const buildResult = (): CrewAvatarOverride | null => {
    if (reset) return null
    if (tier === 'icon') {
      // A stored hand-built ghost stays exactly as it was until the user picks a
      // pose or colour here — the ghost builder is gone, so an untouched Apply
      // must not silently convert the crew to an icon. Every other case (a brand
      // new crew with no override, an existing icon crew) commits the icon draft:
      // that is how a new mate defaults to the SVG + colour tier.
      if (!iconTouched && value?.kind === 'ghost') return value
      // `bg` is normalized through the same resolver the record reader uses, so
      // a draft and its reload compare equal. No reactions ride on an icon.
      return { kind: 'icon', pose: iconPose, bg: resolvePoseBg(iconBg) }
    }
    if (tier === 'pack') {
      // A pack override is the id and nothing else: the art is the library's, so
      // there is no draft to merge and no stored field to preserve. Apply is
      // disabled until an id is picked, so the guard is a type narrowing rather
      // than a reachable branch.
      if (!packId) return null
      return { kind: 'pack', id: packId }
    }
    return pictureResult
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onCancel() }}>
      {/* z-[110]: same stacking reason as WorkspaceModal — the editor's own
          content sits at z-[101], and an equal z-index would render this
          behind its opener. */}
      <DialogContent maxWidth={760} className="z-[110]" aria-label={t('components.avatarBuilder.title')}>
        <DialogHeader>
          <DialogTitle>{t('components.avatarBuilder.title_named', { name })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          {/* Tier switch: shipped icon pose, uploaded picture, or appearance
              pack. Above every pane so switching never loses any side's
              in-progress state — the icon draft, the pending picture and the
              pack id live in separate state. */}
          {/* overflow-x-auto is the floor under `compact` below: compact clears
              every shipped locale at 320px, but the strip must stay reachable
              rather than clipped if a longer one ever lands. It shows no
              scrollbar while nothing overflows, which is every desktop width. */}
          <div className="mb-3 overflow-x-auto" data-testid="avatar-builder-tabs">
            <SegmentedControl
              segments={[
                {
                  key: 'icon',
                  label: t('components.avatarBuilder.mode_icon'),
                  icon: <LayoutGrid size={13} aria-hidden="true" />,
                },
                {
                  key: 'picture',
                  label: t('components.avatarBuilder.mode_picture'),
                  icon: <ImageIcon size={13} aria-hidden="true" />,
                },
                {
                  key: 'pack',
                  label: t('components.avatarBuilder.mode_library'),
                  // A shelf, not a garment: the strip goes icon-only at phone
                  // width, so the icon has to say the same word as the label.
                  icon: <LibraryBig size={13} aria-hidden="true" />,
                },
              ]}
              value={tier}
              onChange={next => setTier(next as Tier)}
              // `compact` below the mobile breakpoint, never measured collapse:
              // collapse reads the parent's width and falls to a DROPDOWN when
              // that reads 0, which is every jsdom render and every layout pass
              // before the dialog's open animation settles — so a tier would
              // vanish behind a trigger exactly when the strip is being
              // asserted. Compact needs no measurement and keeps all three
              // reachable: each is its icon, and the selected one keeps its
              // label. The icons above are what makes that legible.
              compact={isMobile}
              collapse={false}
              layoutId="avatar-builder-mode"
            />
          </div>
          {tier === 'icon' ? (
            <div className="flex flex-col gap-4 md:flex-row" data-testid="avatar-icon-pane">
              {/* Left: large live preview. Three cases, each matching what Apply
                  would save: after a Reset the name-derived default (Apply hands
                  back `null`, which the roster draws as the name-seeded ghost —
                  so the preview is `CrewAvatar` with no override, NOT a seeded
                  icon pose the save would not produce); for a crew that stored a
                  hand-built ghost face with no pick yet, that STORED ghost plus a
                  note that it is kept until a pick replaces it; otherwise the
                  chosen pose on the chosen background. */}
              <div className="flex w-full flex-col items-center gap-3 md:w-[200px] md:flex-none">
                {reset ? (
                  <CrewAvatar
                    seed={name}
                    size={176}
                    className="rounded-xl border border-border"
                  />
                ) : showStoredGhost ? (
                  <CrewAvatar
                    seed={name}
                    avatar={value}
                    size={176}
                    className="rounded-xl border border-border"
                  />
                ) : (
                  <img
                    src={poseDataUri(iconPose, iconBg)}
                    alt=""
                    aria-hidden="true"
                    width={176}
                    height={176}
                    className="rounded-xl border border-border"
                    data-testid="avatar-icon-preview"
                  />
                )}
                {showStoredGhost && (
                  <p
                    className="text-[11px] text-muted text-center"
                    data-testid="avatar-icon-kept-ghost-note"
                  >
                    {t('components.avatarBuilder.icon_keeps_current')}
                  </p>
                )}
                {/* Background colour. Swatches first (a curated set that reads
                    well behind the white ghost), plus a native picker for a free
                    choice — the swatch is the fast path, the picker the escape
                    hatch. The eyes and silhouette are fixed, so this is the whole
                    colour model. */}
                <div className="flex w-full flex-col gap-2">
                  <span className="text-[12px] text-muted">
                    {t('components.avatarBuilder.icon_background')}
                  </span>
                  <div
                    className="grid grid-cols-[repeat(auto-fill,minmax(28px,1fr))] gap-1.5"
                    role="listbox"
                    aria-label={t('components.avatarBuilder.icon_background')}
                  >
                    {ICON_BG_OPTIONS.map(colour => {
                      const selected = iconBg.toLowerCase() === colour.toLowerCase()
                      return (
                        <button
                          key={colour}
                          type="button"
                          role="option"
                          aria-selected={selected}
                          aria-label={
                            TILE_LABEL_KEYS[colour] ? t(TILE_LABEL_KEYS[colour]) : colour
                          }
                          onClick={() => pickIconBg(colour)}
                          className={`aspect-square rounded-md border-2 transition-colors ${
                            selected ? 'border-ring' : 'border-transparent hover:border-border-strong'
                          }`}
                          style={{ backgroundColor: colour }}
                          data-testid={`avatar-icon-bg-${colour.replace('#', '')}`}
                        />
                      )
                    })}
                  </div>
                  <label className="flex items-center justify-between gap-2 text-[12px]">
                    <span>{t('components.avatarBuilder.icon_custom_color')}</span>
                    <input
                      type="color"
                      value={iconBg}
                      onChange={e => pickIconBg(e.target.value)}
                      className="h-7 w-10 cursor-pointer rounded border border-border bg-transparent"
                      aria-label={t('components.avatarBuilder.icon_custom_color')}
                      data-testid="avatar-icon-bg-custom"
                    />
                  </label>
                </div>
              </div>
              {/* Right: the pose gallery. Each thumbnail is the pose on the
                  CURRENT background, so picking shows exactly what the avatar
                  becomes. */}
              <div className="flex min-w-0 flex-1 flex-col gap-3">
                <div
                  className="grid max-h-[380px] grid-cols-[repeat(auto-fill,minmax(84px,1fr))] gap-2 overflow-y-auto pr-1"
                  role="listbox"
                  aria-label={t('components.avatarBuilder.mode_icon')}
                >
                  {POSE_IDS.map(pose => {
                    const selected = iconPose === pose
                    return (
                      <button
                        key={pose}
                        type="button"
                        role="option"
                        aria-selected={selected}
                        aria-label={t('components.avatarBuilder.icon_pose_label', {
                          n: pose.replace('pose-', ''),
                        })}
                        onClick={() => pickIconPose(pose)}
                        className={`flex items-center justify-center rounded-lg border-2 p-1.5 transition-colors ${
                          selected ? 'border-ring bg-accent-subtle' : 'border-transparent hover:bg-bg-hover'
                        }`}
                        data-testid={`avatar-icon-pose-${pose}`}
                      >
                        <img
                          src={poseDataUri(pose, iconBg)}
                          alt=""
                          aria-hidden="true"
                          width={72}
                          height={72}
                          className="rounded-lg"
                        />
                      </button>
                    )
                  })}
                </div>
              </div>
            </div>
          ) : tier === 'pack' ? (
            <div className="flex flex-col gap-2">
              <CrewAvatarLibraryTab
                open={open}
                name={name}
                selectedId={packId}
                onSelect={id => { setPackId(id); setReset(false) }}
              />
            </div>
          ) : (
            <div className="flex flex-col items-center gap-3 py-2" data-testid="avatar-upload-pane">
              {pending ? (
                <img
                  src={pending}
                  alt=""
                  aria-hidden="true"
                  width={176}
                  height={176}
                  className="rounded-xl border border-border object-cover"
                  data-testid="avatar-upload-preview"
                />
              ) : value?.kind === 'image' ? (
                <CrewAvatar seed={name} avatar={value} size={176} className="rounded-xl" />
              ) : null}
              {/* Drop zone doubles as the click target; a plain button inside
                  keeps it keyboard-reachable without inventing a focusable div. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions -- drag-drop only: the div has no activation of its own (the nested <Btn> is the one action and the tab stop), so a role and tabIndex here would add a focus stop that does nothing */}
              <div
                onDragOver={e => { e.preventDefault(); setDragOver(true) }}
                onDragLeave={() => setDragOver(false)}
                onDrop={e => {
                  e.preventDefault()
                  setDragOver(false)
                  void pickFile(e.dataTransfer.files?.[0])
                }}
                className={`flex w-full max-w-[420px] flex-col items-center gap-2 rounded-xl border-2 border-dashed p-6 text-center transition-colors ${
                  dragOver ? 'border-ring bg-accent-subtle' : 'border-border'
                }`}
                data-testid="avatar-upload-dropzone"
              >
                <ImageUp className="lucide-inline" aria-hidden="true" />
                <span className="text-[12px] text-muted">{t('components.avatarBuilder.upload_hint')}</span>
                <Btn onClick={() => fileInput.current?.click()} data-testid="avatar-upload-choose">
                  {t('components.avatarBuilder.upload_choose')}
                </Btn>
                <input
                  ref={fileInput}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  aria-label={t('components.avatarBuilder.upload_choose')}
                  onChange={e => {
                    void pickFile(e.target.files?.[0])
                    // Allow re-picking the same file after an error.
                    e.target.value = ''
                  }}
                  data-testid="avatar-upload-input"
                />
              </div>
              {/* No hand-off: this dialog holds the crew's unsaved avatar draft
                  (a picked-but-unapplied picture, and behind it the editor's
                  unsaved edits) — navigating away would unmount the editor and
                  discard both. */}
              {pickError && (
                <ErrorNotice
                  variant="inline"
                  message={pickError}
                  onDismiss={() => setPickError('')}
                  testId="avatar-upload-error"
                />
              )}
              <span className="text-[11px] text-muted">{t('components.avatarBuilder.upload_note')}</span>
            </div>
          )}
        </DialogBody>
        {/* flex-wrap: at phone width the reset link + hint block and the
            action buttons cannot share one row; wrapping keeps Apply on
            screen instead of pushing it past the dialog's overflow-hidden
            edge. */}
        <DialogFooter className="flex-wrap">
          {/* Reset previews immediately (the name-derived face); Save is what
              commits either outcome to the editor. The hint under the link
              explains that Apply stages the draft and the editor's own Save
              changes step is what persists it. */}
          <div className="mr-auto flex min-w-0 flex-col gap-0.5">
            <button
              type="button"
              onClick={() => {
                pickGen.current += 1
                setPending(null)
                setPackId(null)
                // Reset the icon draft to the fresh-crew defaults too, so a
                // later pick starts clean rather than on a half-edited pose from
                // before the reset.
                setIconPose(seedIconPose(name))
                setIconBg(seedIconBg())
                setIconTouched(false)
                // The explicit reset — Apply hands back `null`, "everything back
                // to the name-derived default". Land on the Icon tier so the
                // preview shows what a fresh crew gets.
                setReset(true)
                setTier('icon')
              }}
              className="self-start text-[12px] text-muted underline underline-offset-2 hover:text-text"
              data-testid="avatar-builder-reset"
            >
              {t('components.avatarBuilder.reset_default')}
            </button>
            <span className="text-[11px] text-muted">{t('components.avatarBuilder.apply_hint')}</span>
          </div>
          <Btn onClick={onCancel}>{t('components.avatarBuilder.cancel')}</Btn>
          <Btn
            primary
            disabled={applyDisabled}
            onClick={() => onSave(buildResult())}
            data-testid="avatar-builder-save"
          >
            {t('components.avatarBuilder.apply')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
