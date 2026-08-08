/**
 * CrewCompanion - Pack Editor
 *
 * WYSIWYG editor for creating and editing custom SVG/Lottie appearance packs.
 */
import { ArrowLeft, CornerDownRight, Film, FolderOpen, Loader2, Plus, X } from 'lucide-react'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import type { PackMeta } from './appearanceTypes'
import { REQUIRED_STATES, ALL_MOODS, OPTIONAL_STATES, STATUS_STATES, BREATHING_STATES, RANDOM_STATES, } from './appearanceTypes'
import { toDataUri } from './animationResolver'
import { PackInfoHeader } from './PackInfoHeader'
import { SaveDialog } from './SaveDialog'
import { EditorFooter } from './EditorFooter'
import { useLang, useSaveWithDialog } from './editorHooks'
import { PANEL_RADIUS } from './panelSkin'
import { galleryApi } from './petBridge'
import { i18nT } from '../../i18n/t'
import type { CSSProperties } from 'react'
import { entryContent, entryFormat } from './packAnimation'
import { DRAG_REGION, NO_DRAG_REGION } from './appRegion'
import { slotLabel } from './slotLabel'
import { errorText } from './errorText'
import './gallery.css'

const api = galleryApi

// ── Types ──────────────────────────────────────────────────────────────────

export interface PackEditorProps {
  /** When editing an existing pack; undefined for create mode */
  existingPack?: PackMeta
  onSave: (pack: PackMeta) => void
  onCancel: () => void
}

interface SlotData {
  content: string
  format: 'svg' | 'lottie' | 'sprite'
  filename: string
}

type Slots = Record<string, SlotData | null>

// All slot keys in display order
const STATE_KEYS = [...REQUIRED_STATES] as string[]
const OPTIONAL_KEYS = [...OPTIONAL_STATES] as string[]
const MOOD_KEYS = [...ALL_MOODS] as string[]
const ALL_SLOT_KEYS = [...STATE_KEYS, ...OPTIONAL_KEYS, ...MOOD_KEYS]

// Editor sections come straight from shared/appearanceTypes so the editor, the pack
// detail view and the resolver all agree on the categories:
//  • Status — the three signals the app actually produces (done / error / loading).
//  • Random — spontaneous behaviour, plus clips the author names themselves.
// Dropped as slots: 'offline' and the five fixed moods (a mood the author drew for
// random play was also being used as a status reaction), and 'thinking'/'working',
// which are now legacy aliases of 'loading'.
const STATUS_DISPLAY = [...STATUS_STATES] as string[]
/** Slot-id prefix for extra clips. An identifier, not display text. */
const EXTRA_SLOT_PREFIX = 'extra_load_'

const RANDOM_FIXED = [...RANDOM_STATES] as string[]
const BREATHING_DISPLAY = [...BREATHING_STATES] as string[]
// Labels come from the shared map, so the editor and the detail view name the
// same slot the same way.
/**
 * Slot display names, translated.
 *
 * The desktop app kept an English map here in front of the `state.*` keys; keys alone
 * are translatable, so the map is gone and the lookup is the single source. Falls back
 * to the raw slot id, which is how a pack declaring a mood the build does not know
 * still shows something meaningful.
 */

// ── Styles ─────────────────────────────────────────────────────────────────

