import { useId, useState } from 'react'
import { ToolInputText } from './ToolInputText'

import { i18nT } from '../i18n/t'
const _DEFAULT_THRESHOLD = 500

export default function ToolInputPreview({ toolInput, threshold = _DEFAULT_THRESHOLD }: { toolInput: string; threshold?: number }) {
  const [expanded, setExpanded] = useState(false)
  const needsExpand = toolInput.length > threshold
  // Stable id linking the toggle to the <pre> it controls, so a screen reader
  // announces the raw tool-input region this disclosure expands (Req 2.5).
  const regionId = useId()
  const preClass = 'bg-bg-hover rounded-md px-3 py-2 text-[13px] font-mono overflow-x-auto whitespace-pre-wrap break-all overflow-y-auto'
  const toggleClass = 'text-accent text-[13px] mt-1 cursor-pointer bg-transparent border-none font-body hover:underline'
  return (
    <div className="mt-1.5">
      {needsExpand && !expanded ? (
        <>
          <pre id={regionId} className={`${preClass} max-h-[4.5em]`}><ToolInputText text={toolInput.slice(0, threshold)} />…</pre>
          <button className={toggleClass} aria-expanded={false} aria-controls={regionId} onClick={() => setExpanded(true)}>{i18nT('components.toolInputPreview.show_full_command')}</button>
        </>
      ) : (
        <>
          <pre id={regionId} className={`${preClass} max-h-[40vh]`}><ToolInputText text={toolInput} /></pre>
          {needsExpand && <button className={toggleClass} aria-expanded={true} aria-controls={regionId} onClick={() => setExpanded(false)}>{i18nT('components.toolInputPreview.collapse')}</button>}
        </>
      )}
    </div>
  )
}
