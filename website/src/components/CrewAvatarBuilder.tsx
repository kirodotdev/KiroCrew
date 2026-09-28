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
import { Coffee, Crown, Dices, Eye, Ghost, Heart, Image as ImageIcon, ImageUp, LibraryBig, Meh, Palette, Play, Smile, Sparkles } from 'lucide-react'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { Btn, Toggle } from './ui'
import SegmentedControl from './SegmentedControl'
import SimpleSelect from './SimpleSelect'
import { useIsMobile } from '../hooks/useIsMobile'
import {
  ACCESSORIES,
  BROWS,
  BRAND_PURPLE,
  EYES,
  MOTION_NAMES,
  MOTION_STATES,
  MOUTHS,
  PROPS,
  TILES,
  ghostDataUri,
  type KiroGhostTraits,
  type MotionState,
} from '../lib/kiroGhostAvatar'
import {
  AVATAR_STATES,
  MOTION_DEFAULTS,
  type AvatarMotions,
  type AvatarSounds,
  type AvatarState,
  type RetiredCue,
} from '../lib/crewAvatarState'
import { SOUND_PRESETS, loadSoundSettings, playPreset, type SoundPreset } from '../hooks/useNotificationSound'
import CrewAvatar, { seededTraits, type CrewAvatarOverride } from './CrewAvatar'
import CrewAvatarLibraryTab from './CrewAvatarLibraryTab'
import ErrorNotice from './ErrorNotice'

/** Trait axes shown as category tabs, in mockup order. `blush` is a two-option
 *  axis (off/on) and `tile` is the color — both special-cased below. */
type Axis = 'eyes' | 'brows' | 'mouth' | 'accessory' | 'prop' | 'blush' | 'tile'

/**
 * Options per pickable axis, read off the style module. `accessory` has no
 * 'none' key in its map (absence is a probability there, not an entry), but
 * `compose` resolves an unknown key to nothing — so a literal 'none' option
 * is both honest and renderable.
 */
const AXIS_OPTIONS: Record<Exclude<Axis, 'tile' | 'blush'>, string[]> = {
  eyes: Object.keys(EYES),
  brows: Object.keys(BROWS),
  mouth: Object.keys(MOUTHS),
  accessory: ['none', ...Object.keys(ACCESSORIES)],
  prop: Object.keys(PROPS),
}

const AXES: Axis[] = ['eyes', 'brows', 'mouth', 'accessory', 'prop', 'blush', 'tile']

/** The states that HAVE a motion, as a lookup the row builder can narrow with.
 *  `working` is not one of them: the ghost's working animation is its own, and a
 *  reaction fires on the transition out of it. */
const MOTION_ROW_STATES: ReadonlySet<string> = new Set<string>(MOTION_STATES)
const motionStateOf = (state: AvatarState): MotionState | null =>
  MOTION_ROW_STATES.has(state) ? (state as MotionState) : null

/**
 * Literal catalog keys, indexed rather than assembled: a key built at runtime
 * (`t(\`…opt_${k}\`)`) is invisible to the extractor and the dead-key gate
 * (see dynamicKeys.test.ts — AboutPanel's UPDATE_ERROR_KEYS is the pattern).
 */
const AXIS_LABEL_KEYS: Record<Axis, string> = {
  eyes: 'components.avatarBuilder.axis_eyes',
  brows: 'components.avatarBuilder.axis_brows',
  mouth: 'components.avatarBuilder.axis_mouth',
  accessory: 'components.avatarBuilder.axis_accessory',
  prop: 'components.avatarBuilder.axis_prop',
  blush: 'components.avatarBuilder.axis_blush',
  tile: 'components.avatarBuilder.axis_tile',
}

/** Same indexed-literal rule as AXIS_LABEL_KEYS. */
const STATE_LABEL_KEYS: Record<AvatarState, string> = {
  working: 'components.avatarBuilder.state_working',
  done: 'components.avatarBuilder.state_done',
  error: 'components.avatarBuilder.state_error',
}