const S = {
  titleBar: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '8px 10px',
    borderBottom: '1px solid var(--border)',
    flexShrink: 0,
    // Drag the window by this bar. Children that take clicks opt out below.
    ...DRAG_REGION,
  },
  backBtn: {
    background: 'none',
    border: 'none',
    color: 'var(--text-muted)',
    cursor: 'pointer',
    fontSize: 12,
    padding: '5px 8px',
    borderRadius: 6,
    ...NO_DRAG_REGION,
  },
  root: {
    width: '100%',
    height: '100%',
    display: 'flex',
    flexDirection: 'column',
    // Editor mode replaces the gallery CARD, so it has to carry the card's own
    // rounded corners and shadow -- without these it rendered as a square
    // full-bleed panel where a rounded one had been.
    borderRadius: PANEL_RADIUS.card,
    overflow: 'hidden' as const,
    boxShadow: 'var(--cc-card-shadow)',
    background: 'var(--bg)',
    color: 'var(--text)',
    fontFamily: 'var(--cc-font)',
  },
  body: {
    flex: 1,
    overflowY: 'auto',
    padding: 20,
  },
  sectionLabel: {
    fontSize: 11,
    color: 'var(--text-muted)',
    textTransform: 'uppercase',
    letterSpacing: 1,
    marginBottom: 8,
    marginTop: 4,
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))',
    gap: 10,
    marginBottom: 16,
  },
  slot: (filled: boolean): CSSProperties => ({
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 4,
    padding: 8,
    borderRadius: 10,
    border: `1px dashed ${filled ? 'var(--accent)' : 'var(--border)'}`,
    background: filled ? 'var(--bg-elevated)' : 'var(--cc-input-bg)',
    cursor: 'pointer',
    transition: 'border-color 150ms, background 150ms',
    position: 'relative',
    minHeight: 100,
  }),
  slotThumb: {
    width: 56,
    height: 56,
    objectFit: 'contain',
    borderRadius: 6,
  },
  slotPlaceholder: {
    width: 56,
    height: 56,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: 6,
    background: 'var(--bg)',
    color: 'var(--text-faint)',
    fontSize: 20,
    border: '1px solid var(--border)',
  },
  slotLabel: {
    fontSize: 10,
    color: 'var(--text-muted)',
    textAlign: 'center',
    fontWeight: 500,
  },
  optionalTag: {
    fontSize: 9,
    color: 'var(--text-faint)',
    background: 'var(--bg)',
    borderRadius: 4,
    padding: '1px 5px',
    border: '1px solid var(--border)',
  },
  // Popover for slot actions
  popover: {
    position: 'fixed',
    zIndex: 1000,
    background: 'var(--bg-elevated)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    boxShadow: '0 4px 16px var(--shadow)',
    minWidth: 180,
    padding: 4,
    whiteSpace: 'nowrap',
  },
  popoverItem: {
    display: 'block',
    width: '100%',
    padding: '6px 12px',
    border: 'none',
    background: 'transparent',
    color: 'var(--text)',
    fontSize: 12,
    textAlign: 'left',
    cursor: 'pointer',
    borderRadius: 4,
  },
  popoverDivider: {
    height: 1,
    background: 'var(--border)',
    margin: '4px 0',
  },
  popoverSub: {
    padding: '4px 12px',
    fontSize: 10,
    color: 'var(--text-faint)',
  },
  error: {
    padding: '10px 14px',
    borderRadius: 8,
    background: 'rgba(239,83,80,0.1)',
    border: '1px solid rgba(239,83,80,0.3)',
    color: 'var(--danger)',
    fontSize: 12,
    marginBottom: 12,
  },
} satisfies Record<string, CSSProperties | ((filled: boolean) => CSSProperties)>


// ── Slot Thumbnail ─────────────────────────────────────────────────────────

function SlotThumbnail({ data }: { data: SlotData | null }) {
  if (!data) {
    return <div style={S.slotPlaceholder}><Plus size={20} aria-hidden="true" /></div>
  }
  if (entryFormat(data) === 'svg') {
    return <img src={toDataUri(entryContent(data))} alt="" style={S.slotThumb} />
  }
  // Lottie placeholder
  return (
    <div style={{
      ...S.slotPlaceholder,
      background: 'var(--bg-elevated)',
      fontSize: 16,
    }}>
      <Film className="lucide-inline" aria-hidden="true" />
    </div>
  )
}

// ── Slot Popover ───────────────────────────────────────────────────────────

