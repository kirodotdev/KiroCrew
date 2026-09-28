/**
 * SpriteImporter — Import a sprite sheet and assign rows to pet states/moods.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { FolderOpen, Plus} from 'lucide-react'
import { REQUIRED_STATES, ALL_MOODS, OPTIONAL_STATES, type PackMeta } from '../shared/appearanceTypes'
import { SpriteRenderer } from './SpriteRenderer'
import { PackInfoHeader } from './PackInfoHeader'
import { SaveDialog } from './SaveDialog'
import { EditorFooter } from './EditorFooter'
import { NumberField } from './NumberField'
import { detectFrameSize } from './spriteDetect'
import {
  derivePetdexGrid,
  PETDEX_FPS,
  PETDEX_SLOT_ROWS,
} from '../../petdexImport'
import { useSaveWithDialog } from './editorHooks'

import { api } from '../mochiApi'
import { i18nT } from '../../../../i18n/t'
import { moodLabel, stateLabel } from '../../i18nKeys'

interface RowPreview {
  index: number
  dataUri: string
  frameCount: number
}

/**
 * A sheet plus the mapping to START from, supplied by an importer that already
 * knows the sheet's layout (see `petdexImport.ts`).
 *
 * It is a starting point, never a commitment: the row previews and the per-slot
 * dropdowns stay fully editable, because the source format carries no mapping of
 * its own and a wrong guess must be visible and fixable rather than silent.
 */
export interface SpritePrefillInput {
  name: string
  author: string
  description: string
  imageUri: string
  /** 0 = "could not determine": fall back to automatic frame detection. */
  frameWidth: number
  frameHeight: number
  fps: number
  rowAssignments: Record<string, number>
}

/**
 * What Save hands back: the sheet's geometry plus the confirmed row mapping,
 * forwarded to the packs route verbatim.
 *
 * A `type`, not an `interface`, and that is load-bearing: the owner types its
 * handler against the open wire shape (`Record<string, unknown>` — the route
 * takes the whole object), and only an object-literal type alias carries the
 * implicit index signature that makes this assignable to it.
 */
export type SpritePackDraft = {
  name: string
  author: string
  description: string
  frameWidth: number
  frameHeight: number
  fps: number
  flipX: boolean
  offsetY: number
  /** The whole sheet, kept so a later re-edit can re-slice it. */
  sourceImage?: string
  /** slot -> the sliced row's data URI */
  assignments: Record<string, string>
  /** slot -> row index, so the mapping survives a re-edit */
  rowAssignments: Record<string, number>
  /** Set only when the user saved OVER an existing pack rather than creating one. */
  overwriteId?: string
}

interface Props {
  existingPack?: PackMeta
  prefill?: SpritePrefillInput
  onDone: (result: SpritePackDraft) => void
  onCancel: () => void
  /**
   * A save the owner attempted and failed. Rendered in the footer so this
   * importer can STAY MOUNTED on failure — the owner used to navigate back to
   * the gallery to show the error, discarding every frame, row and name the user
   * had configured.
   */
  saveError?: string | null
}

