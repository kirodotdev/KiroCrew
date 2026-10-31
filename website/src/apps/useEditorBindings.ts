/**
 * The file panel's end of `contributes.editorBindings`: reads the installed apps,
 * builds the dispatcher the editor fires through, and owns the result card a
 * binding fills (a picker for several locations, markdown, or a loading notice).
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import {
  contributedEditorBindings,
  bindingFor,
  invokeEditorBinding,
  type BindingCardView,
  type EditorBindingAppRecord,
} from './editorBindingContributions'
import type { EditorBindingDispatcher } from './editorBindingsContext'
import { applyEditorBindingOverrides } from './editorBindingOverrides'
import { useEditorBindingOverrides } from './useEditorBindingOverrides'
import { ApiError } from '../api/apiError'
import { appDisplayName } from '../components/appstore/appManifest'
import { i18nT } from '../i18n/t'
import type { ContributedEditorBinding } from './editorBindingContributions'

/** The app's name as the App Store shows it, or its id when it is not in the list. */
function appTitle(apps: unknown, name: string): string {
  const app = Array.isArray(apps) ? apps.find(a => (a as { name?: unknown })?.name === name) : undefined
  return app ? appDisplayName(app) : name
}

/** A 404 the gateway gave because nothing is mounted there: an app's own carries a `code`. */
function unmounted(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 404) return false
  try { return !(JSON.parse(err.body) as { code?: unknown } | null)?.code } catch { return true }
}

/**
 * The sentence for a failed binding: which app, which binding, and why. A route the
 * gateway never mounted answers "not found", which on its own names neither.
 */
export function bindingFailure(binding: ContributedEditorBinding, app: string, err: unknown): string {
  const reason = unmounted(err)
    ? i18nT('components.editorBindings.not_served', { endpoint: binding.endpoint })
    : (err as Error)?.message || String(err)
  return i18nT('components.editorBindings.failed', { app, action: binding.label, reason })
}

export interface EditorBindingsState {
  /** For the editor's key and pointer handlers; null when no enabled app binds anything. */
  dispatcher: EditorBindingDispatcher | null
  /** What the result card over the editor shows; null when closed. */
  card: BindingCardView | null
  closeCard: () => void
}

export function useEditorBindings({ filePath, dirty, text, open, onStale, onError }: {
  filePath: string
  /** Whether the buffer differs from the file on disk; sent with every binding. */
  dirty: boolean
  /** The buffer as shown. Hashed for every binding; sent only after an app says `stale`. */
  text: () => string
  /** Move the editor to a location: in this file (`sameFile`), or by opening another. */
  open: (location: { path: string; line: number; character?: number }, sameFile: boolean) => void
  /** The app's copy is behind and this binding may not be sent the text: tell the reader to save. */
  onStale: () => void
  /** A binding failed: the sentence names the app, the binding and the cause. */
  onError: (message: string) => void
}): EditorBindingsState {
  const { data: apps } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    enabled: false,
  })
  // The reader's own chords from Settings → Shortcuts replace the apps' defaults.
  const overrides = useEditorBindingOverrides()
  const bindings = useMemo(
    () => applyEditorBindingOverrides(contributedEditorBindings((apps ?? []) as EditorBindingAppRecord[]), overrides),
    [apps, overrides],
  )
  const [card, setCard] = useState<BindingCardView | null>(null)
  // A request outlives the render that started it: a slower answer must not
  // reopen a card the reader already closed, or replace a newer binding's card.
  const generation = useRef(0)
  const live = useRef({ dirty, text, open, onStale, onError })
  live.current = { dirty, text, open, onStale, onError }
  const bound = useCallback(
    (trigger: string) => bindingFor(bindings, trigger, filePath) !== null,
    [bindings, filePath],
  )
  const fire = useCallback((trigger: string, at: { line: number; character: number }) => {
    const binding = bindingFor(bindings, trigger, filePath)
    if (!binding) return false
    const mine = ++generation.current
    void invokeEditorBinding(
      binding,
      { path: filePath, line: at.line, character: at.character, dirty: live.current.dirty, trigger },
      {
        text: () => live.current.text(),
        open: (loc, sameFile) => live.current.open(loc, sameFile),
        show: view => { if (generation.current === mine) setCard(view) },
        onStale: () => live.current.onStale(),
        onError: err => live.current.onError(bindingFailure(binding, appTitle(apps, binding.app), err)),
      },
    )
    return true
  }, [apps, bindings, filePath])
  const closeCard = useCallback(() => { generation.current++; setCard(null) }, [])
  const dispatcher = useMemo(() => (bindings.length ? { bound, fire } : null), [bindings, bound, fire])
  return { dispatcher, card, closeCard }
}