function SlotPopover({ slotKey, slots, filled, anchorRect, onSelectFile, onUseSameAs, onClear, onClose, i18nT }: {
  slotKey: string
  slots: Slots
  filled: boolean
  anchorRect: { top: number; left: number; width: number; height: number }
  onSelectFile: () => void
  onUseSameAs: (sourceKey: string) => void
  onClear: () => void
  onClose: () => void
  i18nT: (key: string, vars?: Record<string, string>) => string
}) {
  const popoverRef = useRef<HTMLDivElement>(null)

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  // Find other slots that have content (for "use same as" dropdown)
  const filledOthers = ALL_SLOT_KEYS.filter(
    (k) => k !== slotKey && slots[k] !== null
  )

  // Position below the anchor, flip up if near bottom, keep within window
  const spaceBelow = window.innerHeight - (anchorRect.top + anchorRect.height)
  const flipUp = spaceBelow < 200
  const popW = 180 // minWidth from S.popover
  let left = anchorRect.left + anchorRect.width / 2 - popW / 2
  left = Math.max(4, Math.min(left, window.innerWidth - popW - 4))
  const popStyle: React.CSSProperties = {
    ...S.popover,
    top: flipUp ? undefined : anchorRect.top + anchorRect.height + 4,
    bottom: flipUp ? window.innerHeight - anchorRect.top + 4 : undefined,
    left,
  }

  return (
    <div role="presentation" ref={popoverRef} style={popStyle} onClick={(e) => e.stopPropagation()}>
      <button
        style={S.popoverItem}
        onMouseEnter={(e) => { (e.target as HTMLElement).style.background = 'var(--cc-input-bg)' }}
        onMouseLeave={(e) => { (e.target as HTMLElement).style.background = 'transparent' }}
        onClick={() => { onSelectFile(); onClose() }}
      >
        <FolderOpen className="lucide-inline" aria-hidden="true" />{' '}
        {i18nT('apps.crewCompanion.editor.selectFile')}
      </button>

      {filledOthers.length > 0 && (
        <>
          <div style={S.popoverDivider} />
          <div style={S.popoverSub}>{i18nT('apps.crewCompanion.editor.useSameAs')}</div>
          {filledOthers.map((k) => (
            <button
              key={k}
              style={S.popoverItem}
              onMouseEnter={(e) => { (e.target as HTMLElement).style.background = 'var(--cc-input-bg)' }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.background = 'transparent' }}
              onClick={() => { onUseSameAs(k); onClose() }}
            >
              <CornerDownRight size={11} className="lucide-inline" aria-hidden="true" />{' '}{slotLabel(k)}
            </button>
          ))}
        </>
      )}

      {filled && (
        <>
          <div style={S.popoverDivider} />
          <button
            style={{ ...S.popoverItem, color: 'var(--danger)' }}
            onMouseEnter={(e) => { (e.target as HTMLElement).style.background = 'var(--cc-input-bg)' }}
            onMouseLeave={(e) => { (e.target as HTMLElement).style.background = 'transparent' }}
            onClick={() => { onClear(); onClose() }}
          >
            {i18nT('apps.crewCompanion.editor.clear')}
          </button>
        </>
      )}
    </div>
  )
}


// ── Main PackEditor Component ──────────────────────────────────────────────

