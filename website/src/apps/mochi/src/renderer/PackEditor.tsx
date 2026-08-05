/**
 * Mochi - Pack Editor
 *
 * WYSIWYG editor for creating and editing custom SVG/Lottie appearance packs.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  CornerDownRight,
  Film,
  FolderOpen,
  LoaderCircle,
  Palette,
  Pencil,
  Plus,
  X,
} from 'lucide-react'
import type { PackMeta } from '../shared/appearanceTypes'
import { REQUIRED_STATES, ALL_MOODS, OPTIONAL_STATES } from '../shared/appearanceTypes'
import { toDataUri } from './animationResolver'
import { PackInfoHeader } from './PackInfoHeader'
import { SaveDialog } from './SaveDialog'
import { EditorFooter } from './EditorFooter'
import { useSaveWithDialog } from './editorHooks'

import { api } from '../mochiApi'
import { i18nT } from '../../../../i18n/t'
import { moodLabel, slotLabel, stateLabel } from '../../i18nKeys'

// ── Types ──────────────────────────────────────────────────────────────────

export interface PackEditorProps {
  /** When editing an existing pack; undefined for create mode */
  existingPack?: PackMeta
  onSave: (pack: PackMeta) => void
  onCancel: () => void
}

interface SlotData {
  content: string
  format: 'svg' | 'lottie'
  filename: string
}

type Slots = Record<string, SlotData | null>

// All slot keys in display order
const STATE_KEYS = [...REQUIRED_STATES] as string[]
const OPTIONAL_KEYS = [...OPTIONAL_STATES] as string[]
const MOOD_KEYS = [...ALL_MOODS] as string[]
const ALL_SLOT_KEYS = [...STATE_KEYS, ...OPTIONAL_KEYS, ...MOOD_KEYS]

// ── Styles ─────────────────────────────────────────────────────────────────

  const slotStyle = (filled: boolean): React.CSSProperties => ({
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 4,
    padding: 8,
    borderRadius: 10,
    border: `1px dashed ${filled ? 'var(--accent)' : 'var(--border)'}`,
    background: filled ? 'var(--bg-elevated)' : 'var(--bg-input)',
    cursor: 'pointer',
    transition: 'border-color 150ms, background 150ms',
    position: 'relative',
    minHeight: 100,
  })

