/**
 * The ChatEmbed composer's trigger pickers: the chat-core picker atom
 * (`useComposerPickers` + `ComposerPickerMenus`, the same one the dashboard
 * composer mounts) bound to the embed's textarea and its scoped picker source.
 *
 * What the embed adds is only what is the app's:
 * - the source (`embedPickerSource`), which carries a read only when the
 *   host app declared its endpoint, so an ungranted picker never opens;
 * - what an accepted `@` file does: it is staged as an attachment, as the
 *   main chat stages it, so the send carries it, and removing its chip takes
 *   its `@token` out of the text too. A folder's token stays text: the embed
 *   sends no `meta.dirs`, and the agent reads the path as written;
 * - the commands the embed cannot run, kept out of the `/` menu.
 */
import { useCallback, useEffect, useMemo, useRef, type MutableRefObject } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useComposerPickers } from '../chat-core/composer/pickers'
import type { ComposerControl } from '../components/composerControl'
import type { FileKind } from '../components/FilePickerMenu'
import { mentionTokenRegex, normalizeWindowsPath } from '../utils/fileTokens'
import { useEmbedPickerSource } from './embedPickerSource'
import type { AppApi } from './scopedApi'

/** Commands the dashboard composer runs in the browser (`interceptSlashCommand`
 *  and its knowledge prefix). The embed has no side chat, no onboarding and no
 *  knowledge picker, so its menu does not offer them. Exported for tests. */
export const EMBED_HIDDEN_COMMANDS: readonly string[] = ['/btw', '/kb', '/onboarding', '/side']

/**
 * `text` with every mention of the removed `token` taken out, plus one space
 * after it. A spot where a still-picked token that extends this one starts
 * (`@report final.pdf` when `@report` is removed) is left alone, so removing
 * one chip never rewrites another file's mention. Exported for tests.
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

export function useEmbedPickers({ api, appName, slotKey, agent, draft, setDraft, textareaRef, stage, removeFile: removeStaged }: {
  api: AppApi
  appName: string
  slotKey: string
  agent?: string
  draft: string
  setDraft: (value: string) => void
  textareaRef: MutableRefObject<HTMLTextAreaElement | null>
  /** Stage a gateway-side file on the composer (`useComposerAttachments.stage`). */
  stage: (paths: string[]) => void
  /** Unstage one file (`useComposerAttachments.removeFile`). */
  removeFile: (path: string) => void
}) {
  const source = useEmbedPickerSource(api, appName)
  const queryClient = useQueryClient()
  // The textarea behind the shared `ComposerControl` the pickers insert
  // through: read the caret, put it back after the inserted token.
  const control = useMemo<ComposerControl>(() => ({
    focus: () => textareaRef.current?.focus(),
    getRootElement: () => textareaRef.current,
    getSelection: () => {
      const el = textareaRef.current
      return el ? { start: el.selectionStart ?? 0, end: el.selectionEnd ?? el.selectionStart ?? 0 } : null
    },
    setSelection: (start, end = start, options) => {
      const el = textareaRef.current
      if (!el) return
      el.setSelectionRange(Math.min(start, el.value.length), Math.min(end, el.value.length))
      if (options?.focus) el.focus()
    },
  }), [textareaRef])
  const composerControl = useCallback(() => control, [control])
  // The `@token` each picked file wrote, so removing its chip also takes the
  // token out of the text: otherwise the path the user just removed would
  // still be sent as written.
  const pickedTokensRef = useRef(new Map<string, string>())
  // A session switch clears the staged files, so their tokens go with them.
  useEffect(() => { pickedTokensRef.current.clear() }, [slotKey])
  const draftRef = useRef(draft)
  draftRef.current = draft
  const onFileSelect = useCallback((path: string, kind?: FileKind, token?: string) => {
    if (kind === 'dir') return
    const staged = normalizeWindowsPath(path)
    if (token) pickedTokensRef.current.set(staged, token)
    stage([staged])
  }, [stage])
  const removeFile = useCallback((path: string) => {
    removeStaged(path)
    const token = pickedTokensRef.current.get(path)
    if (!token) return
    pickedTokensRef.current.delete(path)
    const next = stripPickedToken(draftRef.current, token, [...pickedTokensRef.current.values()])
    if (next !== draftRef.current) setDraft(next)
  }, [removeStaged, setDraft])
  const pickers = useComposerPickers({
    source, value: draft, onChange: setDraft, composerControl, queryClient,
    onFileSelect, typedCommandMenus: true, slotId: slotKey, agentName: agent,
  })
  // The menus anchor to the textarea itself.
  const anchorRef = textareaRef as MutableRefObject<HTMLElement | null>
  return { pickers, onFileSelect, removeFile, anchorRef }
}