export const PackEditor: React.FC<PackEditorProps> = ({ existingPack, onSave, onCancel }) => {
  const [name, setName] = useState(existingPack?.name ?? '')
  const [author, setAuthor] = useState(existingPack?.author ?? '')
  const [description, setDescription] = useState(existingPack?.description ?? '')
  const [flipX, setFlipX] = useState(false)
  useLang()
  const [slots, setSlots] = useState<Slots>(() => {
    const init: Slots = {}
    for (const k of ALL_SLOT_KEYS) init[k] = null
    return init
  })
  // Open-ended "random" extras: user-named clips (slot stored under `id`).
  const [extras, setExtras] = useState<Array<{ id: string; name: string }>>([])
  const originalSnapshot = useRef<string | null>(null)
  const slotSnap = () => JSON.stringify({ name, author, description, flipX, slots, extras })
  const isDirty = !existingPack || originalSnapshot.current === null || slotSnap() !== originalSnapshot.current
  const { showSaveDialog, triggerSave, confirmOverwrite, confirmSaveNew, cancelDialog } = useSaveWithDialog(existingPack, isDirty)

  const addExtra = () => setExtras((xs) => [...xs, { id: `extra_${Date.now()}_${xs.length}`, name: '' }])
  const renameExtra = (id: string, nm: string) => setExtras((xs) => xs.map((x) => (x.id === id ? { ...x, name: nm } : x)))
  const removeExtra = (id: string) => {
    setExtras((xs) => xs.filter((x) => x.id !== id))
    setSlots((s) => { const n = { ...s }; delete n[id]; return n })
  }
  const [activePopover, setActivePopover] = useState<string | null>(null)
  const [popoverAnchor, setPopoverAnchor] = useState<{ top: number; left: number; width: number; height: number } | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadingSlot, setLoadingSlot] = useState<string | null>(null)

  // ── Edit mode: pre-fill slots from existing pack ───────────

  useEffect(() => {
    if (!existingPack) return
    let cancelled = false
    ;(async () => {
      try {
        const detail = await api.galleryGetPackDetail(existingPack.id)
        if (cancelled || !detail?.animations) return
        const animations = detail.animations
        const filled: Slots = {}
        for (const k of ALL_SLOT_KEYS) {
          const anim = detail.animations[k]
          if (anim) {
            filled[k] = {
              content: entryContent(anim),
              format: entryFormat(anim),
              filename: `${k}.${entryFormat(anim) === 'lottie' ? 'json' : entryFormat(anim) === 'sprite' ? 'png' : 'svg'}`,
            }
          } else {
            filled[k] = null
          }
        }
        // Load open-ended random extras (arbitrary user-named clips).
        const loadedExtras: Array<{ id: string; name: string }> = []
        const rn: string[] = Array.isArray(detail.randomNames) ? detail.randomNames : []
        rn.forEach((nm, i) => {
          const anim = animations[nm]
          if (anim) {
            const id = EXTRA_SLOT_PREFIX + i
            filled[id] = {
              content: entryContent(anim),
              format: entryFormat(anim),
              filename: `${id}.${entryFormat(anim) === 'lottie' ? 'json' : entryFormat(anim) === 'sprite' ? 'png' : 'svg'}`,
            }
            loadedExtras.push({ id, name: nm })
          }
        })
        setSlots(filled)
        setExtras(loadedExtras)
        // Snapshot for dirty check
        originalSnapshot.current = JSON.stringify({
          name: existingPack.name ?? '', author: existingPack.author ?? '',
          description: existingPack.description ?? '', flipX: false, slots: filled, extras: loadedExtras,
        })
      } catch (err) {
        setError(errorText(err) || i18nT('apps.crewCompanion.editor.loadDataFailed'))
      }
    })()
    return () => { cancelled = true }
  }, [existingPack])

  // ── Slot interactions ──────────────────────────────────────

  const handleSlotClick = useCallback(
    (key: string, e: React.MouseEvent | React.KeyboardEvent) => {
    if (activePopover === key) {
      setActivePopover(null)
      setPopoverAnchor(null)
    } else {
      const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
      setPopoverAnchor({ top: rect.top, left: rect.left, width: rect.width, height: rect.height })
      setActivePopover(key)
    }
  }, [activePopover])

  const handleSelectFile = useCallback(async (key: string) => {
    setLoadingSlot(key)
    setError(null)
    try {
      const result = await api.galleryImportFile()
      if (!result) {
        // User cancelled the file picker
        setLoadingSlot(null)
        return
      }
      if (result.ok === false) {
        setError(result.error || i18nT('apps.crewCompanion.editor.invalidFile'))
        setLoadingSlot(null)
        return
      }
      const { content, filename, format } = (result.value ?? result) as {
        content: string
        filename: string
        format: SlotData['format']
      }
      setSlots((prev) => ({
        ...prev,
        [key]: { content, filename, format },
      }))
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.editor.importFileFailed'))
    }
    setLoadingSlot(null)
  }, [])

  const handleUseSameAs = useCallback((targetKey: string, sourceKey: string) => {
    setSlots((prev) => {
      const source = prev[sourceKey]
      if (!source) return prev
      return { ...prev, [targetKey]: { ...source } }
    })
  }, [])

  const handleClear = useCallback((key: string) => {
    setSlots((prev) => ({ ...prev, [key]: null }))
  }, [])

  // ── Save logic ─────────────────────────────────────────────

  const missingStates = STATE_KEYS.filter((k) => !slots[k])
  const canSave = missingStates.length === 0 && name.trim().length > 0 && isDirty

  const doSave = useCallback(async (asNew: boolean) => {
    if (!canSave || saving) return
    setSaving(true)
    setError(null)

    try {
      const statesData: Record<string, string> = {}
      const moodsData: Record<string, string> = {}

      for (const k of STATE_KEYS) {
        const slot = slots[k]
        if (slot) statesData[k] = entryContent(slot)
      }
      for (const k of OPTIONAL_KEYS) {
        const slot = slots[k]
        if (slot) statesData[k] = entryContent(slot)
      }
      for (const k of MOOD_KEYS) {
        const slot = slots[k]
        if (slot) moodsData[k] = entryContent(slot)
      }

      // Open-ended random extras → { name: content }. Names key the map, so
      // two extras sharing a trimmed name would silently overwrite the first
      // one's artwork — the save "succeeded" and the clip was gone. Refuse
      // instead: the user renames one and keeps both.
      const randomData: Record<string, string> = {}
      for (const x of extras) {
        const nm = x.name.trim()
        const slot = slots[x.id]
        if (!nm || !slot?.content) continue
        if (nm in randomData) {
          setError(i18nT('apps.crewCompanion.editor.duplicateExtraName', { name: nm }))
          setSaving(false)
          return
        }
        if (ALL_SLOT_KEYS.includes(nm)) {
          // A clip named after a BUILT-IN slot is ambiguous in the data model:
          // the detail payload keys animations by slot name, so a random
          // "idle" and the state "idle" collide — the save permanently
          // replaced the state's artwork with the clip's.
          setError(i18nT('apps.crewCompanion.editor.reservedExtraName', { name: nm }))
          setSaving(false)
          return
        }
        randomData[nm] = entryContent(slot)
      }

      const idleSlot = slots['idle']
      const format = idleSlot?.format ?? 'svg'

      const packData = {
        meta: {
          // A pack needs a real id before any request: gallerySavePack refuses an
          // empty one. Mint an id whenever there is no pack to overwrite — that is
          // BOTH an explicit "save as new" (`asNew`) AND, crucially, a brand-new pack
          // from "Make your own", where `existingPack` is undefined. An earlier fix
          // minted only on `asNew`, but `triggerSave` sends a first-time save through
          // `doSave(false)` (there is no existing pack to prompt an overwrite against),
          // so new packs hit the `''` branch and every save failed with "needs a name".
          id: asNew || !existingPack ? crypto.randomUUID() : existingPack.id,
          name: name.trim(),
          author: author.trim() || i18nT('apps.crewCompanion.editor.unknownAuthor'),
          description: description.trim(),
          format,
        },
        states: statesData,
        moods: moodsData,
        random: randomData,
      }

      const result = await api.gallerySavePack(packData)
      if (result && result.ok === false) {
        setError(result.error || i18nT('apps.crewCompanion.editor.saveFailed'))
        setSaving(false)
        return
      }

      const savedMeta = (result?.value ?? result) as PackMeta
      onSave(savedMeta)
    } catch (err) {
      setError(errorText(err) || i18nT('apps.crewCompanion.editor.saveFailed'))
    }
    setSaving(false)
  }, [canSave, saving, slots, name, author, description, existingPack, onSave])

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div style={S.root}>
      {/*
        Title bar. The editor is an overlay covering the gallery header, which is
        where the window's only `-webkit-app-region: drag` region lives — so this bar
        carries its own, otherwise the window becomes unmovable while editing. The
        back button must opt out of it, since a drag region swallows clicks.
      */}
      <div style={S.titleBar}>
        <button
          style={S.backBtn}
          onClick={onCancel}
          title={i18nT('apps.crewCompanion.editor.back')}
          aria-label={i18nT('apps.crewCompanion.editor.back')}
        >
          <ArrowLeft size={12} className="lucide-inline" aria-hidden="true" />{' '}{i18nT('apps.crewCompanion.editor.back')}
        </button>
      </div>
      <PackInfoHeader
        title={existingPack ? i18nT('apps.crewCompanion.editor.editTitle') : i18nT('apps.crewCompanion.editor.createTitle')}
        name={name} author={author} description={description} flipX={flipX}
        onNameChange={setName} onAuthorChange={setAuthor} onDescriptionChange={setDescription} onFlipXChange={setFlipX}
        tt={i18nT}
      />

      {/* Body: state grid */}
      <div style={S.body}>
        {error && (
          <div style={S.error}>
            {error}
            <button
              onClick={() => setError(null)}
              style={{ float: 'right', background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', fontSize: 14 }}
              aria-label={i18nT('apps.crewCompanion.panel.close')}
            ><X size={14} aria-hidden="true" /></button>
          </div>
        )}

        {/* Required states */}
        <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.editor.requiredStates')}</div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.crewCompanion.editor.sizeHint')}</div>
        <div style={S.grid}>
          {STATE_KEYS.map((key) => (
            <div
              role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSlotClick(key, e) } }}
              key={key}
              style={S.slot(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}>
                      <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
                    </div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>{slotLabel(key)}</div>
            </div>
          ))}
        </div>

        {/* Status — reactive to what the agent is doing */}
        <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.state.groupStatus')} <span style={S.optionalTag}>{i18nT('apps.crewCompanion.editor.optional')}</span></div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.crewCompanion.editor.statusHint')}</div>
        <div style={S.grid}>
          {STATUS_DISPLAY.map((key) => (
            <div
              role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSlotClick(key, e) } }}
              key={key}
              style={S.slot(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}>
                      <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
                    </div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>
                {slotLabel(key)} <span style={S.optionalTag}>{i18nT('apps.crewCompanion.editor.optional')}</span>
              </div>
            </div>
          ))}
        </div>

        {/* Breathing — one drawing per phase of the guided exercise */}
        <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.state.groupBreathing')} <span style={S.optionalTag}>{i18nT('apps.crewCompanion.editor.optional')}</span></div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.crewCompanion.editor.breathingHint')}</div>
        <div style={S.grid}>
          {BREATHING_DISPLAY.map((key) => (
            <div
              role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSlotClick(key, e) } }}
              key={key}
              style={S.slot(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}>
                      <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
                    </div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>
                {slotLabel(key) || key} <span style={S.optionalTag}>{i18nT('apps.crewCompanion.editor.optional')}</span>
              </div>
            </div>
          ))}
        </div>

        {/* Random — spontaneous behaviours (walking + moods + your own clips) */}
        <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.state.groupRandom')} <span style={S.optionalTag}>{i18nT('apps.crewCompanion.editor.optional')}</span></div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.crewCompanion.editor.hint8152')}</div>
        <div style={S.grid}>
          {RANDOM_FIXED.map((key) => (
            <div
              role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSlotClick(key, e) } }}
              key={key}
              style={S.slot(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}>
                      <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
                    </div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>
                {slotLabel(key)} <span style={S.optionalTag}>{i18nT('apps.crewCompanion.editor.optional')}</span>
              </div>
            </div>
          ))}
          {extras.map((x) => (
            <div key={x.id} style={{ ...S.slot(!!slots[x.id]), position: 'relative' }}>
              <button
                onClick={(e) => { e.stopPropagation(); removeExtra(x.id) }}
                aria-label={i18nT('apps.crewCompanion.editor.remove')}
                title={i18nT('apps.crewCompanion.editor.remove')}
                style={{ position: 'absolute', top: 2, right: 4, background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 12, lineHeight: 1, padding: 0, zIndex: 1 }}
              ><X size={12} aria-hidden="true" /></button>
              <div role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSlotClick(x.id, e) } }} onClick={(e) => handleSlotClick(x.id, e)} style={{ cursor: 'pointer' }}>
                {loadingSlot === x.id ? (
                  <div style={S.slotPlaceholder}>
                      <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
                    </div>
                ) : (
                  <SlotThumbnail data={slots[x.id]} />
                )}
              </div>
              <input
                value={x.name}
                onChange={(e) => renameExtra(x.id, e.target.value)}
                onClick={(e) => e.stopPropagation()}
                placeholder="name"
                style={{ width: '100%', boxSizing: 'border-box', marginTop: 4, background: 'var(--cc-input-bg)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)', fontSize: 10, padding: '2px 4px', textAlign: 'center', outline: 'none' }}
              />
            </div>
          ))}
          <div
              role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); addExtra() } }}
            onClick={addExtra}
            style={{ ...S.slot(false), cursor: 'pointer', display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center' }}
          >
            <Plus size={20} aria-hidden="true" style={{ color: 'var(--text-muted)' }} />
            <div style={S.slotLabel}>{i18nT('apps.crewCompanion.editor.addClip')}</div>
          </div>
        </div>
      </div>

      {/* Popover rendered at root level with fixed positioning */}
      {activePopover && popoverAnchor && (
        <SlotPopover
          slotKey={activePopover}
          slots={slots}
          filled={!!slots[activePopover]}
          anchorRect={popoverAnchor}
          onSelectFile={() => handleSelectFile(activePopover)}
          onUseSameAs={(src) => handleUseSameAs(activePopover, src)}
          onClear={() => handleClear(activePopover)}
          onClose={() => { setActivePopover(null); setPopoverAnchor(null) }}
          i18nT={i18nT}
        />
      )}

      <EditorFooter
        missingStates={missingStates}
        canSave={canSave}
        saving={saving}
        onCancel={onCancel}
        onSave={() => triggerSave(doSave)}
        tt={i18nT}
      />
      <SaveDialog
        visible={showSaveDialog}
        onOverwrite={() => confirmOverwrite(doSave)}
        onSaveNew={() => confirmSaveNew(doSave)}
        onCancel={cancelDialog}
        i18nT={i18nT}
      />
    </div>
  )
}