const S: Record<string, React.CSSProperties> = {
  root: {
    width: '100%',
    height: '100%',
    display: 'flex',
    flexDirection: 'column',
    background: 'var(--bg)',
    color: 'var(--text)',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    overflow: 'hidden',
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
    display: 'flex',
    alignItems: 'center',
    gap: 6,
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
}


// ── Slot Thumbnail ─────────────────────────────────────────────────────────

function SlotThumbnail({ data }: { data: SlotData | null }) {
  if (!data) {
    return <div style={S.slotPlaceholder}><Plus size={14} /></div>
  }
  if (data.format === 'svg') {
    return <img src={toDataUri(data.content)} alt="" style={S.slotThumb} />
  }
  // Lottie placeholder
  return (
    <div style={{
      ...S.slotPlaceholder,
      background: 'var(--bg-elevated)',
      color: 'var(--text-muted)',
    }}>
      <Film size={18} />
    </div>
  )
}

// ── Slot Popover ───────────────────────────────────────────────────────────

function SlotPopover({ slotKey, slots, filled, anchorRect, onSelectFile, onUseSameAs, onClear, onClose }: {
  slotKey: string
  slots: Slots
  filled: boolean
  anchorRect: { top: number; left: number; width: number; height: number }
  onSelectFile: () => void
  onUseSameAs: (sourceKey: string) => void
  onClear: () => void
  onClose: () => void
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
    // The popover is a menu of actions, so it carries menu semantics rather
    // than being a bare click-catching div: without a role a screen reader
    // announces the buttons with no grouping, and the repo rule (rightly)
    // blocks a div that handles clicks but names no role. The onClick only
    // contains the event so the outside-click closer does not fire.
    <div
      ref={popoverRef}
      role="menu"
      aria-label={slotLabel(slotKey)}
      style={popStyle}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        style={S.popoverItem}
        role="menuitem"
        onMouseEnter={(e) => { (e.target as HTMLElement).style.background = 'var(--bg-input)' }}
        onMouseLeave={(e) => { (e.target as HTMLElement).style.background = 'transparent' }}
        onClick={() => { onSelectFile(); onClose() }}
      >
        <FolderOpen size={13} /> {i18nT('apps.mochi.editor.select_file')}
      </button>

      {filledOthers.length > 0 && (
        <>
          <div style={S.popoverDivider} role="separator" />
          <div style={S.popoverSub}>{i18nT('apps.mochi.editor.use_same_as')}</div>
          {filledOthers.map((k) => (
            <button
              key={k}
              style={S.popoverItem}
              role="menuitem"
              onMouseEnter={(e) => { (e.target as HTMLElement).style.background = 'var(--bg-input)' }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.background = 'transparent' }}
              onClick={() => { onUseSameAs(k); onClose() }}
            >
              <CornerDownRight size={13} style={{ flexShrink: 0 }} /> {slotLabel(k)}
            </button>
          ))}
        </>
      )}

      {filled && (
        <>
          <div style={S.popoverDivider} role="separator" />
          <button
            style={{ ...S.popoverItem, color: 'var(--danger)' }}
            role="menuitem"
            onMouseEnter={(e) => { (e.target as HTMLElement).style.background = 'var(--bg-input)' }}
            onMouseLeave={(e) => { (e.target as HTMLElement).style.background = 'transparent' }}
            onClick={() => { onClear(); onClose() }}
          >
            <X size={12} style={{ marginRight: 4, verticalAlign: '-2px' }} />
            {i18nT('apps.mochi.editor.clear')}
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
  const [slots, setSlots] = useState<Slots>(() => {
    const init: Slots = {}
    for (const k of ALL_SLOT_KEYS) init[k] = null
    return init
  })
  const originalSnapshot = useRef<string | null>(null)
  const slotSnap = () => JSON.stringify({ name, author, description, flipX, slots })
  const isDirty = !existingPack || originalSnapshot.current === null || slotSnap() !== originalSnapshot.current
  const { showSaveDialog, triggerSave, confirmOverwrite, confirmSaveNew, cancelDialog } = useSaveWithDialog(existingPack, isDirty)
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
        const filled: Slots = {}
        for (const k of ALL_SLOT_KEYS) {
          const anim = detail.animations[k]
          // This editor edits SVG/Lottie slots only. A sprite pack is authored
          // in SpriteImporter (one sheet + row assignments), so a sprite slot
          // has no per-slot document to load here — treat it as absent rather
          // than mislabelling the sheet as an `.svg`.
          if (anim && anim.format !== 'sprite') {
            filled[k] = {
              content: anim.content,
              format: anim.format,
              filename: `${k}.${anim.format === 'lottie' ? 'json' : 'svg'}`,
            }
          } else {
            filled[k] = null
          }
        }
        setSlots(filled)
        // Snapshot for dirty check
        originalSnapshot.current = JSON.stringify({
          name: existingPack.name ?? '', author: existingPack.author ?? '',
          description: existingPack.description ?? '', flipX: false, slots: filled,
        })
      } catch (err: any) {
        setError(err?.message || i18nT('apps.mochi.errors.load_pack_data'))
      }
    })()
    return () => { cancelled = true }
  }, [existingPack])

  // ── Slot interactions ──────────────────────────────────────

  // Accepts a keyboard event too: the anchor comes from `currentTarget`'s rect, not
  // from pointer coordinates, so Enter/Space on a focused slot positions the popover
  // exactly as a click does.
  const handleSlotClick = useCallback((key: string, e: React.MouseEvent | React.KeyboardEvent) => {
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
        setError(result.error || i18nT('apps.mochi.errors.invalid_file'))
        setLoadingSlot(null)
        return
      }
      const { content, filename, format } = result.value ?? result
      setSlots((prev) => ({
        ...prev,
        [key]: { content, filename, format },
      }))
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.import_file'))
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
        if (slot) statesData[k] = slot.content
      }
      for (const k of OPTIONAL_KEYS) {
        const slot = slots[k]
        if (slot) statesData[k] = slot.content
      }
      for (const k of MOOD_KEYS) {
        const slot = slots[k]
        if (slot) moodsData[k] = slot.content
      }

      const idleSlot = slots['idle']
      const format = idleSlot?.format ?? 'svg'

      const packData = {
        meta: {
          id: asNew ? '' : (existingPack?.id ?? ''),
          name: name.trim(),
          author: author.trim() || i18nT('apps.mochi.editor.author_unknown'),
          description: description.trim(),
          format,
        },
        states: statesData,
        moods: moodsData,
      }

      const result = await api.gallerySavePack(packData)
      if (result && result.ok === false) {
        setError(result.error || i18nT('apps.mochi.errors.save'))
        setSaving(false)
        return
      }

      const savedMeta: PackMeta = result?.value ?? result
      onSave(savedMeta)
    } catch (err: any) {
      setError(err?.message || i18nT('apps.mochi.errors.save'))
    }
    setSaving(false)
  }, [canSave, saving, slots, name, author, description, existingPack, onSave])

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div style={S.root}>
      <PackInfoHeader
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            {existingPack ? <Pencil size={13} /> : <Palette size={13} />}
            {existingPack ? i18nT('apps.mochi.editor.edit_title') : i18nT('apps.mochi.editor.create_title')}
          </span>
        }
        name={name} author={author} description={description} flipX={flipX}
        onNameChange={setName} onAuthorChange={setAuthor} onDescriptionChange={setDescription} onFlipXChange={setFlipX}

      />

      {/* Body: state grid */}
      <div style={S.body}>
        {error && (
          <div style={S.error}>
            {error}
            <button
              onClick={() => setError(null)}
              aria-label={i18nT('apps.mochi.chatPanel.dismiss')}
              style={{ float: 'right', background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', fontSize: 14 }}
            ><X size={13} /></button>
          </div>
        )}

        {/* Required states */}
        <div style={S.sectionLabel}>{i18nT('apps.mochi.editor.required_states')}</div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.mochi.editor.size_hint')}</div>
        <div style={S.grid}>
          {STATE_KEYS.map((key) => (
            <div
              key={key}
              role="button"
              tabIndex={0}
              aria-label={slotLabel(key)}
              style={slotStyle(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
              onKeyDown={(e) => {
                if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
                  e.preventDefault()
                  handleSlotClick(key, e)
                }
              }}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}><LoaderCircle size={16} className="lucide-inline" /></div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>{stateLabel(key)}</div>
            </div>
          ))}
        </div>

        {/* Optional peek states */}
        <div style={S.sectionLabel}>
          {i18nT('apps.mochi.editor.optional_states')} <span style={S.optionalTag}>{i18nT('apps.mochi.editor.optional')}</span>
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.mochi.editor.peek_hint')}</div>
        <div style={S.grid}>
          {OPTIONAL_KEYS.map((key) => (
            <div
              key={key}
              role="button"
              tabIndex={0}
              aria-label={slotLabel(key)}
              style={slotStyle(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
              onKeyDown={(e) => {
                if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
                  e.preventDefault()
                  handleSlotClick(key, e)
                }
              }}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}><LoaderCircle size={16} className="lucide-inline" /></div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>
                {stateLabel(key)} <span style={S.optionalTag}>{i18nT('apps.mochi.editor.optional')}</span>
              </div>
            </div>
          ))}
        </div>

        {/* Optional mood slots */}
        <div style={S.sectionLabel}>
          {i18nT('apps.mochi.editor.moods')} <span style={S.optionalTag}>{i18nT('apps.mochi.editor.optional')}</span>
        </div>
        <div style={S.grid}>
          {MOOD_KEYS.map((key) => (
            <div
              key={key}
              role="button"
              tabIndex={0}
              aria-label={slotLabel(key)}
              style={slotStyle(!!slots[key])}
              onClick={(e) => handleSlotClick(key, e)}
              onKeyDown={(e) => {
                if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
                  e.preventDefault()
                  handleSlotClick(key, e)
                }
              }}
            >
              {loadingSlot === key ? (
                <div style={S.slotPlaceholder}><LoaderCircle size={16} className="lucide-inline" /></div>
              ) : (
                <SlotThumbnail data={slots[key]} />
              )}
              <div style={S.slotLabel}>
                {moodLabel(key)} <span style={S.optionalTag}>{i18nT('apps.mochi.editor.optional')}</span>
              </div>
            </div>
          ))}
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

        />
      )}

      <EditorFooter
        missingStates={missingStates}
        canSave={canSave}
        saving={saving}
        onCancel={onCancel}
        onSave={() => triggerSave(doSave)}

      />
      <SaveDialog
        visible={showSaveDialog}
        onOverwrite={() => confirmOverwrite(doSave)}
        onSaveNew={() => confirmSaveNew(doSave)}
        onCancel={cancelDialog}

      />
    </div>
  )
}
