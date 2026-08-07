/**
 * SpriteImporter — Import a sprite sheet and assign rows to pet states/moods.
 */
import { FolderOpen, Plus } from 'lucide-react'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { REQUIRED_STATES, OPTIONAL_STATES, STATUS_STATES, BREATHING_STATES, RANDOM_STATES, type PackMeta } from './appearanceTypes'
import { SpriteRenderer } from './SpriteRenderer'
import { PackInfoHeader } from './PackInfoHeader'
import { SaveDialog } from './SaveDialog'
import { EditorFooter } from './EditorFooter'
import { NumberField } from './NumberField'
import SimpleSelect from '../../components/SimpleSelect'
import { detectFrameSize } from './spriteDetect'
import { useLang, useSaveWithDialog } from './editorHooks'
import { galleryApi } from './petBridge'
import { entryContent, type AnimationEntry } from './packAnimation'
import type { SpriteImportResult } from './spriteImportTypes'
import './gallery.css'

const api = galleryApi

interface RowPreview {
  index: number
  dataUri: string
  frameCount: number
}

interface Props {
  existingPack?: PackMeta
  onDone: (result: SpriteImportResult) => void
  onCancel: () => void
  /** A refusal from the save the parent ran; shown here because a failed save
   *  keeps this editor open (unmounting discarded the user's edits). */
  saveError?: string | null
}

