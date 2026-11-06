/**
 * The Mention atom (chat-core RFC §3, P3-d): what an accepted `@` pick does in
 * a composer whose host hands the `Composer` root its staged-file list instead
 * of wiring `ChatInput`'s `onFileSelect` / `onRemoveFile` (a split pane). A
 * picked file is staged, and the `@token` its pick wrote is kept for the slot,
 * so removing the file's chip takes that token out of the text too. A
 * folder's `@rel/` token is its chip (`parseDirTokens`), so a folder pick
 * stages nothing.
 */
import { useMemo, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { ComposerFileSelect } from './pickers'
import { addPendingFile, mentionTokenRegex, normalizeWindowsPath } from '../../utils/fileTokens'

/**
 * `text` with every mention of the removed `token` taken out, plus one space
 * after it. A spot where a still-picked token that extends this one starts
 * (`@report final.pdf` when `@report` is removed) is left alone, so removing
 * one chip never rewrites another file's mention.
 */
export function stripPickedToken(text: string, token: string, retained: readonly string[]): string {
  const longer = retained.filter(t => t.length > token.length && t.startsWith(token))
  const re = mentionTokenRegex(token.slice(1), 'g', longer.map(t => t.slice(1)))
  let out = ''
  let last = 0
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    if (m[0].length === 0) { re.lastIndex++; continue }
    const at = m.index + m[1].length
    if (longer.some(t => text.startsWith(t, at))) continue
    let end = m.index + m[0].length
    if (text[end] === ' ') end++
    out += text.slice(last, at)
    last = end
  }
  return out + text.slice(last)
}

/** The pick and chip-remove `ChatInput` uses when its host passed neither. */
export interface ComposerMentionSlice {
  onFileSelect: ComposerFileSelect
  onRemoveFile: (path: string) => void
}

/** The atom's slice for the root, or null when the host passed no file list. */
export function useMentionSlice(
  setPendingFiles: Dispatch<SetStateAction<string[]>> | undefined,
  slotKey: string | null,
  inputRef: MutableRefObject<string>,
  onChange: (value: string) => void,
): ComposerMentionSlice | null {
  // Slot -> staged path -> the `@token` its pick wrote. Per slot: a pane
  // rebound to another slot parks that slot's draft and gets it back later.
  const [tokens] = useState(() => new Map<string | null, Map<string, string>>())
  const live = useRef({ slotKey, onChange })
  live.current = { slotKey, onChange }
  return useMemo(() => {
    if (!setPendingFiles) return null
    const slotTokens = () => {
      const slot = live.current.slotKey
      if (!tokens.has(slot)) tokens.set(slot, new Map())
      return tokens.get(slot)!
    }
    return {
      onFileSelect: (path, kind, token) => {
        if (kind === 'dir') return
        const staged = normalizeWindowsPath(path)
        if (token) slotTokens().set(staged, token)
        setPendingFiles(prev => addPendingFile(prev, staged))
      },
      onRemoveFile: (path) => {
        setPendingFiles(prev => prev.filter(p => p !== path))
        const picked = slotTokens()
        const token = picked.get(path)
        if (!token) return
        picked.delete(path)
        const text = inputRef.current
        const next = stripPickedToken(text, token, [...picked.values()])
        if (next !== text) live.current.onChange(next)
      },
    }
  }, [setPendingFiles, tokens, inputRef])
}
