import type { ReactNode } from 'react'

interface FallbackFile {
  name: string
  contents: string
}

interface FilePairFallbackOptions {
  collapsed?: boolean
  disableFileHeader?: boolean
}

const OLD_FILE_MARKER = '-'.repeat(3)
const NEW_FILE_MARKER = '+'.repeat(3)

/** Preserve both sides while Pierre is loading or its workers recover. */
export function filePairFallbackText(oldFile: FallbackFile | null, newFile: FallbackFile | null): string {
  if (oldFile && newFile) {
    return `${OLD_FILE_MARKER} ${oldFile.name}\n${oldFile.contents}\n${NEW_FILE_MARKER} ${newFile.name}\n${newFile.contents}`
  }
  return (newFile ?? oldFile)?.contents ?? ''
}

export function FilePairPlainFallback({ oldFile, newFile, options, renderHeaderMetadata, renderHeaderPrefix, renderHeaderFilenameSuffix }: {
  oldFile: FallbackFile | null
  newFile: FallbackFile | null
  options?: FilePairFallbackOptions
  renderHeaderMetadata?: () => ReactNode
  renderHeaderPrefix?: () => ReactNode
  renderHeaderFilenameSuffix?: () => ReactNode
}) {
  const file = newFile ?? oldFile
  const header = options?.disableFileHeader === false ? (
    <>
      {renderHeaderPrefix?.()}
      <span data-title="" className="min-w-0 flex-1 truncate">{file?.name}</span>
      {renderHeaderFilenameSuffix?.()}
      {renderHeaderMetadata?.()}
    </>
  ) : undefined
  return (
    <PlainCodeFallback
      text={filePairFallbackText(oldFile, newFile)}
      header={header}
      hideBody={options?.collapsed === true}
    />
  )
}

/** Plain-text stand-in used while the Pierre chunk loads and for patch text
 *  that does not (yet) parse — e.g. the partial frames of a streaming diff.
 *  Text content matches the final render, so the swap is a restyle, not a
 *  content reflow. */
export function PlainCodeFallback({ text, header, hideBody = false }: {
  text: string
  header?: ReactNode
  hideBody?: boolean
}) {
  return (
    <>
      {header != null ? (
        <div data-diffs-header="" className="flex min-h-9 items-center justify-end gap-1 border-b border-border px-2 py-1">
          {header}
        </div>
      ) : null}
      {!hideBody ? (
        <pre className="m-0 px-3 py-2 overflow-x-auto text-[13px] font-mono leading-relaxed whitespace-pre">
          {text}
        </pre>
      ) : null}
    </>
  )
}
