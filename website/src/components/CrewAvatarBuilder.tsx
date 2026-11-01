/**
 * Game-style ghost avatar builder — the "捏脸" tier of per-crew custom avatars.
 *
 * A nested dialog opened from the crew editor. Left column: large live
 * preview, blush/mirror switches, a randomize button. Right column: one
 * category tab per trait axis with a thumbnail grid, each thumbnail rendering
 * the CURRENT draft face with only that axis varied — so a tile shows exactly
 * what picking it does. The draft starts from the face the crew already wears
 * (the pinned traits, or the name-derived ones), never from a blank.
 *
 * A third identity tier, Library, wears an appearance pack from the crew
 * appearance library — art somebody else drew, served per state by the gateway.
 * Its whole draft is one pack id, so the pane lives in its own component
 * (`CrewAvatarLibraryTab`) which owns the listing, the import and the delete.
 *
 * Reactions is the REACTION layer rather than a tier: per state (working, done,
 * error) a built-in motion and a sound. It is separate from the axes because it
 * is not part of the crew's identity — every other axis stays fixed across
 * states, which is what keeps a reacting crew recognisable as the same crew.
 *
 * The pane is GHOST-ONLY, and it is absent rather than explanatory on the other
 * two tiers: a picture is a static, silent drawing, and a pack ships its own
 * per-state art and audio, so on both there is nothing here to author. A tab
 * that renders only a note saying so is a promise the tier cannot keep.
 *
 * Composition goes through `compose()` from the style module — the same and
 * only path the roster uses — so the preview cannot drift from the saved
 * result. The trait VOCABULARY also stays in the style module: this file only
 * enumerates `Object.keys(...)` of the exported maps, so a new hat appears
 * here without edits.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
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
import { type AvatarState, type RetiredCue } from '../lib/crewAvatarState'
import CrewAvatar, { type CrewAvatarOverride } from './CrewAvatar'
import {
  DEFAULT_POSE_BG,
  POSE_BG_RE,
  POSE_IDS,
  poseDataUri,
  resolvePoseBg,
  seedIconPose,
  seedIconBg as seedIconBgFromName,
  ICON_BG_PALETTE,
} from '../lib/avatarPoses'
import CrewAvatarLibraryTab from './CrewAvatarLibraryTab'
import ErrorNotice from './ErrorNotice'

/**
 * Literal catalog keys, indexed rather than assembled: a key built at runtime
 * is invisible to the extractor and the dead-key gate (see dynamicKeys.test.ts).
 */
const STATE_LABEL_KEYS: Record<AvatarState, string> = {
  working: 'components.avatarBuilder.state_working',
  done: 'components.avatarBuilder.state_done',
  error: 'components.avatarBuilder.state_error',
}

/**
 * Human color names for the tile swatches, keyed by hex. Screen readers get
 * "Sky blue", not "#21a5de" (UX review: a raw hex is noise to a person).
 * Literal catalog keys, indexed — same extractor constraint as
 * OPT_LABEL_KEYS. A hex missing here (a future palette entry) falls back to
 * the hex itself: still announceable, just not friendly, and the dead-key
 * gate will not fire because every listed key ships in the catalog.
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

/** The background a fresh icon draft starts on. When a name is present (the
 *  create flow) it follows the name, exactly as the pose does, so the editor
 *  opens on the SAME colour the create tile previewed — an un-picked Apply then
 *  saves the icon the user was looking at. With no name (editing an existing
 *  crew) it falls back to the active theme's accent when that reads as a hex,
 *  else the art's shipped purple. */
