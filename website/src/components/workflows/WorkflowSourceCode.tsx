import { useMemo } from 'react'

import { CodeEditor } from '../../components/CodeEditor'
import { PierreCode } from '../../pierre'

const READ_ONLY_OPTIONS = {
  disableLineNumbers: false,
  overflow: 'scroll' as const,
}

export default function WorkflowSourceCode({
  source,
  sourceFormat = 'python',
  onChange,
  ariaLabel,
  compact = false,
}: {
  source: string
  sourceFormat?: 'python' | 'task-plan'
  onChange?: (value: string) => void
  ariaLabel: string
  compact?: boolean
}) {
  const language = sourceFormat === 'task-plan' ? 'yaml' : 'python'
  const filename = sourceFormat === 'task-plan' ? 'workflow.yaml' : 'workflow.py'
  const file = useMemo(
    () => ({ name: filename, contents: source }),
    [filename, source],
  )
  const editable = onChange !== undefined

  return (
    <div
      role="region"
      aria-label={ariaLabel}
      tabIndex={editable ? undefined : 0}
      data-workflow-source-mode={editable ? 'editable' : 'read-only'}
      className={`relative w-full overflow-hidden rounded-md border border-border bg-bg-elevated font-mono text-[13px] leading-normal ${compact ? 'h-56' : 'h-[340px]'}`}
    >
      {editable ? (
        <CodeEditor
          content={source}
          lang={language}
          lineNums
          wordWrap={false}
          onChange={onChange}
          flush
          filePath={filename}
        />
      ) : (
        <PierreCode
          file={file}
          langHint={language}
          options={READ_ONLY_OPTIONS}
          scrollClassName="absolute inset-0 overflow-auto pierre-surface"
        />
      )}
    </div>
  )
}
