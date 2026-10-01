import { Component, lazy, useCallback, useMemo, useRef, useState } from 'react'
import { textareaDropTargetAtPoint } from '../../utils/textareaPointOffset'
import type { ComposerControl } from '../composerControl'

/* The two editor engines behind one `ComposerControl`: the production
   textarea, and the opt-in Lexical editor loaded as a lazy chunk whose load
   failure falls back to the textarea with the draft intact. */
export const LexicalComposerInput = lazy(() => import('../LexicalComposerInput'))

export class ComposerLoadBoundary extends Component<
  { children: React.ReactNode; onError: () => void },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // The fallback is deliberately seamless for the USER (the textarea composer
    // takes over with the draft intact), but the failure must never be silent
    // for the OPERATOR: a failing editor chunk after a deploy would otherwise
    // disable the opt-in path fleet-wide with nothing to diagnose. Same
    // convention as AppHost's boundary.
    // eslint-disable-next-line no-console -- surface composer chunk failures for debugging
    console.error('[ChatInput] Lexical composer failed to load; falling back to textarea:', error, info.componentStack)
    this.props.onError()
  }

  render() {
    return this.state.failed ? null : this.props.children
  }
}

export function useComposerEngine({ lexicalComposer }: { lexicalComposer: boolean }) {
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const composerAnchorRef = useRef<HTMLElement | null>(null)
  const lexicalControlRef = useRef<ComposerControl | null>(null)
  const [lexicalLoadFailed, setLexicalLoadFailed] = useState(false)
  const [lexicalFailedNoticeDismissed, setLexicalFailedNoticeDismissed] = useState(false)
  const [lexicalControlRevision, setLexicalControlRevision] = useState(0)
  const markLexicalReady = useCallback(() => {
    composerAnchorRef.current = lexicalControlRef.current?.getRootElement() ?? null
    setLexicalControlRevision(value => value + 1)
  }, [])
  const textareaControl = useMemo<ComposerControl>(() => ({
    focus: () => inputRef.current?.focus(),
    getRootElement: () => inputRef.current,
    getSelection: () => {
      const textarea = inputRef.current
      if (!textarea) return null
      return {
        start: textarea.selectionStart ?? 0,
        end: textarea.selectionEnd ?? textarea.selectionStart ?? 0,
      }
    },
    setSelection: (start, end = start, options) => {
      const textarea = inputRef.current
      if (!textarea) return
      const boundedStart = Math.min(start, textarea.value.length)
      const boundedEnd = Math.min(end, textarea.value.length)
      textarea.setSelectionRange(boundedStart, boundedEnd)
      if (options?.focus) textarea.focus()
    },
    dropTargetAtPoint: (clientX, clientY, adjust) => {
      const textarea = inputRef.current
      return textarea ? textareaDropTargetAtPoint(textarea, clientX, clientY, adjust) : null
    },
  }), [])
  const composerControl = useCallback(
    () => lexicalComposer && !lexicalLoadFailed ? lexicalControlRef.current : textareaControl,
    [lexicalComposer, lexicalLoadFailed, textareaControl],
  )
  const setTextareaRef = useCallback((textarea: HTMLTextAreaElement | null) => {
    inputRef.current = textarea
    if (textarea || !lexicalComposer || lexicalLoadFailed) composerAnchorRef.current = textarea
  }, [lexicalComposer, lexicalLoadFailed])

  return {
    inputRef, composerAnchorRef, lexicalControlRef, lexicalLoadFailed, setLexicalLoadFailed,
    lexicalFailedNoticeDismissed, setLexicalFailedNoticeDismissed, lexicalControlRevision, markLexicalReady,
    composerControl, setTextareaRef,
  }
}
