/**
 * PetdexImporter — obtain a pet from petdex.dev, then hand it to the sprite
 * importer for mapping.
 *
 * This screen only ANSWERS "which pet"; it never decides which row drives which
 * animation. petdex files carry no mapping (the rows are a positional
 * convention), so the mapping is always confirmed by the user in the next step
 * with every row animated in front of them.
 *
 * Three ways in, because the files can already be on the machine:
 *   1. By name or link — the gateway fetches it from petdex.dev.
 *   2. Already installed — pets the `petdex` CLI put in `~/.codex/pets/`.
 *   3. A file you have — straight to the sprite importer.
 */
import React, { useCallback, useEffect, useState } from 'react'

import {
  petdexPrefill,
  type PetdexInstalled,
  type PetdexPet,
} from '../../petdexImport'
import { api } from '../mochiApi'
import type { SpritePrefillInput } from './SpriteImporter'
import { i18nT } from '../../../../i18n/t'
import { useImeGuard } from '../../../../hooks/useImeGuard'

interface Props {
  /** A pet was obtained and measured; take over with the mapping step. */
  onReady: (prefill: SpritePrefillInput) => void
  /** The user has their own sheet — open the plain sprite importer. */
  onUseFile: () => void
  onCancel: () => void
}

const S: Record<string, React.CSSProperties> = {
  root: { display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' },
  header: {
    display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
    borderBottom: '1px solid var(--border)', flexShrink: 0,
  },
  title: { fontSize: 13, fontWeight: 600, flex: 1 },
  body: { flex: 1, overflowY: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 18 },
  section: { display: 'flex', flexDirection: 'column', gap: 8 },
  label: { fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.4, color: 'var(--text-muted)' },
  hint: { fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.5 },
  inputRow: { display: 'flex', gap: 8 },
  input: {
    flex: 1, background: 'var(--bg-input)', border: '1px solid var(--border)',
    borderRadius: 6, padding: '6px 10px', color: 'var(--text)', fontSize: 12, outline: 'none',
  },
  btn: {
    padding: '6px 14px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
    border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text)',
  },
  btnAccent: {
    padding: '6px 14px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
    border: '1px solid var(--accent)', background: 'var(--accent)', color: '#fff',
  },
  petRow: {
    display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
    borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-input)',
    cursor: 'pointer', textAlign: 'left', width: '100%', color: 'var(--text)',
  },
  petName: { fontSize: 12, fontWeight: 600 },
  petDesc: { fontSize: 11, color: 'var(--text-muted)' },
  error: {
    fontSize: 11, color: 'var(--danger, #e5534b)', background: 'var(--bg-input)',
    border: '1px solid var(--danger, #e5534b)', borderRadius: 6, padding: '6px 10px',
  },
}

/** Measure a data URI so the row grid can be derived from real pixels. */
function measure(uri: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight })
    img.onerror = () => reject(new Error('could not read that spritesheet'))
    img.src = uri
  })
}

export const PetdexImporter: React.FC<Props> = ({ onReady, onUseFile, onCancel }) => {
  const ime = useImeGuard()
  const [slug, setSlug] = useState('')
  const [installed, setInstalled] = useState<PetdexInstalled[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api?.petdexListInstalled?.().then((pets) => {
      if (alive) setInstalled(pets)
    })
    return () => { alive = false }
  }, [])

  const take = useCallback(
    async (which: string, source: 'local' | 'remote') => {
      setError(null)
      setBusy(which)
      try {
        const result = await api?.petdexImport?.(which, source)
        if (!result) return setError(i18nT('apps.mochi.petdex.unavailable'))
        if (!result.ok) return setError(result.error)
        const pet: PetdexPet = result.value
        const uri = `data:${pet.imageMime};base64,${pet.imageBase64}`
        const { width, height } = await measure(uri)
        onReady(petdexPrefill(pet, width, height))
      } catch (err) {
        setError((err instanceof Error ? err.message : '') || i18nT('apps.mochi.petdex.failed'))
      } finally {
        setBusy(null)
      }
    },
    [onReady],
  )

  const submitSlug = useCallback(() => {
    const value = slug.trim()
    if (value !== '') void take(value, 'remote')
  }, [slug, take])

  return (
    <div style={S.root}>
      <div style={S.header}>
        <span style={S.title}>{i18nT('apps.mochi.petdex.title')}</span>
        <button style={S.btn} onClick={onCancel}>{i18nT('apps.mochi.petdex.cancel')}</button>
      </div>

      <div style={S.body}>
        {error && <div style={S.error}>{error}</div>}

        {/* 1 — by name or link */}
        <div style={S.section}>
          <div style={S.label}>{i18nT('apps.mochi.petdex.by_name')}</div>
          <div style={S.inputRow}>
            <input
              style={S.input}
              value={slug}
              placeholder={i18nT('apps.mochi.petdex.name_placeholder')}
              onChange={(e) => setSlug(e.target.value)}
              {...ime.bindEnter({ onEnter: submitSlug })}
              disabled={busy !== null}
            />
            <button
              style={S.btnAccent}
              onClick={submitSlug}
              disabled={busy !== null || slug.trim() === ''}
            >
              {busy !== null ? i18nT('apps.mochi.petdex.fetching') : i18nT('apps.mochi.petdex.fetch')}
            </button>
          </div>
          <div style={S.hint}>{i18nT('apps.mochi.petdex.network_note')}</div>
        </div>

        {/* 2 — already installed by the petdex CLI */}
        <div style={S.section}>
          <div style={S.label}>{i18nT('apps.mochi.petdex.installed')}</div>
          {installed.length === 0 ? (
            <div style={S.hint}>{i18nT('apps.mochi.petdex.none_installed')}</div>
          ) : (
            installed.map((pet) => (
              <button
                key={pet.slug}
                style={S.petRow}
                onClick={() => void take(pet.slug, 'local')}
                disabled={busy !== null}
              >
                <span style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
                  <span style={S.petName}>{pet.name}</span>
                  {pet.description && <span style={S.petDesc}>{pet.description}</span>}
                </span>
              </button>
            ))
          )}
        </div>

        {/* 3 — the user already has the sheet */}
        <div style={S.section}>
          <div style={S.label}>{i18nT('apps.mochi.petdex.own_file')}</div>
          <div style={S.hint}>{i18nT('apps.mochi.petdex.own_file_hint')}</div>
          <div>
            <button style={S.btn} onClick={onUseFile} disabled={busy !== null}>
              {i18nT('apps.mochi.petdex.choose_file')}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
