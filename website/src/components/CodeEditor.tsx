import { useCallback, useMemo, useRef } from 'react'

import { PierreEditor, type PierreEditorHandle } from '../pierre'

/** Pierre-based code editor for editable text content. */
export function CodeEditor({
  content, lang, lineNums, wordWrap, onChange, onSave, flush, editorRef, filePath, diffBase, diffSplit, diffExpandUnchanged,
}: {
  content: string
  lang: string
  lineNums: boolean
  wordWrap: boolean
  onChange: (v: string) => void
  /** Cmd/Ctrl+S inside the editor surface. */
  onSave?: () => void
  /** Drop the rounded border box — the host surface provides the frame. */
  flush?: boolean
  /** Imperative reveal/focus handle — how a host jumps to a cited line. */
  editorRef?: React.Ref<PierreEditorHandle>
  filePath?: string
  /** Live-diff editing baseline (`null` = new file; `undefined` = plain editor). */
  diffBase?: string | null
  /** Live-diff surface: show unchanged regions instead of folding them. */
  diffExpandUnchanged?: boolean
  /** Split vs unified layout for the live-diff surface. */
  diffSplit?: boolean
}) {
  // Pierre owns the buffer during an edit session. Keep its file identity
  // stable for changes it emitted, but reseed on an external source change.
  const initialRef = useRef<{ key: string; file: { name: string; contents: string; cacheKey: string } }>()
  const lastEmittedRef = useRef<string | null>(null)
  const lastContentRef = useRef<string | null>(null)
  const seedRef = useRef(0)
  const key = `${filePath ?? ''}:${lang}:${diffBase === undefined ? 'plain' : 'diff'}`
  const seedFile = () => ({
    key,
    file: {
      name: filePath?.split('/').pop() || `file.${lang}`,
      contents: content,
      cacheKey: `edit:${key}:${seedRef.current}`,
    },
  })
  if (initialRef.current?.key !== key) {
    initialRef.current = seedFile()
  } else if (content !== lastContentRef.current) {
    if (content === lastEmittedRef.current) {
      lastEmittedRef.current = null
    } else {
      seedRef.current++
      initialRef.current = seedFile()
    }
  }
  lastContentRef.current = content
  const handleChange = useCallback((value: string) => {
    lastEmittedRef.current = value
    onChange(value)
  }, [onChange])
  const liveFile = useMemo(
    () => ({ ...initialRef.current!.file, contents: content }),
    [content],
  )
  const options = useMemo(
    () => ({
      disableLineNumbers: !lineNums,
      overflow: (wordWrap ? 'wrap' : 'scroll') as 'wrap' | 'scroll',
    }),
    [lineNums, wordWrap],
  )
  return (
    <div className={`w-full h-full overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}>
      <PierreEditor
        key={initialRef.current.file.cacheKey}
        ref={editorRef}
        file={liveFile}
        options={options}
        onChange={handleChange}
        onSave={onSave}
        diffBase={diffBase}
        diffSplit={diffSplit}
        diffExpandUnchanged={diffExpandUnchanged}
      />
    </div>
  )
}