/** Motion names, labelled. Same indexed-literal rule as AXIS_LABEL_KEYS — the
 *  key is written out rather than assembled from the motion name, so the
 *  extractor and the dead-key gate can both see it. */
const MOTION_LABEL_KEYS: Record<string, string> = {
  none: 'components.avatarBuilder.motion_none',
  bounce: 'components.avatarBuilder.motion_bounce',
  nod: 'components.avatarBuilder.motion_nod',
  sparkle: 'components.avatarBuilder.motion_sparkle',
  shake: 'components.avatarBuilder.motion_shake',
  'cross-eyes': 'components.avatarBuilder.motion_cross_eyes',
  droop: 'components.avatarBuilder.motion_droop',
}

const SOUND_LABEL_KEYS: Record<Exclude<SoundPreset, 'none'>, string> = {
  chime: 'components.avatarBuilder.sound_chime',
  ding: 'components.avatarBuilder.sound_ding',
  blip: 'components.avatarBuilder.sound_blip',
  pop: 'components.avatarBuilder.sound_pop',
  pulse: 'components.avatarBuilder.sound_pulse',
}

const OPT_LABEL_KEYS: Record<string, string> = {
  none: 'components.avatarBuilder.opt_none',
  blush: 'components.avatarBuilder.opt_blush',
  canon: 'components.avatarBuilder.opt_canon',
  closed: 'components.avatarBuilder.opt_closed',
  sleepy: 'components.avatarBuilder.opt_sleepy',
  wink: 'components.avatarBuilder.opt_wink',
  wide: 'components.avatarBuilder.opt_wide',
  sparkle: 'components.avatarBuilder.opt_sparkle',
  visor: 'components.avatarBuilder.opt_visor',
  glasses: 'components.avatarBuilder.opt_glasses',
  cross: 'components.avatarBuilder.opt_cross',
  squint: 'components.avatarBuilder.opt_squint',
  swirl: 'components.avatarBuilder.opt_swirl',
  heart: 'components.avatarBuilder.opt_heart',
  cyclops: 'components.avatarBuilder.opt_cyclops',
  raised: 'components.avatarBuilder.opt_raised',
  angry: 'components.avatarBuilder.opt_angry',
  flat: 'components.avatarBuilder.opt_flat',
  smile: 'components.avatarBuilder.opt_smile',
  open: 'components.avatarBuilder.opt_open',
  cat: 'components.avatarBuilder.opt_cat',
  oh: 'components.avatarBuilder.opt_oh',
  grin: 'components.avatarBuilder.opt_grin',
  tongue: 'components.avatarBuilder.opt_tongue',
  wobble: 'components.avatarBuilder.opt_wobble',
  smirk: 'components.avatarBuilder.opt_smirk',
  antenna: 'components.avatarBuilder.opt_antenna',
  halo: 'components.avatarBuilder.opt_halo',
  cap: 'components.avatarBuilder.opt_cap',
  phones: 'components.avatarBuilder.opt_phones',
  bow: 'components.avatarBuilder.opt_bow',
  crown: 'components.avatarBuilder.opt_crown',
  beanie: 'components.avatarBuilder.opt_beanie',
  party: 'components.avatarBuilder.opt_party',
  flower: 'components.avatarBuilder.opt_flower',
  bandana: 'components.avatarBuilder.opt_bandana',
  hardhat: 'components.avatarBuilder.opt_hardhat',
  mug: 'components.avatarBuilder.opt_mug',
  glass: 'components.avatarBuilder.opt_glass',
  wrench: 'components.avatarBuilder.opt_wrench',
  bolt: 'components.avatarBuilder.opt_bolt',
  star: 'components.avatarBuilder.opt_star',
  term: 'components.avatarBuilder.opt_term',
}

/** Every pickable tile, brand purple first — the "no hue" spelling. */
const TILE_OPTIONS = [BRAND_PURPLE, ...TILES]

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

const pickRandom = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)]