export const SpriteImporter: React.FC<Props> = ({
  existingPack,
  prefill,
  onDone,
  onCancel,
  saveError,
}) => {
  const [imgSrc, setImgSrc] = useState<string | null>(prefill?.imageUri ?? null)
  const [imgW, setImgW] = useState(0)
  const [imgH, setImgH] = useState(0)
  // A prefilled frame size of 0 means the importer could not confirm the grid;
  // 32 then keeps the auto-detection path in charge rather than shearing frames
  // against a geometry nobody verified.
  const [frameW, setFrameW] = useState(prefill?.frameWidth || 32)
  const [frameH, setFrameH] = useState(prefill?.frameHeight || 32)
  const [fps, setFps] = useState(prefill?.fps || 8)
  const [flipX, setFlipX] = useState(false)
  const [offsetY, setOffsetY] = useState(0)
  const [rows, setRows] = useState<RowPreview[]>([])
  // True when a picked sheet matched the petdex.dev grid and the mapping was
  // pre-filled from that convention. Surfaced in the UI: dropdowns that fill
  // themselves with no explanation read as a bug.
  const [petdexDetected, setPetdexDetected] = useState(false)
  const [assignments, setAssignments] = useState<Record<string, number | null>>(() => {
    const init: Record<string, number | null> = {}
    for (const s of REQUIRED_STATES) init[s] = null
    for (const s of OPTIONAL_STATES) init[s] = null
    for (const m of ALL_MOODS) init[m] = null
    for (const [slot, row] of Object.entries(prefill?.rowAssignments ?? {})) {
      if (slot in init) init[slot] = row
    }
    return init
  })
  // `existingPack` (re-edit) and `prefill` (fresh import) are mutually
  // exclusive; the edit-load effect below no-ops without an existingPack.
  const [name, setName] = useState(existingPack?.name || prefill?.name || '')
  const [author, setAuthor] = useState(existingPack?.author || prefill?.author || '')
  const [description, setDescription] = useState(
    existingPack?.description || prefill?.description || '',
  )

  // Pending assignments from edit load (stored in ref, not window)
  const pendingAssignments = useRef<Record<string, number | null> | null>(null)
  const originalSnapshot = useRef<string | null>(null)

  const snap = () => JSON.stringify({ name, author, description, flipX, frameW, frameH, fps, offsetY, assignments })
  const isDirty = !existingPack || originalSnapshot.current === null || snap() !== originalSnapshot.current

  // Load existing pack
  useEffect(() => {
    if (!existingPack) return
    api?.galleryGetPackDetail?.(existingPack.id).then(async (d) => {
      if (!d?.sprite) return
      setFrameW(d.sprite.frameWidth || 32)
      setFrameH(d.sprite.frameHeight || 32)
      setFps(d.sprite.fps || 8)
      if (d.sprite.flipX) setFlipX(true)
      if (d.sprite.offsetY) setOffsetY(d.sprite.offsetY)

      // Store pending assignments from rowAssignments
      const loadedAssignments: Record<string, number | null> = {}
      for (const s of REQUIRED_STATES) loadedAssignments[s] = null
      for (const s of OPTIONAL_STATES) loadedAssignments[s] = null
      for (const m of ALL_MOODS) loadedAssignments[m] = null
      if (d.sprite.rowAssignments) {
        const ra = d.sprite.rowAssignments as Record<string, number>
        for (const [k, v] of Object.entries(ra)) loadedAssignments[k] = v
      }
      pendingAssignments.current = loadedAssignments

      // Snapshot for dirty check
      originalSnapshot.current = JSON.stringify({
        name: existingPack.name || '', author: existingPack.author || '', description: existingPack.description || '',
        flipX: !!d.sprite.flipX, frameW: d.sprite.frameWidth || 32, frameH: d.sprite.frameHeight || 32,
        fps: d.sprite.fps || 8, offsetY: d.sprite.offsetY || 0, assignments: loadedAssignments,
      })

      // Load source image (triggers slice) or fallback to strips
      let sourceLoaded = false
      if (d.sprite.source) {
        const b64 = await api?.galleryReadPackFile?.(existingPack.id, d.sprite.source)
        if (b64) { setImgSrc(`data:image/png;base64,${b64}`); sourceLoaded = true }
      }
      if (!sourceLoaded) {
        // Build rows from existing strip animations
        const anims = d.animations as Record<string, { content: string; format: string }>
        const rowMap = new Map<string, number>()
        const fallbackRows: RowPreview[] = []
        const fa: Record<string, number | null> = {}
        for (const s of REQUIRED_STATES) fa[s] = null
        for (const s of OPTIONAL_STATES) fa[s] = null
        for (const m of ALL_MOODS) fa[m] = null
        for (const [key, anim] of Object.entries(anims)) {
          const uri = anim.content.startsWith('data:') ? anim.content : `data:image/png;base64,${anim.content}`
          if (!rowMap.has(uri)) {
            rowMap.set(uri, fallbackRows.length)
            fallbackRows.push({ index: fallbackRows.length, dataUri: uri, frameCount: 0 })
          }
          fa[key] = rowMap.get(uri)!
        }
        setRows(fallbackRows)
        setAssignments(fa)
        pendingAssignments.current = null
        originalSnapshot.current = JSON.stringify({
          name: existingPack.name || '', author: existingPack.author || '', description: existingPack.description || '',
          flipX: !!d.sprite.flipX, frameW: d.sprite.frameWidth || 32, frameH: d.sprite.frameHeight || 32,
          fps: d.sprite.fps || 8, offsetY: d.sprite.offsetY || 0, assignments: fa,
        })
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-once: this SEEDS the draft (frame size, fps, flipX, offsetY, row assignments, sheet) from the stored pack and takes the dirty snapshot it is compared against. Re-running on a new `existingPack` identity would overwrite whatever the user has configured since and re-baseline the snapshot, so an edit in progress would silently revert and then read as unchanged. Which pack is being edited is fixed for the lifetime of this mount: the gallery unmounts the importer to leave edit mode.
  }, [])

  const handleSelectFile = useCallback(async () => {
    const result = await api?.importSpriteFile?.()
    if (!result || result.ok === false) return
    const { content, mime } = result.value ?? result
    // The mime comes from the picked file. It used to be hard-coded to PNG,
    // which left WebP sheets (what petdex.dev ships) to browser sniffing.
    const uri = `data:${mime || 'image/png'};base64,${content}`
    // Auto-detect frame size from the image
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d', { willReadFrequently: true })!
      ctx.drawImage(img, 0, 0)
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height)
      // A sheet whose pixels divide exactly into the documented petdex.dev grid
      // gets that grid and its row convention, so a pet downloaded by hand lands
      // the same way as one imported through the Petdex screen. The mapping stays
      // editable and the match is announced -- it is a starting point, not a claim.
      const petdex = derivePetdexGrid(img.naturalWidth, img.naturalHeight)
      if (petdex.matchesConvention) {
        setFrameW(petdex.frameWidth)
        setFrameH(petdex.frameHeight)
        setFps(PETDEX_FPS)
        setOffsetY(0)
        setPetdexDetected(true)
        setAssignments((prev) => {
          const next = { ...prev }
          for (const [slot, row] of Object.entries(PETDEX_SLOT_ROWS)) {
            if (slot in next && next[slot] == null && row < petdex.rows) next[slot] = row
          }
          return next
        })
        setImgSrc(uri)
        return
      }
      setPetdexDetected(false)
      const detected = detectFrameSize(imageData)
      if (detected.frameWidth > 0 && detected.frameWidth < img.naturalWidth) setFrameW(detected.frameWidth)
      if (detected.frameHeight > 0 && detected.frameHeight < img.naturalHeight) setFrameH(detected.frameHeight)
      if (detected.offsetY > 0) setOffsetY(detected.offsetY)
      setImgSrc(uri)
    }
    img.src = uri
  }, [])

  // Slice image into rows
  useEffect(() => {
    if (!imgSrc) return
    const img = new Image()
    const onLoad = () => {
      const iw = img.naturalWidth
      const ih = img.naturalHeight
      setImgW(iw)
      setImgH(ih)
      const fw = frameW || 32
      const fh = frameH || 32
      const effectiveH = ih - offsetY
      const numRows = Math.ceil(effectiveH / fh)
      const cols = Math.floor(iw / fw)
      const newRows: RowPreview[] = []
      const canvas = document.createElement('canvas')
      const ctx = canvas.getContext('2d', { willReadFrequently: true })!

      for (let r = 0; r < numRows; r++) {
        const srcY = offsetY + r * fh
        const rowH = Math.min(fh, ih - srcY)
        if (rowH <= 0) break
        canvas.width = cols * fw
        canvas.height = rowH
        ctx.clearRect(0, 0, canvas.width, rowH)
        ctx.drawImage(img, 0, srcY, cols * fw, rowH, 0, 0, cols * fw, rowH)
        newRows.push({ index: r, dataUri: canvas.toDataURL('image/png'), frameCount: cols })
      }
      setRows(newRows)

      // Restore pending assignments from edit
      if (pendingAssignments.current) {
        setAssignments(pendingAssignments.current)
        pendingAssignments.current = null
      }
    }
    img.addEventListener('load', onLoad)
    img.src = imgSrc
    return () => img.removeEventListener('load', onLoad)
  }, [imgSrc, frameW, frameH, offsetY])

  // Save logic
  const missingStates = REQUIRED_STATES.filter(s => assignments[s] == null)
  const canSave = missingStates.length === 0 && name.trim().length > 0 && isDirty
  const { showSaveDialog, triggerSave, confirmOverwrite, confirmSaveNew, cancelDialog } = useSaveWithDialog(existingPack, isDirty)

  const doSave = useCallback((asNew: boolean) => {
    const result: Record<string, string> = {}
    const rowAssignments: Record<string, number> = {}
    for (const [key, rowIdx] of Object.entries(assignments)) {
      if (rowIdx != null && rows[rowIdx]) {
        result[key] = rows[rowIdx].dataUri
        rowAssignments[key] = rowIdx
      }
    }
    const data: SpritePackDraft = { name, author, description, frameWidth: frameW, frameHeight: frameH, fps, flipX, offsetY, sourceImage: imgSrc || undefined, assignments: result, rowAssignments }
    if (!asNew && existingPack) data.overwriteId = existingPack.id
    onDone(data)
  }, [name, author, description, frameW, frameH, fps, flipX, offsetY, imgSrc, assignments, rows, existingPack, onDone])

  const S = {
    container: { display: 'flex', flexDirection: 'column' as const, height: '100%', color: 'var(--text)', background: 'var(--bg)' },
    body: { flex: 1, overflowY: 'auto' as const, padding: '12px 20px' },
    select: { background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6, padding: '4px 8px', color: 'var(--text)', fontSize: 12, outline: 'none' },
    section: { marginBottom: 12 },
    sectionLabel: { fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase' as const, letterSpacing: 1, marginBottom: 6 },
  }

  /**
   * One assignment card (preview + label + row selector). A plain render
   * helper rather than a nested component: defining a component inside render
   * would give it a fresh type identity every pass and remount the subtree.
   * The three grids (required / optional / moods) differ only in emphasis and
   * label source, which is exactly the parameter surface here.
   */
  const renderSlotCard = (slot: string, label: string, required: boolean) => {
    const rowIdx = assignments[slot]
    const row = rowIdx != null ? rows[rowIdx] : null
    const border = required
      ? (row ? '1.5px solid var(--accent)' : '1.5px dashed var(--border)')
      : (row ? '1px solid var(--border)' : '1px dashed var(--border)')
    return (
      <div key={slot} style={{
        background: 'var(--bg-input)', borderRadius: 8, padding: 8,
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
        border,
      }}>
        <div style={{ width: 64, height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
          {row ? <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} displaySize={64} /> : <span style={{ fontSize: 24, color: 'var(--text-faint)' }}><Plus size={14} /></span>}
        </div>
        {required
          ? <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text)' }}>{label} *</span>
          : <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{label}</span>}
        <select value={rowIdx ?? ''} onChange={e => setAssignments(prev => ({ ...prev, [slot]: e.target.value === '' ? null : Number(e.target.value) }))} style={{ ...S.select, width: '100%', fontSize: 10 }}>
          <option value="">—</option>
          {rows.map(r => <option key={r.index} value={r.index}>{i18nT('apps.mochi.sprite.row_n', { n: r.index + 1 })}</option>)}
        </select>
      </div>
    )
  }

  return (
    <div style={S.container}>
      <PackInfoHeader
        title={existingPack ? i18nT('apps.mochi.sprite.edit_title') : i18nT('apps.mochi.sprite.title')}
        name={name} author={author} description={description} flipX={flipX}
        onNameChange={setName} onAuthorChange={setAuthor} onDescriptionChange={setDescription} onFlipXChange={setFlipX}

      />

      <div style={S.body}>
        {/* Frame config */}
        <div style={{ ...S.section, display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' as const }}>
          <button onClick={handleSelectFile} style={{
            padding: '6px 14px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
            border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text)',
            display: 'inline-flex', alignItems: 'center', gap: 6,
          }}><FolderOpen size={13} /> {imgSrc ? i18nT('apps.mochi.sprite.change_file') : i18nT('apps.mochi.sprite.select_file')}</button>
          <NumberField label={i18nT('apps.mochi.sprite.frame_width')} value={frameW} min={8} max={512} onChange={setFrameW} />
          <NumberField label={i18nT('apps.mochi.sprite.frame_height')} value={frameH} min={8} max={512} onChange={setFrameH} />
          <NumberField label={i18nT('apps.mochi.sprite.fps')} value={fps} min={1} max={60} onChange={setFps} />
          <NumberField label={i18nT('apps.mochi.sprite.offset_y')} value={offsetY} onChange={setOffsetY} />
        </div>

        {/* Source image preview with grid overlay */}
        {imgSrc && (
          <div style={S.section}>
            <div style={S.sectionLabel}>{i18nT('apps.mochi.sprite.preview')} {i18nT('apps.mochi.sprite.dimensions', { w: imgW, h: imgH, cols: Math.floor(imgW / (frameW || 1)), rows: Math.floor(imgH / (frameH || 1)) })}</div>
            <div style={{ background: 'var(--bg-input)', borderRadius: 8, padding: 8, overflow: 'auto', maxHeight: 250, position: 'relative' }}>
              <div style={{ position: 'relative', display: 'inline-block' }}>
                <img src={imgSrc} alt="" style={{ imageRendering: 'pixelated', display: 'block', maxWidth: 'none', minWidth: 400 }} />
                {frameH > 0 && Array.from({ length: Math.ceil(imgH / frameH) }, (_, i) => i > 0 && (
                  <div key={`h${i}`} style={{ position: 'absolute', left: 0, right: 0, top: `${((i * frameH + offsetY) / imgH) * 100}%`, height: 1, background: 'var(--accent)' }} />
                ))}
                {frameW > 0 && Array.from({ length: Math.floor(imgW / frameW) }, (_, i) => i > 0 && (
                  <div key={`v${i}`} style={{ position: 'absolute', top: 0, bottom: 0, left: `${(i * frameW / imgW) * 100}%`, width: 1, background: 'var(--accent)' }} />
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Row previews */}
        {(petdexDetected || (prefill?.rowAssignments && Object.keys(prefill.rowAssignments).length > 0)) && (
          <div style={{
            fontSize: 11, color: 'var(--text-muted)', background: 'var(--bg-input)',
            border: '1px solid var(--border)', borderRadius: 6, padding: '6px 10px',
            margin: '0 0 8px',
          }}>{i18nT('apps.mochi.sprite.petdex_prefilled')}</div>
        )}

        {rows.length > 0 && (
          <div style={S.section}>
            <div style={S.sectionLabel}>{i18nT('apps.mochi.sprite.rows')} ({rows.length})</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {rows.map(row => (
                <div key={row.index} style={{
                  background: 'var(--bg-input)', borderRadius: 8, padding: 8,
                  display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                }}>
                  <div style={{ width: 80, height: 80, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
                    {/* displaySize is REQUIRED here: without it the sheet renders
                        at its native frame size (192x208 for a petdex sheet) and
                        the 80px box just crops the middle, which reads as a
                        zoomed-in preview. The slot pickers below already pass it. */}
                    <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} displaySize={72} />
                  </div>
                  <span style={{ fontSize: 11, color: 'var(--text)', fontWeight: 600 }}>{i18nT('apps.mochi.sprite.row_n', { n: row.index + 1 })}</span>
                  <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>{i18nT('apps.mochi.sprite.frames_short', { count: row.frameCount })}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* State assignments */}
        {rows.length > 0 && (
          <div style={S.section}>
            <div style={S.sectionLabel}>{i18nT('apps.mochi.editor.required_states')}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {REQUIRED_STATES.map(s => renderSlotCard(s, stateLabel(s), true))}
            </div>
            <div style={{ ...S.sectionLabel, marginTop: 12 }}>{i18nT('apps.mochi.editor.optional_states')}</div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.mochi.editor.peek_hint')}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {OPTIONAL_STATES.map(s => renderSlotCard(s, stateLabel(s), false))}
            </div>
            <div style={{ ...S.sectionLabel, marginTop: 12 }}>{i18nT('apps.mochi.editor.moods')}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {ALL_MOODS.map(m => renderSlotCard(m, moodLabel(m), false))}
            </div>
          </div>
        )}
      </div>

      <EditorFooter
        missingStates={rows.length > 0 ? missingStates : []}
        canSave={canSave}
        saveError={saveError}
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
