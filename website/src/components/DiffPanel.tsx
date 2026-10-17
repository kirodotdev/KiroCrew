import { memo, useMemo } from 'react'
import { PierreFilePair } from '../pierre'
import Clickable from './Clickable'
import { copyToClipboard } from '../utils/clipboard'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/**
 * Side-by-side / unified diff viewer (Pierre-rendered).
 * Used inside DetailPanel for the standalone diff tab (minimal file chips,
 * side-panel diff mode).
 */
export default memo(function DiffPanel({ filePath, original, modified, sideBySide = true, lineNumbers = false }: {
  filePath: string
  original: string
  modified: string
  sideBySide?: boolean
  lineNumbers?: boolean
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Show the banner only when both sides carry content and it's the same.
  // Both-empty is a degenerate "new empty file" state, not a meaningful
  // identical comparison — let it fall through to the diff gracefully.
  const isIdentical = original === modified && (!!original || !!modified)

  const oldFile = useMemo(
    () => (original ? { name: filePath, contents: original } : null),
    [filePath, original],
  )
  const newFile = useMemo(
    () => (modified ? { name: filePath, contents: modified } : null),
    [filePath, modified],
  )
  const options = useMemo(
    () => ({
      diffStyle: (sideBySide ? 'split' : 'unified') as 'split' | 'unified',
      disableLineNumbers: !lineNumbers,
    }),
    [sideBySide, lineNumbers],
  )

  return (
    <div className="relative w-full h-full flex flex-col">
      {isIdentical ? (
        <div className="flex-1 flex items-center justify-center">
          <span className="text-muted text-sm">
            {i18nT('components.diffPanel.contents_identical')}
          </span>
        </div>
      ) : (
        <div className="flex-1 overflow-auto pierre-surface">
          <PierreFilePair oldFile={oldFile} newFile={newFile} options={options} />
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
