import { Fragment, type ReactNode } from 'react'

import { addressNamedByMessage } from '../api/edgeAuthChallenge'

/**
 * Render an error message, linking the address it names -- if it names one.
 *
 * Only ONE produced message ever does: the framed proxy challenge, which has to carry
 * an address because a nested pane cannot complete a sign-in inside itself and its
 * reader cannot see the address bar (that shows the outer dashboard's URL). As inert
 * text that address has to be hand-copied at the moment the reader is locked out.
 *
 * Which message that is belongs to the producer, not here: `addressNamedByMessage`
 * answers null for everything else, so an error whose text the far side authored cannot
 * become a link no matter what it contains. A general URL linkifier would be unsafe --
 * `friendlyErrText` passes server-authored text through, so a hostile 403 body naming a
 * same-origin action route would be offered to the reader as something to click.
 */
export function withOriginLink(message: string): ReactNode {
  const origin = addressNamedByMessage(message)
  if (origin === null) return message

  const at = message.indexOf(origin)
  if (at < 0) return message
  const parts = [
    message.slice(0, at),
    <a
      key="origin"
      href={origin}
      target="_blank"
      rel="noopener noreferrer"
      className="underline hover:no-underline"
    >
      {origin}
    </a>,
    message.slice(at + origin.length),
  ]
  return <>{parts.map((part, i) => <Fragment key={i}>{part}</Fragment>)}</>
}
