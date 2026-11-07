import { createContext } from 'react'

/** Fence tags that the host of a transcript draws wrapped, as `CodeBlock`'s own
 *  prose tags are: the first word of the info string, in lower case.
 *  `ChatEmbed` fills it from its `proseFences` prop. Every other surface sees
 *  the empty default, so a tag an app defines changes only that app's chat. */
export const ProseFencesContext = createContext<ReadonlySet<string>>(new Set())

/** The context value for a host's list: trimmed, lower case, empty tags dropped. */
export function proseFenceSet(tags: readonly string[]): ReadonlySet<string> {
  return new Set(tags.map(t => t.trim().toLowerCase()).filter(Boolean))
}