/** Longest source file the picker accepts BEFORE crop/downscale. Generous —
 *  the output is re-encoded regardless — but bounds the decode of a
 *  mis-picked 200MB TIFF-in-a-.png. */
const MAX_SOURCE_BYTES = 20 * 1024 * 1024
/** Output edge — matches the design spec and keeps the upload tens of KB. */
const OUTPUT_PX = 512
/** Client-side output budget, under the server's 1 MB cap with headroom. */
const MAX_OUTPUT_BYTES = 900 * 1024
/** Volume for an explicitly requested preview when the user's own is muted —
 *  a preview button that does nothing reads as broken. */
const PREVIEW_FALLBACK_VOLUME = 0.35

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

/** The identity tier: hand-pick ghost traits, wear a picture, or wear a pack. */
type Tier = 'face' | 'picture' | 'pack'
/** The dialog's panes. `reactions` is not a fourth identity — it decorates the
 *  ghost tier, which is why `tier` is tracked separately. */
type Pane = Tier | 'reactions'

/** The tier a stored override selects when the dialog opens. */
const tierOf = (value: CrewAvatarOverride | null): Tier =>
  value?.kind === 'image' ? 'picture' : value?.kind === 'pack' ? 'pack' : 'face'

/** Only the states the user actually configured are stored, so an untouched
 *  reaction layer stays absent from the record rather than shipping three
 *  empty objects. */
const isSet = (map: object): boolean => Object.keys(map).length > 0

