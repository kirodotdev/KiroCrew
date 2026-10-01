import { useCallback, useRef, useState } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { skillsCacheStaleTime } from '../../lib/skillsCache'
import { matchFileToken, matchPathToken, matchSkillToken, replaceTokenAtCaret } from '../composerTokens'
import type { ComposerControl } from '../composerControl'
import type { ChatInputProps } from './props'

/* The in-input trigger pickers: `/` commands, `@` files, `$` skills and
   shell-style `./` paths. One rule decides which menu the text at the caret
   opens, for the textarea and the Lexical editor alike; the menus themselves
   render in `PickerMenus.tsx`. */

export function useComposerPickers({ project, onFileSelect, typedCommandMenus, value, onChange, composerControl, queryClient, slotId, agentName }: {
  project?: string
  onFileSelect: ChatInputProps['onFileSelect']
  typedCommandMenus: boolean
  value: string
  onChange: (v: string) => void
  composerControl: () => ComposerControl | null
  queryClient: QueryClient
  slotId: string | null
  agentName?: string
}) {
  const [slashMenuOpen, setSlashMenuOpen] = useState(false)
  const [filePickerOpen, setFilePickerOpen] = useState(false)
  // Shell-style `./` / `../` completion. Its own open/query pair rather than a
  // flag on the @ picker's, because the two carry different tokens and only one
  // token can end at the caret — see `pathTokenAt` below.
  const [pathPickerOpen, setPathPickerOpen] = useState(false)
  const [pathQuery, setPathQuery] = useState('')
  // The path token ending at the caret, or null. Gated on a project dir: `./`
  // names nothing without the root it resolves against, so with no project the
  // menu stays shut rather than opening on a listing that cannot be produced.
  const pathTokenAt = useCallback(
    (before: string) => (project ? matchPathToken(before) : null),
    [project],
  )
  const [fileQuery, setFileQuery] = useState('')
  const [skillPickerOpen, setSkillPickerOpen] = useState(false)
  const [skillQuery, setSkillQuery] = useState('')
  // Project skill awaiting consent, together with the exact chat/project/request
  // that initiated it. A grant can outlive this dialog, so completion must not
  // write into a different draft or supersede a newer consent request.
  const nextTrustRequestIdRef = useRef(0)
  const activeTrustRequestIdRef = useRef<number | null>(null)
  const [trustPrompt, setTrustPrompt] = useState<{
    requestId: number
    leaf: string
    slotKey?: string
    project?: string
  } | null>(null)
  /** Any picker is open. The pickers own ↑/↓ and Escape while they are, so the
   *  prompt-history keys and the dictation Escape both yield to it. */
  const anyPickerOpenRef = useRef(false)
  anyPickerOpenRef.current = slashMenuOpen || filePickerOpen || skillPickerOpen || pathPickerOpen
  const closePickers = useCallback(() => {
    setSlashMenuOpen(false)
    setFilePickerOpen(false); setFileQuery('')
    setSkillPickerOpen(false); setSkillQuery('')
    setPathPickerOpen(false); setPathQuery('')
  }, [])
  /** Open whichever picker the edited text calls for. `text` is the whole
   *  value; `before` is the text up to the caret. */
  const openPickersForText = useCallback((text: string, before: string) => {
    setSlashMenuOpen(typedCommandMenus && text.startsWith('/'))
    // Anchor @/$ detection to the token being edited AT THE CARET, not the
    // end of the whole input. `before` ends at the caret, so a match means
    // "the token ends where my cursor is" — which makes both pickers fire
    // mid-sentence and when trailing text/newlines follow the token.
    // Matchers live in composerTokens.ts (unit-tested there).
    const fileQ = onFileSelect ? matchFileToken(before) : null
    if (fileQ !== null) { setFilePickerOpen(true); setFileQuery(fileQ) }
    else { setFilePickerOpen(false); setFileQuery('') }
    // $ and @ are mutually exclusive (a token starts with one sigil); @ wins.
    const skillQ = fileQ === null ? matchSkillToken(before) : null
    if (typedCommandMenus && skillQ !== null) { setSkillPickerOpen(true); setSkillQuery(skillQ) }
    else { setSkillPickerOpen(false); setSkillQuery('') }
    const pathQ = pathTokenAt(before)
    if (pathQ !== null) { setPathPickerOpen(true); setPathQuery(pathQ) }
    else { setPathPickerOpen(false); setPathQuery('') }
  }, [typedCommandMenus, onFileSelect, pathTokenAt])
  // Warm the per-slot-and-project skills cache when the input gains focus so the first
  // `$` trigger renders the picker instantly (the fetch is the only latency).
  // prefetchQuery is a no-op if the cache is already fresh (staleTime), so it's
  // cheap to call on every focus. The key and the session key must match
  // SkillPickerMenu's exactly — including the trailing agent segment — or the
  // prefetch warms a different entry and the menu still pays the fetch on open.
  // The deadline binds HERE too, not only in the menu: react-query dedupes on that
  // shared key, so the menu opening onto this fetch never runs its own queryFn.
  const skillSlotKey = slotId ? `dashboard:${slotId}` : undefined
  const skillSlotKeyRef = useRef(skillSlotKey)
  skillSlotKeyRef.current = skillSlotKey
  const skillProjectRef = useRef(project)
  skillProjectRef.current = project
  const prefetchSkills = useCallback(() => {
    queryClient.prefetchQuery({
      queryKey: ['skills', skillSlotKey ?? null, project ?? null, agentName ?? null],
      queryFn: ({ signal }) => api.skills(skillSlotKey, agentName, signal),
      staleTime: skillsCacheStaleTime(project),
    })
  }, [queryClient, skillSlotKey, project, agentName])
  // Shared caret-relative token insertion for the @/$ pickers: replace the
  // sigil-token ending at the caret with `token`, commit, and restore the caret
  // just after it. One copy keeps the two onSelect handlers duplication-free.
  const applyPickedToken = useCallback((tokenRe: RegExp, token: string) => {
    const selection = composerControl()?.getSelection()
    const next = replaceTokenAtCaret(value, selection?.start ?? value.length, tokenRe, token)
    onChange(next.value)
    requestAnimationFrame(() => composerControl()?.setSelection(next.caret, next.caret, { focus: true }))
  }, [value, onChange, composerControl])

  return {
    slashMenuOpen, setSlashMenuOpen, filePickerOpen, setFilePickerOpen, fileQuery, setFileQuery,
    pathPickerOpen, setPathPickerOpen, pathQuery, setPathQuery, skillPickerOpen, setSkillPickerOpen, skillQuery, setSkillQuery,
    nextTrustRequestIdRef, activeTrustRequestIdRef, trustPrompt, setTrustPrompt, skillSlotKey, skillSlotKeyRef, skillProjectRef,
    anyPickerOpenRef, closePickers, openPickersForText, prefetchSkills, applyPickedToken,
  }
}