function seedIconBg(name?: string): string {
  const seeded = (name ?? '').trim()
  if (seeded) return seedIconBgFromName(seeded)
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

/** The identity tier: pick a shipped pose, hand-pick ghost traits, wear a
 *  picture, or wear a pack. `icon` is the primary tier new crews are built
 *  with; `face` (the ghost builder) and `pack` stay for crews already wearing
 *  them. */
type Tier = 'icon' | 'face' | 'picture' | 'pack'
/** The dialog's panes. `reactions` is not a fourth identity — it decorates the
 *  ghost tier, which is why `tier` is tracked separately. */
type Pane = Tier | 'reactions'

/** The tier a stored override selects when the dialog opens.
 *
 * Every crew opens on a tier reachable from the strip. A crew with NO override
 * (brand-new, or still on its name-derived face) opens on `icon` — the primary
 * tier new avatars are built with. A crew that pinned a ghost face ALSO opens
 * on `icon` now that the ghost-face picker is gone (issue #18616): its stored
 * ghost is kept and keeps rendering, but the dialog lands on an authorable
 * tier rather than a pane the user can no longer reach. */
const tierOf = (value: CrewAvatarOverride | null): Tier =>
  value?.kind === 'icon'
    ? 'icon'
    : value?.kind === 'image'
      ? 'picture'
      : value?.kind === 'pack'
        ? 'pack'
        : 'icon'

export default function CrewAvatarBuilder({
  open,
  name,
  value,
  retiredCue = null,
  iconOnly = false,
  onCancel,
  onSave,
}: {
  open: boolean
  /** Crew name — the dialog subtitle and the seed of the default face. */
  name: string
  /** The pinned override currently held by the editor, or null for default. */
  value: CrewAvatarOverride | null
  /**
   * Offer the icon tier alone: a pose and a background colour, with no tier
   * strip. The New crewmate card uses it, because a crewmate that does not
   * exist yet has no record for a picture upload or a pack to attach to.
   */
  iconOnly?: boolean
  /**
   * The SAVED record still names a preset sound on a tier that no longer plays
   * one (a picture, a pack), so the crew went quiet without the user doing
   * anything. Read from the record rather than from `value`, which cannot carry
   * it: the reaction readers are ghost-gated, which is the retirement itself.
   *
   * Passed in rather than derived here because the builder is handed a coerced
   * draft, and the raw record is the editor's to read.
   *
   * The STATES, not a flag: the notice names which sounds are going, because
   * "your saved sound" about an unnamed one of three reads as a threat to
   * whichever the reader cares about most. And the TIER they were saved on,
   * because each pane's notice explains its own tier's silence and the
   * explanation is only true there.
   */
  retiredCue?: RetiredCue | null
  onCancel: () => void
  /** null = reset to the name-derived face. */
  onSave: (next: CrewAvatarOverride | null) => void
}) {
  const { t } = useTranslation()
  // Drives the tier strip's compact form. Measured at 320px, the three full
  // labels run 229px (English) to 290px (Russian) into 214px of dialog, so a
  // phone cannot show all three as words.
  const isMobile = useIsMobile()
  const [tier, setTier] = useState<Tier>(tierOf(value))
  const [pane, setPane] = useState<Pane>(tierOf(value))
  // Whether the crew ALREADY wears a ghost face when the editor opens. The
  // ghost-face picker is gone (issue #18616), so this no longer gates any tab —
  // it marks the stored record that must be KEPT verbatim: a ghost crew opens
  // on the Icon tier and its stored ghost is handed back unchanged unless the
  // user actively picks an SVG pose or colour. Keyed off the STORED record, not
  // the live tier, so a trip through another tier never loses the ghost.
  const wearsGhost = useMemo(() => value?.kind === 'ghost', [value])
  /** The pack the draft wears, or null. A pack override is nothing BUT this id,
   *  so the Library pane needs no draft of its own. */
  const [packId, setPackId] = useState<string | null>(value?.kind === 'pack' ? value.id : null)
  /** The ICON draft: which pose, and the background colour behind it. Seeded
   *  from the stored icon record when editing one, else the name-seeded pose
   *  and colour (both follow the name) — so the editor opens on the SAME icon
   *  the create tile previewed; with no name it falls back to a theme-accent
   *  background. Held even while another tier is selected (like the
   *  ghost draft), so a trip through another tab never discards it. */
  const [iconPose, setIconPose] = useState<string>(value?.kind === 'icon' ? value.pose : seedIconPose(name))
  const [iconBg, setIconBg] = useState<string>(
    value?.kind === 'icon' ? resolvePoseBg(value.bg) : seedIconBg(name),
  )
  // Any pose, swatch or colour pick sets this, so the seeded pose itself is
  // a pick like any other once the user chooses it.
  const [iconTouched, setIconTouched] = useState(false)
  // The user hit "back to the default" and has not picked since. This is the
  // UNDRESS intent: Apply then commits `null` — the reset spelling the backend
  // documents for every tier — regardless of which tier the reset landed on.
  // Without it, a non-ghost crew's Reset lands on the Icon tier whose
  // buildResult() always returns a concrete icon, so Reset would silently save
  // a name-seeded icon instead of clearing the override. Any real pick, a tab
  // change, or a reopen clears it.
  const [resetToDefault, setResetToDefault] = useState(false)
  // Whether the user hit Reset this opening, kept SEPARATELY from
  // `resetToDefault` because a tab change clears that one (it re-seeds the icon
  // draft) but must NOT un-say a reset. Without this, a ghost crew's Reset
  // followed by a tab detour and back to Icon would let the keep-the-stored-
  // ghost shortcut in buildResult resurrect the very ghost the reset cleared
  // (#18616 review). Cleared only by a real pick or a reopen.
  const [didReset, setDidReset] = useState(false)
  /** The cropped-and-scaled picture chosen THIS opening (data URI), not yet
   *  uploaded — upload happens on the editor's Save, keeping Apply free of
   *  side effects for pictures exactly as it is for traits. */
  const [pending, setPending] = useState<string | null>(
    value?.kind === 'image' ? (value.pendingData ?? null) : null,
  )
  const [pickError, setPickError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  /* The states the retired-cue notice names, in the record's own order. A
     plain comma list rather than a localised conjunction: "A, B" needs no
     per-language joining rule, where "A and B" would. */
  const retiredCueLabel = (retiredCue?.states ?? []).map(state => t(STATE_LABEL_KEYS[state])).join(', ')
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
      setPane(tierOf(value))
      setPackId(value?.kind === 'pack' ? value.id : null)
      setIconPose(value?.kind === 'icon' ? value.pose : seedIconPose(name))
      setIconBg(
        value?.kind === 'icon' ? resolvePoseBg(value.bg) : seedIconBg(name),
      )
      setIconTouched(false)
      setResetToDefault(false)
      setDidReset(false)
      setPending(value?.kind === 'image' ? (value.pendingData ?? null) : null)
      setPickError('')
      setDragOver(false)
      pickGen.current += 1
    }
  }, [open, value, name, iconOnly])

  // While the Picture pane is showing, a file dropped ANYWHERE but the small
  // dashed zone (the preview image above it, the dialog body, the page) would
  // take the browser's default — navigate to the file — and unmount the whole
  // editor with every unsaved crew edit in it. Cancel the default at the
  // window for the pane's lifetime; the drop zone's own handlers still run
  // first (bubbling) and stop propagation is not needed because both listen
  // for the same default-cancelling outcome.
  useEffect(() => {
    if (!open || pane !== 'picture') return
    const swallow = (e: DragEvent) => { e.preventDefault() }
    window.addEventListener('dragover', swallow)
    window.addEventListener('drop', swallow)
    return () => {
      window.removeEventListener('dragover', swallow)
      window.removeEventListener('drop', swallow)
    }
  }, [open, pane])

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
      if (gen === pickGen.current) { setResetToDefault(false); setPending(uri) }
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

  // The icon-only door opens on a seeded pose over the theme colour, not on
  // the face the card shows. Until the user picks, the preview shows the
  // card's own face and Apply waits, so opening the editor never swaps the
  // crewmate's face for a plain ghost the user never chose.
  const iconUntouched = iconOnly && value === null && !iconTouched
  // The Icon pane is showing the crew's CURRENT avatar rather than a chosen
  // icon, so it must highlight no pose/colour: the create-mode door (above),
  // AND a crew that already wears a ghost and has not picked yet (#18616) — its
  // stored ghost is what Apply keeps, so nothing in the grid is "selected"
  // until the user actually picks. Unlike `iconUntouched`, this does NOT
  // disable Apply: for a ghost crew, Apply with no pick is the valid
  // "keep my ghost" outcome.
  const iconShowsCurrent = iconUntouched || (wearsGhost && !iconTouched)
  // A ghost crew that has not picked previews through the REAL renderer, so the
  // preview always matches what Apply saves (preview-matches-Apply, #18616
  // review). Before a reset that is the stored ghost (`value`); after a reset it
  // is `undefined` — what the committed `null` renders, the name-derived face —
  // NOT the name-seeded pose img, which Apply would never save here.
  const iconPreviewsGhost = wearsGhost && !iconTouched
  const iconGhostPreviewAvatar = didReset ? undefined : (value ?? undefined)
  const applyDisabled =
    (tier === 'picture' && pictureResult === null) || (tier === 'pack' && packId === null)
    || iconUntouched

  /**
   * The override Apply commits.
   *
   * The reaction layer leaves only through a GHOST result. A picture is static
   * and silent and a pack plays its own art and audio, so neither carries a
   * reaction key — but the pane's state is kept while another tier is selected,
   * so switching to a picture and back does not discard work done here.
   * Reactions alone are also a complete override — `{kind:'ghost', motions}`
   * means "the name-derived face, plus these reactions".
   */
  const buildResult = (): CrewAvatarOverride | null => {
    // The user hit "back to the default" and has not picked since. What the
    // default IS depends on whether the crew already wears a ghost:
    //   - A crew that ALREADY wears a ghost resets to its own name-derived
    //     face. `null` is the undress spelling the backend reads as "the name's
    //     face", so that crew keeps its ghost (#18616: an existing ghost is
    //     never disturbed).
    //   - A crew that does NOT wear a ghost must never reset INTO one. The reset
    //     handler landed the view on the icon tier and re-seeded iconPose/iconBg
    //     to the name-seeded defaults, so we fall through to the icon branch
    //     below and commit that icon — Reset previews and saves the SAME icon,
    //     and is never a path to a ghost the crew never chose.
    if (resetToDefault && wearsGhost) return null
    if (tier === 'icon') {
      // A ghost crew that RESET this opening undresses to its name-derived face
      // (`null`), even after a tab detour cleared `resetToDefault` — `didReset`
      // outlives that, so the keep-the-stored-ghost shortcut below cannot bring
      // back the ghost the reset cleared (#18616 review).
      if (wearsGhost && didReset) return null
      // Edge case (#18616): a crew that already wears a ghost opens on the Icon
      // tier, but its stored ghost is KEPT until the user actively picks an SVG
      // pose or colour. Until that first pick (`iconTouched`) — and provided
      // they did not reset — Apply hands the stored ghost back verbatim rather
      // than overwriting it with the name-seeded icon; stored ghosts are never
      // migrated.
      if (wearsGhost && !iconTouched && !didReset) return value
      // The whole override is the pose + background; no reactions ride on it
      // (an icon is static, like a picture). `bg` is normalized through the same
      // resolver the record reader uses, so a draft and its reload compare equal.
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
    if (tier === 'picture') return pictureResult
    return null
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
          {/* Tier switch: icon pose, uploaded picture, or a library pack. Above
              every pane so switching never loses the other side's in-progress
              state — the pending picture and the pack id live in separate state.
              The ghost-face tier and its Reactions sub-tab were removed with the
              ghost-face picker (issue #18616): a ghost can no longer be authored
              or edited here, though stored ghosts are kept and keep rendering. */}
          {/* overflow-x-auto is the floor under `compact` below: compact clears
              every shipped locale at 320px, but the strip must stay reachable
              rather than clipped if a longer one ever lands. It shows no
              scrollbar while nothing overflows, which is every desktop width. */}
          {!iconOnly && <div className="mb-3 overflow-x-auto" data-testid="avatar-builder-tabs">
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
              value={pane}
              onChange={next => {
                const p = next as Pane
                setResetToDefault(false)
                setPane(p)
                if (p !== 'reactions') setTier(p)
              }}
              // `compact` below the mobile breakpoint, never measured collapse:
              // collapse reads the parent's width and falls to a DROPDOWN when
              // that reads 0, which is every jsdom render and every layout pass
              // before the dialog's open animation settles — so two of the three
              // tiers would vanish behind a trigger exactly when the strip is
              // being asserted. Compact needs no measurement and keeps all three
              // reachable: each is its icon, and the selected one keeps its
              // label. The icons above are what makes that legible.
              compact={isMobile}
              collapse={false}
              layoutId="avatar-builder-mode"
            />
          </div>}
          {pane === 'icon' ? (
            <div className="flex flex-col gap-4 md:flex-row" data-testid="avatar-icon-pane">
              {/* Left: large live preview of the chosen pose on the chosen
                  background, mirroring the Face pane's layout so switching tiers
                  does not jump the dialog's shape. */}
              <div className="flex w-full flex-col items-center gap-3 md:w-[200px] md:flex-none">
                {/* The icon-only door previews the seeded ICON (pose over the
                    seeded colour) the card shows, both before and after a pick,
                    so opening the editor never shows the trait ghost the icon
                    tier replaced. A crew that already wears a ghost is the one
                    exception (#18616): until it picks, the pane previews that
                    stored ghost through the real renderer — the face Apply
                    would keep — so the preview matches what Apply saves. */}
                {iconPreviewsGhost ? (
                  <CrewAvatar seed={name} avatar={iconGhostPreviewAvatar} size={176} />
                ) : (
                  <img
                    src={poseDataUri(iconPose, iconBg)}
                    alt=""
                    aria-hidden="true"
                    width={176}
                    height={176}
                    className="rounded-full border border-border"
                    data-testid={iconShowsCurrent ? 'avatar-icon-preview-current' : 'avatar-icon-preview'}
                  />
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
                    {ICON_BG_PALETTE.map(colour => {
                      const selected = !iconShowsCurrent && iconBg.toLowerCase() === colour.toLowerCase()
                      return (
                        <button
                          key={colour}
                          type="button"
                          role="option"
                          aria-selected={selected}
                          aria-label={
                            TILE_LABEL_KEYS[colour] ? t(TILE_LABEL_KEYS[colour]) : colour
                          }
                          onClick={() => { setIconBg(colour); setIconTouched(true); setResetToDefault(false); setDidReset(false) }}
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
                      onChange={e => { setIconBg(e.target.value); setIconTouched(true); setResetToDefault(false); setDidReset(false) }}
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
                    const selected = !iconShowsCurrent && iconPose === pose
                    return (
                      <button
                        key={pose}
                        type="button"
                        role="option"
                        aria-selected={selected}
                        aria-label={t('components.avatarBuilder.icon_pose_label', {
                          n: pose.replace('pose-', ''),
                        })}
                        onClick={() => { setIconPose(pose); setIconTouched(true); setResetToDefault(false); setDidReset(false) }}
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
                          className="rounded-full"
                        />
                      </button>
                    )
                  })}
                </div>
              </div>
            </div>
          ) : pane === 'pack' ? (
            <div className="flex flex-col gap-2">
              <CrewAvatarLibraryTab
                open={open}
                name={name}
                selectedId={packId}
                onSelect={id => { setResetToDefault(false); setPackId(id) }}
              />
              {/* Same reason as the picture tier's line: this tier has no
                  Reactions tab, and an unexplained absence reads as a bug. */}
              <span className="text-[11px] text-muted" data-testid="avatar-reactions-absent-pack">
                {t('components.avatarBuilder.react_absent_note')}
              </span>
              {retiredCue?.kind === 'pack' && (
                /* Consequence copy, not a footnote: this line is the only
                   warning between the user and a saved cue that disappears
                   for good on the next save, so it is a callout rather than
                   the muted aside it was (UX lens 2 -- never mute
                   consequence copy). `text-warn` and not `text-warn-fg`:
                   that token is the ink for a SOLID `--warn` fill and is
                   `#000` in almost every dark theme, which on a 12% tint
                   over a dark page is black on brown. */
                <p
                  role="note"
                  className="rounded-md border border-warn/30 bg-warn-subtle px-2 py-1.5 text-[11px] text-warn-text"
                  data-testid="avatar-reactions-retired-cue-pack"
                >
                  {t('components.avatarBuilder.react_retired_cue_note_pack', { states: retiredCueLabel })}
                </p>
              )}
            </div>
          ) : pane === 'picture' ? (
            <div className="flex flex-col items-center gap-3 py-2" data-testid="avatar-upload-pane">
              {pending ? (
                <img
                  src={pending}
                  alt=""
                  aria-hidden="true"
                  width={176}
                  height={176}
                  className="rounded-full border border-border object-cover"
                  data-testid="avatar-upload-preview"
                />
              ) : value?.kind === 'image' ? (
                <CrewAvatar seed={name} avatar={value} size={176} />
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
                  unsaved ghost traits) — navigating to the chat would unmount
                  the editor and discard both. */}
              {pickError && (
                <ErrorNotice
                  variant="inline"
                  message={pickError}
                  onDismiss={() => setPickError('')}
                  testId="avatar-upload-error"
                />
              )}
              <span className="text-[11px] text-muted">{t('components.avatarBuilder.upload_note')}</span>
              {/* The Reactions tab is the ghost's, so on this tier it is not in
                  the strip at all — and a tab that vanishes with no word said
                  reads as something missing rather than as something decided
                  (first-run reader: "why 'Reactions' is absent… I do not know").
                  The absence is explained where the user is looking. */}
              <span className="text-[11px] text-muted" data-testid="avatar-reactions-absent-picture">
                {t('components.avatarBuilder.react_absent_note')}
              </span>
              {/* This crew was SAVED with a preset chime and will not make it
                  any more. Silence with no word said is the worst version of
                  that: the user hunts for a broken sound setting rather than
                  reading one line about a tier that has none. Shown only for a
                  record that really carries one, so it is not a standing notice
                  every picture crew has to dismiss with its eyes. */}
              {retiredCue?.kind === 'image' && (
                /* Same callout, same token reasoning as the pack tier above. */
                <p
                  role="note"
                  className="rounded-md border border-warn/30 bg-warn-subtle px-2 py-1.5 text-[11px] text-warn-text"
                  data-testid="avatar-reactions-retired-cue-picture"
                >
                  {t('components.avatarBuilder.react_retired_cue_note_picture', { states: retiredCueLabel })}
                </p>
              )}
            </div>
          ) : null}
        </DialogBody>
        {/* flex-wrap: at phone width the reset link + hint block and the
            action buttons cannot share one row; wrapping keeps Apply on
            screen instead of pushing it past the dialog's overflow-hidden
            edge. */}
        <DialogFooter className="flex-wrap">
          {/* Reset previews immediately (draft -> null shows the name-derived
              face); Save is what commits either outcome to the editor. The
              hint under the link is High finding #2 from the first-run review:
              without it, Apply reads as the final save and the editor's own
              Save changes step is a silent second commit the user can miss. */}
          <div className="mr-auto flex min-w-0 flex-col gap-0.5">
            {/* The icon-only door has no tier to reset to and no second save
                step: Cancel keeps the current look, Apply is the pick, and
                this link returns to the name's own face, so a pick is never
                a one-way door. It shows once a pick is applied: before that
                the card already wears that face, and Cancel keeps it. */}
            {iconOnly && value !== null && (
              <button
                type="button"
                onClick={() => onSave(null)}
                className="self-start text-[12px] text-muted underline underline-offset-2 hover:text-text"
                data-testid="avatar-builder-use-original"
              >
                {t('components.avatarBuilder.use_original_look', { name })}
              </button>
            )}
            {!iconOnly && <>
            <button
              type="button"
              onClick={() => {
                pickGen.current += 1
                setPending(null)
                setPackId(null)
                // The user asked to undress: Apply commits `null` whatever
                // tier this lands on. Cleared by any pick (icon pose/colour,
                // a trait, a pack, a picture) below.
                setResetToDefault(true)
                // Persisted across a tab change (which clears resetToDefault):
                // a ghost crew's reset must keep winning over the keep-the-
                // stored-ghost shortcut in buildResult, even after a detour
                // (#18616 review).
                setDidReset(true)
                // Clear any earlier pose/colour pick so the preview and
                // selection reflect the reset, not a pick the reset discarded
                // (#18616 review — pick-then-reset mismatch). A ghost crew then
                // previews the name-derived face `null` renders; a non-ghost
                // crew re-seeds its icon below and previews that.
                setIconTouched(false)
                // Reset the icon draft to the fresh-crew defaults too, so a
                // later switch to the Icon tier starts clean rather than on a
                // half-edited pose from before the reset.
                setIconPose(seedIconPose(name))
                setIconBg(seedIconBg(name))
                // The default face has no reactions either: this link is the
                // one control that means "everything back to the default" — and
                // the label says both halves out loud, because a link promising
                // only the face while also erasing two motions and three sounds
                // is a control the user cannot predict.
                // Reset always lands on the Icon tier now that the ghost-face
                // tier is unreachable (issue #18616). For a ghost crew the
                // `resetToDefault && wearsGhost` branch in buildResult still
                // commits `null` (the undress the backend reads as "the name's
                // face"), so Reset returns it to its name-derived default rather
                // than a name-seeded icon; a non-ghost crew commits that icon.
                setTier('icon')
                setPane('icon')
              }}
              className="self-start text-[12px] text-muted underline underline-offset-2 hover:text-text"
              data-testid="avatar-builder-reset"
            >
              {t('components.avatarBuilder.reset_default')}
            </button>
            <span className="text-[11px] text-muted">{t('components.avatarBuilder.apply_hint')}</span>
            </>}
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