export default function CrewAvatarBuilder({
  open,
  name,
  value,
  retiredCue = null,
  savedPack = false,
  savedReactions,
  onCancel,
  onSave,
}: {
  open: boolean
  /** Crew name — the dialog subtitle and the seed of the default face. */
  name: string
  /** The pinned override currently held by the editor, or null for default. */
  value: CrewAvatarOverride | null
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
  /**
   * The crew's SAVED record wears an appearance pack.
   *
   * Read from the record rather than from `value`, which is the editor's draft
   * and stops saying "pack" the moment one Apply has landed — while the stored
   * record keeps wearing it until the editor's own Save. `buildResult` needs the
   * stored tier, not the drafted one (see the faceless-ghost branch there).
   */
  savedPack?: boolean
  /**
   * Which reaction keys the crew's SAVED record carries a non-empty map for.
   *
   * `buildResult` names an emptied map on the wire so the backend's carry does
   * not restore what the user cleared — and the question "is there anything to
   * restore" is about the STORED record, not the draft. `value` is the draft, and
   * it stops carrying the original after the first Apply lands an empty map: a
   * later reopen re-inits from `{}`, the draft-only test reads "never had one",
   * the key is omitted, and the carry brings the cleared motion back. Read from
   * the record by the editor, the same way `retiredCue` and `savedPack` are.
   */
  savedReactions?: { motions?: boolean; sounds?: boolean }
  onCancel: () => void
  /** null = reset to the name-derived face. */
  onSave: (next: CrewAvatarOverride | null) => void
}) {
  const { t } = useTranslation()
  // Drives the tier strip's compact form. Measured at 320px, the three full
  // labels run 229px (English) to 290px (Russian) into 214px of dialog, so a
  // phone cannot show all three as words.
  const isMobile = useIsMobile()
  /** Name-derived traits — the pre-fill when nothing is pinned, and the face
   *  the reset link previews. */
  const defaults = useMemo(() => seededTraits(name), [name])
  /** null draft = "no override": preview the default face; the first pick
   *  branches the draft off it. */
  const [draft, setDraft] = useState<KiroGhostTraits | null>(
    value?.kind === 'ghost' ? (value.traits ?? null) : null,
  )
  const [axis, setAxis] = useState<Axis>('eyes')
  const [tier, setTier] = useState<Tier>(tierOf(value))
  const [pane, setPane] = useState<Pane>(tierOf(value))
  /** The pack the draft wears, or null. A pack override is nothing BUT this id,
   *  so the Library pane needs no draft of its own. */
  const [packId, setPackId] = useState<string | null>(value?.kind === 'pack' ? value.id : null)
  /** Per-state reactions. Held flat (not nested under the tier) so a trip
   *  through the Picture tab and back never discards them — the pane is hidden
   *  there, not reset. Only a ghost result carries them out. */
  const [motions, setMotions] = useState<AvatarMotions>(
    value?.kind === 'ghost' ? (value.motions ?? {}) : {},
  )
  const [sounds, setSounds] = useState<AvatarSounds>(
    value?.kind === 'ghost' ? (value.sounds ?? {}) : {},
  )
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
      setDraft(value?.kind === 'ghost' ? (value.traits ?? null) : null)
      setAxis('eyes')
      setTier(tierOf(value))
      setPane(tierOf(value))
      setPackId(value?.kind === 'pack' ? value.id : null)
      setMotions(value?.kind === 'ghost' ? (value.motions ?? {}) : {})
      setSounds(value?.kind === 'ghost' ? (value.sounds ?? {}) : {})
      setPending(value?.kind === 'image' ? (value.pendingData ?? null) : null)
      setPickError('')
      setDragOver(false)
      pickGen.current += 1
    }
  }, [open, value])

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

  const shown = draft ?? defaults

  const setTrait = (patch: Partial<KiroGhostTraits>) => setDraft({ ...shown, ...patch })

  const randomize = () =>
    setDraft({
      eyes: pickRandom(AXIS_OPTIONS.eyes),
      brows: pickRandom(AXIS_OPTIONS.brows),
      mouth: pickRandom(AXIS_OPTIONS.mouth),
      accessory: pickRandom(AXIS_OPTIONS.accessory),
      prop: pickRandom(AXIS_OPTIONS.prop),
      blush: Math.random() < 0.5,
      flip: Math.random() < 0.5,
      tile: pickRandom(TILE_OPTIONS),
    })

  /** Thumbnails for the active axis: the draft face with one axis varied. */
  const thumbs = useMemo(() => {
    if (axis === 'tile') {
      return TILE_OPTIONS.map(tile => ({ key: tile, uri: ghostDataUri({ ...shown, tile }) }))
    }
    if (axis === 'blush') {
      // A two-option axis: off first (parallel to every other tab's "None").
      return [
        { key: 'none', uri: ghostDataUri({ ...shown, blush: false }) },
        { key: 'blush', uri: ghostDataUri({ ...shown, blush: true }) },
      ]
    }
    return AXIS_OPTIONS[axis].map(key => ({
      key,
      uri: ghostDataUri({ ...shown, [axis]: key }),
    }))
  }, [axis, shown])

  const optLabel = (key: string) => { const k = OPT_LABEL_KEYS[key]; return k ? t(k) : key }

  /** Store one state's motion. Written even when it equals the default: the
   *  select shows the default as the current choice, so a user who opens the
   *  list and picks that same entry has made a decision, and a record that
   *  omitted it would silently follow a future change of default. */
  const setStateMotion = (state: MotionState, name: string) =>
    setMotions(prev => ({ ...prev, [state]: name }))

  const setStateSound = (state: AvatarState, preset: string) =>
    setSounds(prev => {
      const out = { ...prev }
      if (preset) out[state] = preset as SoundPreset
      else delete out[state]
      return out
    })

  const resetState = (state: AvatarState) => {
    const ms = motionStateOf(state)
    if (ms) setMotions(prev => { const out = { ...prev }; delete out[ms]; return out })
    setSounds(prev => { const out = { ...prev }; delete out[state]; return out })
  }

  /** Accessible name of one state's sound control — the state alone would
   *  repeat three times on the pane. */
  const soundLabel = (state: AvatarState) =>
    `${t(STATE_LABEL_KEYS[state])} — ${t('components.avatarBuilder.react_sound')}`

  const previewSound = (preset: SoundPreset) => {
    // An explicit preview ignores the global on/off — the user just asked to
    // hear it — but still uses their volume, falling back only if muted.
    const settings = loadSoundSettings()
    playPreset(preset, settings.volume > 0 ? settings.volume : PREVIEW_FALLBACK_VOLUME)
  }

  /** Icons keep every axis visible when the strip collapses to its compact
   *  form on narrow widths — an icon-less segment there renders as an empty
   *  button, hiding the axis entirely. */
  const AXIS_ICONS: Record<Axis, React.ReactNode> = {
    eyes: <Eye size={13} aria-hidden="true" />,
    brows: <Meh size={13} aria-hidden="true" />,
    mouth: <Smile size={13} aria-hidden="true" />,
    accessory: <Crown size={13} aria-hidden="true" />,
    prop: <Coffee size={13} aria-hidden="true" />,
    blush: <Heart size={13} aria-hidden="true" />,
    tile: <Palette size={13} aria-hidden="true" />,
  }
  const segments = AXES.map(a => ({ key: a, label: t(AXIS_LABEL_KEYS[a]), icon: AXIS_ICONS[a] }))

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
      if (gen === pickGen.current) setPending(uri)
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
    (tier === 'picture' && pictureResult === null) || (tier === 'pack' && packId === null)

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
    if (tier === 'pack') {
      // A pack override is the id and nothing else: the art is the library's, so
      // there is no draft to merge and no stored field to preserve. Apply is
      // disabled until an id is picked, so the guard is a type narrowing rather
      // than a reachable branch.
      if (!packId) return null
      return { kind: 'pack', id: packId }
    }
    if (tier === 'picture') return pictureResult
    // An emptied map is a STATEMENT, not an absence, and the wire cannot tell
    // the two apart unless this says so: `_carry_motions_through_motionless_save`
    // keeps a ghost's stored `motions` when a save says NOTHING about the key,
    // because the shipped editor could not author them and a save from a client
    // that cannot see a value is not a decision about it. This builder can see
    // them, so clearing the last one has to name the key — omitting it reads as
    // that older client and the backend restores what the user just cleared.
    // Both keys go through one rule rather than only the one that has a carry
    // today, so a second carry cannot quietly resurrect a cleared cue.
    //
    // Judged on the SAVED record first: that is what the backend would restore.
    // The draft is consulted too, for the window between an Apply that set a
    // reaction and the Save that persists it — clearing inside that window has
    // nothing stored to restore, so naming the key there costs nothing and
    // keeps the rule one sentence. What the draft alone cannot answer is the
    // reopen after a clear was applied: it re-inits from `{}` and would read
    // "never had one" while the record still has it.
    const cleared = (key: 'motions' | 'sounds'): boolean =>
      !!savedReactions?.[key] || (value?.kind === 'ghost' && isSet(value[key] ?? {}))
    const reactions = {
      ...(isSet(motions) ? { motions } : cleared('motions') ? { motions: {} } : {}),
      ...(isSet(sounds) ? { sounds } : cleared('sounds') ? { sounds: {} } : {}),
    }
    if (draft) return { kind: 'ghost', traits: draft, ...reactions }
    const anyReaction = isSet(motions) || isSet(sounds)
    // A faceless ghost — "the name-derived face, plus these reactions" — cannot
    // express a TIER CHANGE when the stored record is a pack:
    // `_carry_pack_through_faceless_save` reads a ghost carrying no `traits` as a
    // save from a client that cannot see packs, keeps the pack, and copies only
    // the cue across — so choosing Ghost face, picking a motion and saving left
    // the crew wearing its pack with the motion gone. Pinning the face the pane
    // is SHOWING makes the change unambiguous, and it is the face the user was
    // looking at when they chose it.
    //
    // Only over a stored pack, and only with a reaction to carry: a ghost-to-ghost
    // save keeps the faceless spelling, where deriving the face from the name is
    // the point, and with nothing set at all the reset below is the honest answer
    // (an explicit `null` is a reset the backend honours on every tier, pack
    // included).
    if (savedPack && anyReaction) return { kind: 'ghost', traits: shown, ...reactions }
    // Judged on what is actually SET, never on `reactions`: a record whose only
    // content is two empty maps is not an override, and sending it as one would
    // reach the validator's all-empty collapse and be refused as junk. With
    // nothing set and no face pinned, the honest answer is the reset — which
    // deletes the whole record, cleared maps included.
    return anyReaction ? { kind: 'ghost', ...reactions } : null
  }

  /** One state's row on the Reactions tab. */
  const stateRow = (state: AvatarState) => {
    const ms = motionStateOf(state)
    // The default IS the current choice until the user overrides it, so the
    // select shows it rather than an empty trigger. A name from a newer
    // vocabulary has no option row, and falls back to itself in the trigger.
    const motion = ms ? (motions[ms] ?? MOTION_DEFAULTS[ms]) : ''
    const storedSound = sounds[state]
    // A stored `'none'` and an absent key are both silence, and the select
    // offers one option for that — so `'none'` shows as (and, on the next
    // Apply, normalizes to) the unset spelling.
    const selectedSound = storedSound && storedSound !== 'none' ? storedSound : ''
    return (
      <section
        key={state}
        className="rounded-lg border border-border p-3"
        aria-labelledby={`avatar-state-${state}-label`}
        data-testid={`avatar-state-row-${state}`}
      >
        <div className="mb-2 flex items-center justify-between gap-2">
          <span id={`avatar-state-${state}-label`} className="text-[12.5px] font-medium">
            {t(STATE_LABEL_KEYS[state])}
          </span>
          <button
            type="button"
            onClick={() => resetState(state)}
            className="text-[11px] text-muted underline underline-offset-2 hover:text-text"
            data-testid={`avatar-state-reset-${state}`}
          >
            {/* Named after the row it clears: the pane also carries the
                footer's whole-avatar reset, and two unscoped "Reset" controls on
                one surface read as one control nobody can predict. */}
            {t('components.avatarBuilder.react_reset_state', { state: t(STATE_LABEL_KEYS[state]) })}
          </button>
        </div>
        {/* Narrow-first: ONE column on a phone, where a 72px preview beside
            the selects leaves about 64px of trigger and every motion name
            truncates — the preview stacks above the controls instead, and both
            get the row's full width. The two-column shape starts at `sm`, and
            there the preview column is RESERVED on the working row (which has no
            preview) so all three rows' controls share one left edge instead of
            the working one starting 72px further in. */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[72px_minmax(0,1fr)]">
          {/* The preview goes through `CrewAvatar` rather than composing a URI
              here, so what the row shows is drawn by the same resolution the
              roster uses — including the default this select is displaying. */}
          {ms && (
            <div className="sm:col-start-1" data-testid={`avatar-state-preview-${state}`}>
              <CrewAvatar
                seed={name}
                avatar={{ kind: 'ghost', traits: shown, motions }}
                state={state}
                size={72}
                className="rounded-lg"
              />
            </div>
          )}
          {/* Nested grid for the same reason one level down: the play button
              lives in column two of the SOUND row only, and both selects sit in
              column one — so they are exactly as wide as each other rather than
              the sound one being short by a button. */}
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-end gap-2 sm:col-start-2">
            {ms && (
              <div
                className="col-start-1 flex min-w-0 flex-col gap-1"
                data-testid={`avatar-state-motion-${state}`}
              >
                <span className="text-[11px] text-muted">
                  {t('components.avatarBuilder.react_motion')}
                </span>
                <SimpleSelect
                  options={[...MOTION_NAMES[ms]]}
                  optionLabels={MOTION_NAMES[ms].map(nm => t(MOTION_LABEL_KEYS[nm]))}
                  value={motion}
                  triggerFallback={motion}
                  onChange={next => setStateMotion(ms, next)}
                  aria-label={`${t(STATE_LABEL_KEYS[state])} — ${t('components.avatarBuilder.react_motion')}`}
                  className="w-full"
                />
              </div>
            )}
            <>
              <div
                className="col-start-1 flex min-w-0 flex-col gap-1"
                data-testid={`avatar-state-sound-${state}`}
              >
                <span className="text-[11px] text-muted">{t('components.avatarBuilder.react_sound')}</span>
                {/* `clearLabel` IS the unset row: selecting it clears to '' and
                    the state's key is dropped, which is the same silence a
                    stored `'none'` means. */}
                <SimpleSelect
                  options={[...SOUND_PRESETS]}
                  optionLabels={SOUND_PRESETS.map(preset => t(SOUND_LABEL_KEYS[preset]))}
                  value={selectedSound}
                  onChange={next => setStateSound(state, next)}
                  clearLabel={t('components.avatarBuilder.sound_silent')}
                  aria-label={soundLabel(state)}
                  className="w-full"
                />
              </div>
              <Btn
                className="col-start-2"
                onClick={() => selectedSound && previewSound(selectedSound)}
                disabled={!selectedSound}
                aria-label={t('components.avatarBuilder.sound_preview')}
                title={t('components.avatarBuilder.sound_preview')}
                data-testid={`avatar-state-sound-preview-${state}`}
              >
                <Play size={13} aria-hidden="true" />
              </Btn>
            </>
          </div>
        </div>
      </section>
    )
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
          {/* Tier switch: hand-picked ghost vs uploaded picture, plus the
              reaction layer. Above every pane so switching never loses any
              side's in-progress state — the ghost draft, the pending picture
              and the per-state reactions live in separate state. Picking an
              identity tier also SETS the tier; the Reactions pane decorates the
              ghost, and is offered only while the ghost is the selected tier. */}
          {/* overflow-x-auto is the floor under `compact` below: compact clears
              every shipped locale at 320px, but the strip must stay reachable
              rather than clipped if a longer one ever lands. It shows no
              scrollbar while nothing overflows, which is every desktop width. */}
          <div className="mb-3 overflow-x-auto" data-testid="avatar-builder-tabs">
            <SegmentedControl
              segments={[
                {
                  key: 'face',
                  label: t('components.avatarBuilder.mode_face'),
                  icon: <Ghost size={13} aria-hidden="true" />,
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
                // Ghost-only: a picture cannot move and a pack moves from its
                // own files, so on those tiers there is nothing to author here.
                ...(tier === 'face'
                  ? [
                      {
                        key: 'reactions',
                        label: t('components.avatarBuilder.mode_reactions'),
                        icon: <Sparkles size={13} aria-hidden="true" />,
                      },
                    ]
                  : []),
              ]}
              value={pane}
              onChange={next => {
                const p = next as Pane
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
          </div>
          {pane === 'reactions' ? (
            <div className="flex flex-col gap-3" data-testid="avatar-reactions-pane">
              <p className="text-[11.5px] text-muted">{t('components.avatarBuilder.react_hint')}</p>
              {/* No inner scroller: the list is exactly three rows, and a
                  height cap on it cut the last row in half with no affordance to
                  say so. `DialogBody` already scrolls (`min-h-0 flex-1
                  overflow-y-auto`), so on a short window the whole pane scrolls
                  with a real scrollbar instead. The face pane keeps its own cap
                  because its grid is dozens of thumbnails, not three rows. */}
              <div className="flex flex-col gap-3">
                {AVATAR_STATES.map(stateRow)}
              </div>
            </div>
          ) : pane === 'pack' ? (
            <div className="flex flex-col gap-2">
              <CrewAvatarLibraryTab
                open={open}
                name={name}
                selectedId={packId}
                onSelect={setPackId}
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
                  className="rounded-md border border-warn/30 bg-warn-subtle px-2 py-1.5 text-[11px] text-warn"
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
                  className="rounded-md border border-warn/30 bg-warn-subtle px-2 py-1.5 text-[11px] text-warn"
                  data-testid="avatar-reactions-retired-cue-picture"
                >
                  {t('components.avatarBuilder.react_retired_cue_note_picture', { states: retiredCueLabel })}
                </p>
              )}
            </div>
          ) : (
          <div className="flex flex-col gap-4 md:flex-row">
            {/* Left: live preview + the whole-face view switch. Flip stays a
                toggle rather than a tab: it is a view transform of the same
                face, not a trait with its own vocabulary. Narrow-first: the
                columns stack below md so a phone gets the full width for the
                tab strip and the thumbnail grid. */}
            <div className="flex w-full flex-col items-center gap-3 md:w-[200px] md:flex-none">
              <img
                src={ghostDataUri(shown)}
                alt=""
                aria-hidden="true"
                width={176}
                height={176}
                className="rounded-xl border border-border"
                data-testid="avatar-builder-preview"
              />
              <div className="flex w-full flex-col gap-2 text-[12px]">
                <div className="flex items-center justify-between">
                  <span>{t('components.avatarBuilder.mirror')}</span>
                  <Toggle
                    checked={shown.flip}
                    onChange={v => setTrait({ flip: v })}
                    label={t('components.avatarBuilder.mirror')}
                  />
                </div>
              </div>
              <Btn onClick={randomize} className="w-full" data-testid="avatar-builder-randomize">
                <Dices className="lucide-inline" aria-hidden="true" />
                {t('components.avatarBuilder.randomize')}
              </Btn>
            </div>
            {/* Right: category tabs + the thumbnail grid. */}
            <div className="flex min-w-0 flex-1 flex-col gap-3">
              {/* Adaptive collapse stays ON (a phone width cannot fit seven
                  labeled tabs without clipping); the icons above are what
                  keep every axis visible in the collapsed compact form. */}
              <SegmentedControl segments={segments} value={axis} onChange={setAxis} layoutId="avatar-builder-axis" />
              <div className="grid max-h-[380px] grid-cols-[repeat(auto-fill,minmax(84px,1fr))] gap-2 overflow-y-auto pr-1" role="listbox" aria-label={t(AXIS_LABEL_KEYS[axis])}>
                {thumbs.map(({ key, uri }) => {
                  const selected =
                    axis === 'tile'
                      ? shown.tile === key
                      : axis === 'blush'
                        ? shown.blush === (key === 'blush')
                        : shown[axis] === key
                  return (
                    <button
                      key={key}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      aria-label={
                        axis === 'tile'
                          ? (TILE_LABEL_KEYS[key] ? t(TILE_LABEL_KEYS[key]) : key)
                          : optLabel(key)
                      }
                      onClick={() =>
                        setTrait(
                          axis === 'tile'
                            ? { tile: key }
                            : axis === 'blush'
                              ? { blush: key === 'blush' }
                              : { [axis]: key },
                        )
                      }
                      className={`flex flex-col items-center gap-1 rounded-lg border-2 p-1.5 pb-1 transition-colors ${
                        selected ? 'border-ring bg-accent-subtle' : 'border-transparent hover:bg-bg-hover'
                      }`}
                      data-testid={`avatar-opt-${key.replace('#', '')}`}
                    >
                      <img src={uri} alt="" aria-hidden="true" width={72} height={72} className="rounded-lg" />
                      <span className={`text-[10.5px] leading-tight ${selected ? 'text-text-strong' : 'text-muted'}`}>
                        {/* Tile options carry no text label: the swatch IS the
                            meaning, and a raw hex code is noise to a person
                            (first-run review, Medium #7). The hex still reaches
                            AT users via the aria-label. */}
                        {axis === 'tile' ? '\u00a0' : optLabel(key)}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
          )}
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
            <button
              type="button"
              onClick={() => {
                pickGen.current += 1
                setDraft(null)
                setPending(null)
                setPackId(null)
                // The default face has no reactions either: this link is the
                // one control that means "everything back to the default" — and
                // the label says both halves out loud, because a link promising
                // only the face while also erasing two motions and three sounds
                // is a control the user cannot predict.
                setMotions({})
                setSounds({})
                setTier('face')
                setPane('face')
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
