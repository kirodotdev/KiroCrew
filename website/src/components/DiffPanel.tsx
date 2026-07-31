import { memo, lazy, Suspense } from 'react'
import { monacoLang, useIsDark } from './MonacoCodeBlock'
import { kirocrewDark, kirocrewLight } from './monacoTheme'
import Clickable from './Clickable'
import { copyToClipboard } from '../utils/clipboard'

import { i18nT } from '../i18n/t'
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
  // Show the banner only when both sides carry content and it's the same.
  // Both-empty is a degenerate "new empty file" state, not a meaningful
  // identical comparison — let it fall through to the editor gracefully.
  const isIdentical = original === modified && (!!original || !!modified)

  return (
    <div className="relative w-full h-full flex flex-col">
      {isIdentical ? (
        <div className="flex-1 flex items-center justify-center">
          <span className="text-muted text-sm">
            {i18nT('components.diffPanel.contents_identical')}
          </span>
        </div>
      ) : (
        <div className="flex-1 overflow-hidden">
          <Suspense fallback={<div className="flex items-center justify-center h-full text-muted text-sm">{i18nT('components.diffPanel.loading_diff')}</div>}>
            <MonacoDiffEditor
              original={original}
              modified={modified}
              language={lang}
              theme={isDark ? 'kirocrew-dark' : 'kirocrew-light'}
              beforeMount={(monaco) => {
                monaco.editor.defineTheme('kirocrew-dark', kirocrewDark)
                monaco.editor.defineTheme('kirocrew-light', kirocrewLight)
              }}
              onMount={(editor) => {
                // Jump to the first change once the diff is computed (async).
                const nav = editor.onDidUpdateDiff(() => {
                  nav.dispose()
                  const first = editor.getLineChanges()?.[0]
                  if (first) editor.getModifiedEditor().revealLineInCenter(first.modifiedStartLineNumber || first.modifiedEndLineNumber || 1)
                })
              }}
              options={{
                readOnly: true,
                renderSideBySide: sideBySide,
                minimap: { enabled: false },
                scrollBeyondLastLine: false,
                fontSize: 13,
                lineNumbers: lineNumbers ? 'on' : 'off',
                automaticLayout: true,
                renderValidationDecorations: 'off',
                guides: { indentation: false },
                stickyScroll: { enabled: false },
                renderLineHighlight: 'none',
                scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
              }}
              height="100%"
            />
          </Suspense>
        </div>
      )}
      <Clickable
        className="shrink-0 flex items-center px-5 py-3 border-t border-border text-[11px] font-mono truncate text-muted cursor-pointer hover:text-text transition-colors"
        title={i18nT('components.diffPanel.click_to_copy_path')}
        onClick={() => copyToClipboard(filePath)}
      >
        {filePath}
      </Clickable>
    </div>
  )
})