export const SpriteImporter: React.FC<Props> = ({ existingPack, onDone, onCancel, saveError }) => {
  const { i18nT } = useLang()
  const [imgSrc, setImgSrc] = useState<string | null>(null)
  const [imgW, setImgW] = useState(0)
  const [imgH, setImgH] = useState(0)
  const [frameW, setFrameW] = useState(32)
  const [frameH, setFrameH] = useState(32)
  const [fps, setFps] = useState(8)
  const [flipX, setFlipX] = useState(false)
  const [offsetY, setOffsetY] = useState(0)
  const [rows, setRows] = useState<RowPreview[]>([])
  const [assignments, setAssignments] = useState<Record<string, number | null>>(() => {
    const init: Record<string, number | null> = {}
    for (const s of REQUIRED_STATES) init[s] = null
    for (const s of OPTIONAL_STATES) init[s] = null
    return init
  })
  const [name, setName] = useState(existingPack?.name || '')
  const [author, setAuthor] = useState(existingPack?.author || '')
  const [description, setDescription] = useState(existingPack?.description || '')

  // Pending assignments from edit load (stored in ref, not window)
  const pendingAssignments = useRef<Record<string, number | null> | null>(null)
  const originalSnapshot = useRef<string | null>(null)
  /** Open-ended random clip names from the loaded pack (PetDex custom clips). */
  const randomNamesRef = useRef<Set<string>>(new Set())

  const snap = () => JSON.stringify({ name, author, description, flipX, frameW, frameH, fps, offsetY, assignments })
  const isDirty = !existingPack || originalSnapshot.current === null || snap() !== originalSnapshot.current

  // Load existing pack
  useEffect(() => {
    if (!existingPack) return
    api?.galleryGetPackDetail?.(existingPack.id).then(async (d) => {
      if (!d?.sprite) return
      // Remember which slots are open-ended random CLIPS. Their assignments
      // flow through the same map as the fixed slots, but the save must file
      // them as randomAssignments — the bridge classifies any unknown key in
      // `assignments` as a MOOD, which silently disabled a PetDex pack's
      // random behavior after an edit.
      randomNamesRef.current = new Set((d.randomNames as string[] | undefined) ?? [])
      setFrameW(d.sprite.frameWidth || 32)
      setFrameH(d.sprite.frameHeight || 32)
      setFps(d.sprite.fps || 8)
      if (d.sprite.flipX) setFlipX(true)
      if (d.sprite.offsetY) setOffsetY(d.sprite.offsetY)

      // Store pending assignments from rowAssignments
      const loadedAssignments: Record<string, number | null> = {}
      for (const s of REQUIRED_STATES) loadedAssignments[s] = null
      for (const s of OPTIONAL_STATES) loadedAssignments[s] = null
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
        const anims = d.animations as Record<string, AnimationEntry>
        const rowMap = new Map<string, number>()
        const fallbackRows: RowPreview[] = []
        const fa: Record<string, number | null> = {}
        for (const s of REQUIRED_STATES) fa[s] = null
        for (const s of OPTIONAL_STATES) fa[s] = null
        for (const [key, anim] of Object.entries(anims)) {
          const uri = entryContent(anim).startsWith('data:') ? entryContent(anim) : `data:image/png;base64,${entryContent(anim)}`
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
  }, [])

  const handleSelectFile = useCallback(async () => {
    const result = await api?.importSpriteFile?.()
    if (!result || result.ok === false) return
    const content = result.value?.content ?? ''
    if (!content) return
    const uri = `data:image/png;base64,${content}`
    // Auto-detect frame size from the image
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')!
      ctx.drawImage(img, 0, 0)
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height)
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
      const ctx = canvas.getContext('2d')!

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
    const randomResult: Record<string, string> = {}
    const rowAssignments: Record<string, number> = {}
    // A slot is a random CLIP when it's one of the fixed random vocabulary
    // slots OR one of the loaded pack's open-ended clip names (PetDex packs).
    // Those must travel as randomAssignments: the bridge files any unknown
    // key in `assignments` as a mood, which reclassified random clips and
    // disabled the pack's random behavior on the next save.
    const isRandom = (k: string) =>
      (RANDOM_STATES as readonly string[]).includes(k) || randomNamesRef.current.has(k)
    for (const [key, rowIdx] of Object.entries(assignments)) {
      if (rowIdx != null && rows[rowIdx]) {
        if (isRandom(key)) randomResult[key] = rows[rowIdx].dataUri
        else result[key] = rows[rowIdx].dataUri
        rowAssignments[key] = rowIdx
      }
    }
    const data: SpriteImportResult = { name, author, description, frameWidth: frameW, frameHeight: frameH, fps, flipX, offsetY, sourceImage: imgSrc || undefined, assignments: result, rowAssignments }
    if (Object.keys(randomResult).length > 0) data.randomAssignments = randomResult
    if (!asNew && existingPack) data.overwriteId = existingPack.id
    onDone(data)
  }, [name, author, description, frameW, frameH, fps, flipX, offsetY, imgSrc, assignments, rows, existingPack, onDone])

  const S = {
    container: { display: 'flex', flexDirection: 'column' as const, height: '100%', color: 'var(--text)', background: 'var(--bg)' },
    body: { flex: 1, overflowY: 'auto' as const, padding: '12px 20px' },
    section: { marginBottom: 12 },
    sectionLabel: { fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase' as const, letterSpacing: 1, marginBottom: 6 },
  }

  return (
    <div style={S.container}>
      <PackInfoHeader
        title={existingPack ? i18nT('apps.crewCompanion.sprite.editTitle') : i18nT('apps.crewCompanion.sprite.title')}
        name={name} author={author} description={description} flipX={flipX}
        onNameChange={setName} onAuthorChange={setAuthor} onDescriptionChange={setDescription} onFlipXChange={setFlipX}
        tt={i18nT}
      />

      <div style={S.body}>
        {/* Frame config */}
        <div style={{ ...S.section, display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' as const }}>
          <button onClick={handleSelectFile} style={{
            padding: '6px 14px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
            border: '1px solid var(--border)', background: 'var(--cc-input-bg)', color: 'var(--text)',
          }}><FolderOpen className="lucide-inline" aria-hidden="true" />{' '}{imgSrc ? i18nT('apps.crewCompanion.sprite.changeFile') : i18nT('apps.crewCompanion.sprite.selectFile')}</button>
          <NumberField label={i18nT('apps.crewCompanion.sprite.frameWidth')} value={frameW} min={8} max={512} onChange={setFrameW} />
          <NumberField label={i18nT('apps.crewCompanion.sprite.frameHeight')} value={frameH} min={8} max={512} onChange={setFrameH} />
          <NumberField label="FPS" value={fps} min={1} max={60} onChange={setFps} />
          <NumberField label={i18nT('apps.crewCompanion.sprite.offsetY')} value={offsetY} onChange={setOffsetY} />
        </div>

        {/* Source image preview with grid overlay */}
        {imgSrc && (
          <div style={S.section}>
            <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.sprite.previewDims', { w: imgW, h: imgH, cols: Math.floor(imgW / (frameW || 1)), rows: Math.floor(imgH / (frameH || 1)) })}</div>
            <div style={{ background: 'var(--cc-input-bg)', borderRadius: 8, padding: 8, overflow: 'auto', maxHeight: 250, position: 'relative' }}>
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
        {rows.length > 0 && (
          <div style={S.section}>
            {/* Not a category — this is the SOURCE material: each row of the sheet
                sliced into a loop you can then assign to a slot below. "Rows" named
                the mechanism rather than the thing. */}
            <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.sprite.rows')} ({rows.length})</div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6, marginTop: -4 }}>{i18nT('apps.crewCompanion.sprite.assignHint')}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {rows.map(row => (
                <div key={row.index} style={{
                  background: 'var(--cc-input-bg)', borderRadius: 8, padding: 8,
                  display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                }}>
                  <div style={{ width: 80, height: 80, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
                    <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} />
                  </div>
                  <span style={{ fontSize: 11, color: 'var(--text)', fontWeight: 600 }}>{i18nT('apps.crewCompanion.sprite.row', { n: row.index + 1 })}</span>
                  <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>{row.frameCount}f</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* State assignments */}
        {rows.length > 0 && (
          <div style={S.section}>
            <div style={S.sectionLabel}>{i18nT('apps.crewCompanion.editor.requiredStates')}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {REQUIRED_STATES.map(s => {
                const rowIdx = assignments[s]
                const row = rowIdx != null ? rows[rowIdx] : null
                return (
                  <div key={s} style={{
                    background: 'var(--cc-input-bg)', borderRadius: 8, padding: 8,
                    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                    border: row ? '1.5px solid var(--accent)' : '1.5px dashed var(--border)',
                  }}>
                    <div style={{ width: 64, height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
                      {row ? <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} displaySize={64} /> : <Plus size={24} className="lucide-inline" aria-hidden="true" style={{ color: 'var(--text-faint)' }} />}
                    </div>
                    <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text)' }}>{s} *</span>
                    <SimpleSelect
                      options={rows.map(r => String(r.index))}
                      optionLabels={rows.map(r => i18nT('apps.crewCompanion.sprite.row', { n: r.index + 1 }))}
                      value={rowIdx == null ? '' : String(rowIdx)}
                      onChange={v => setAssignments(prev => ({ ...prev, [s]: v === '' ? null : Number(v) }))}
                      clearLabel="—"
                      aria-label={s}
                      style={{ width: '100%' }}
                      className="px-2 py-1 text-[10px]"
                    />
                  </div>
                )
              })}
            </div>
            {/* Status — the same three slots the "Make your own" editor asks for
                (shared/appearanceTypes). This grid used to list every OPTIONAL_STATE,
                including legacy ones, so importing a sheet offered slots the editor
                didn't and vice versa. */}
            <div style={{ ...S.sectionLabel, marginTop: 12 }}>{i18nT('apps.crewCompanion.state.groupStatus')} <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>(optional)</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {[...STATUS_STATES].map(s => {
                const rowIdx = assignments[s]
                const row = rowIdx != null ? rows[rowIdx] : null
                return (
                  <div key={s} style={{
                    background: 'var(--cc-input-bg)', borderRadius: 8, padding: 8,
                    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                    border: row ? '1px solid var(--border)' : '1px dashed var(--border)',
                  }}>
                    <div style={{ width: 64, height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
                      {row ? <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} displaySize={64} /> : <Plus size={24} className="lucide-inline" aria-hidden="true" style={{ color: 'var(--text-faint)' }} />}
                    </div>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s}</span>
                    <SimpleSelect
                      options={rows.map(r => String(r.index))}
                      optionLabels={rows.map(r => i18nT('apps.crewCompanion.sprite.row', { n: r.index + 1 }))}
                      value={rowIdx == null ? '' : String(rowIdx)}
                      onChange={v => setAssignments(prev => ({ ...prev, [s]: v === '' ? null : Number(v) }))}
                      clearLabel="—"
                      aria-label={s}
                      style={{ width: '100%' }}
                      className="px-2 py-1 text-[10px]"
                    />
                  </div>
                )
              })}
            </div>
            {/* Random — the same single slot the create editor offers ("Wander").
                The five mood slots that used to sit here aren't offered on creation,
                so an imported sheet could carry art the editor could never produce. */}
            <div style={{ ...S.sectionLabel, marginTop: 12 }}>{i18nT('apps.crewCompanion.state.groupBreathing')} <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>(optional)</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {[...BREATHING_STATES].map(s => {
                const rowIdx = assignments[s]
                const row = rowIdx != null ? rows[rowIdx] : null
                return (
                  <div key={s} style={{
                    background: 'var(--cc-input-bg)', borderRadius: 8, padding: 8,
                    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                    border: row ? '1px solid var(--border)' : '1px dashed var(--border)',
                  }}>
                    <div style={{ width: 64, height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
                      {row ? <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} displaySize={64} /> : <Plus size={24} className="lucide-inline" aria-hidden="true" style={{ color: 'var(--text-faint)' }} />}
                    </div>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s}</span>
                    <SimpleSelect
                      options={rows.map(r => String(r.index))}
                      optionLabels={rows.map(r => i18nT('apps.crewCompanion.sprite.row', { n: r.index + 1 }))}
                      value={rowIdx == null ? '' : String(rowIdx)}
                      onChange={v => setAssignments(prev => ({ ...prev, [s]: v === '' ? null : Number(v) }))}
                      clearLabel="—"
                      aria-label={s}
                      style={{ width: '100%' }}
                      className="px-2 py-1 text-[10px]"
                    />
                  </div>
                )
              })}
            </div>
            <div style={{ ...S.sectionLabel, marginTop: 12 }}>{i18nT('apps.crewCompanion.state.groupRandom')} <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>(optional)</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8 }}>
              {[...RANDOM_STATES].map(s => {
                const rowIdx = assignments[s]
                const row = rowIdx != null ? rows[rowIdx] : null
                return (
                  <div key={s} style={{
                    background: 'var(--cc-input-bg)', borderRadius: 8, padding: 8,
                    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                    border: row ? '1px solid var(--border)' : '1px dashed var(--border)',
                  }}>
                    <div style={{ width: 64, height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', transform: flipX ? 'scaleX(-1)' : 'none' }}>
                      {row ? <SpriteRenderer src={row.dataUri} frameWidth={frameW} frameHeight={frameH} fps={fps} displaySize={64} /> : <Plus size={24} className="lucide-inline" aria-hidden="true" style={{ color: 'var(--text-faint)' }} />}
                    </div>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s}</span>
                    <SimpleSelect
                      options={rows.map(r => String(r.index))}
                      optionLabels={rows.map(r => i18nT('apps.crewCompanion.sprite.row', { n: r.index + 1 }))}
                      value={rowIdx == null ? '' : String(rowIdx)}
                      onChange={v => setAssignments(prev => ({ ...prev, [s]: v === '' ? null : Number(v) }))}
                      clearLabel="—"
                      aria-label={s}
                      style={{ width: '100%' }}
                      className="px-2 py-1 text-[10px]"
                    />
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>

      {saveError ? (
        <p role="alert" style={{ margin: '0 20px 8px', fontSize: 12, color: 'var(--danger, #d33)' }}>
          {saveError}
        </p>
      ) : null}
      <EditorFooter
        missingStates={rows.length > 0 ? missingStates : []}
        canSave={canSave}
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
