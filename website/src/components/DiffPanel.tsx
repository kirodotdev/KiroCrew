import { memo, lazy, Suspense } from 'react'
import { monacoLang, useIsDark } from './MonacoCodeBlock'
import { kirocrewDark, kirocrewLight } from './monacoTheme'
import Clickable from './Clickable'
import { copyToClipboard } from '../utils/clipboard'

const MonacoDiffEditor = lazy(async () => {
  const { ensureMonacoLocal } = await import('../utils/monacoLocal')
  await ensureMonacoLocal()
  const { DiffEditor } = await import('@monaco-editor/react')
  return { default: DiffEditor }
})

function extOf(fp: string) {
  const i = fp.lastIndexOf('.')
  return i >= 0 ? fp.slice(i + 1).toLowerCase() : ''
}

/**
 * Side-by-side / unified Monaco diff viewer.
 * Used inside DetailPanel when a file-change chip is clicked.
 */
export default memo(function DiffPanel({ filePath, original, modified, sideBySide = true, lineNumbers = false }: {
  filePath: string
  original: string
  modified: string
  sideBySide?: boolean
  lineNumbers?: boolean
}) {
  const isDark = useIsDark()
  const lang = monacoLang(extOf(filePath)) || 'plaintext'

  return (
    <div className="relative w-full h-full flex flex-col">
      <div className="flex-1 overflow-hidden">
        <Suspense fallback={<div className="flex items-center justify-center h-full text-muted text-sm">Loading diff…</div>}>
          <MonacoDiffEditor
            original={original}
            modified={modified}
            language={lang}
            theme={isDark ? 'kirocrew-dark' : 'kirocrew-light'}
            beforeMount={(monaco) => {
              monaco.editor.defineTheme('kirocrew-dark', kirocrewDark)
              monaco.editor.defineTheme('kirocrew-light', kirocrewLight)
            }}
            options={{
              readOnly: true,
              renderSideBySide: sideBySide,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              fontSize: 13,
              lineNumbers: lineNumbers ? 'on' : 'off',
              automaticLayout: true,
              scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
            }}
            height="100%"
          />
        </Suspense>
      </div>
      <Clickable
        className="shrink-0 flex items-center px-5 py-3 border-t border-border text-[11px] font-mono truncate text-muted cursor-pointer hover:text-text transition-colors"
        title="Click to copy path"
        onClick={() => copyToClipboard(filePath)}
      >
        {filePath}
      </Clickable>
    </div>
  )
})
