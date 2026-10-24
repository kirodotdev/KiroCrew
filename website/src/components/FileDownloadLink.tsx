import { useState, type MouseEvent, type ReactNode } from 'react'

import ErrorNotice from './ErrorNotice'
import { downloadFileToDisk, fileDownloadUrl } from '../utils/fileReadUrl'

/**
 * A Download link for a project file that goes through `downloadFileToDisk`.
 *
 * It stays a real `<a href download>`, so copying the link or opening it in a
 * new tab still works. A plain click is taken over, because a bare navigation
 * cannot read the endpoint's answer: a file over the scan limit answers 409 and
 * must ask the owner first, and a refused file must say why instead of saving
 * the refusal's JSON as the file.
 *
 * A failure renders right after the link unless the host passes `onError`:
 * a host whose link sits in a row of file actions takes the message and shows
 * it in a row of its own, so that row never carries the notice's controls.
 */
export default function FileDownloadLink({
  filePath,
  download = true,
  className,
  ariaLabel,
  onError,
  children,
}: {
  filePath: string
  /** The `download` attribute: a file name, or `true` for the URL's own. */
  download?: string | true
  className?: string
  ariaLabel?: string
  /** Take the failure message (and `null` on a new attempt) instead of rendering it here. */
  onError?: (message: string | null) => void
  children: ReactNode
}) {
  const [error, setError] = useState<string | null>(null)
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    // A modified click (new tab, save link) keeps the browser's own behaviour.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    const report = onError ?? setError
    report(null)
    void downloadFileToDisk(filePath, report)
  }
  return (
    <>
      <a
        href={fileDownloadUrl(filePath)}
        download={download === true ? '' : download}
        className={className}
        aria-label={ariaLabel}
        onClick={onClick}
      >
        {children}
      </a>
      <ErrorNotice
        variant="inline"
        className="text-left whitespace-normal"
        message={error}
        onDismiss={() => setError(null)}
        askAgent
        testId="file-download-link-error"
      />
    </>
  )
}
