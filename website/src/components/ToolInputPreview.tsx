import { useState } from 'react'
import { ToolInputText } from './ToolInputText'

import { i18nT } from '../i18n/t'
const _DEFAULT_THRESHOLD = 500

export default function ToolInputPreview({ toolInput, threshold = _DEFAULT_THRESHOLD }: { toolInput: string; threshold?: number }) {
  const [expanded, setExpanded] = useState(false)
  const needsExpand = toolInput.length > threshold
  return (
    <div className="mt-1.5">
      {needsExpand && !expanded ? (
        <>
          <pre className="bg-bg-hover rounded-md px-3 py-2 text-[13px] font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-[4.5em] overflow-y-auto"><ToolInputText text={toolInput.slice(0, threshold)} />…</pre>
          <button className="text-accent text-[13px] mt-1 cursor-pointer bg-transparent border-none font-body hover:underline" onClick={() => setExpanded(true)}>{i18nT('components.toolInputPreview.show_full_command')}</button>
        </>
      ) : (
        <>
          <pre className="bg-bg-hover rounded-md px-3 py-2 text-[13px] font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-[40vh] overflow-y-auto"><ToolInputText text={toolInput} /></pre>
          {needsExpand && <button className="text-accent text-[13px] mt-1 cursor-pointer bg-transparent border-none font-body hover:underline" onClick={() => setExpanded(false)}>{i18nT('components.toolInputPreview.collapse')}</button>}
        </>
      )}
    </div>
  )
}
