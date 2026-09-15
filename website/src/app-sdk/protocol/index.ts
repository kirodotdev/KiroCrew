/**
 * The marker protocol between an agent's message text and any surface that renders it.
 *
 * The agent encodes UI affordances inline in prose — follow-up choices as
 * `[OPTIONS: a | b]` / `[OPTION: a | b]`, and a mid-turn steer acknowledgement as
 * `[STEERING steer-<id>: …]`. A transcript that does not strip and interpret them shows
 * the raw syntax to the user, and one that strips them without offering the affordance
 * deletes the user's choices outright.
 *
 * This module is deliberately React-free and dependency-free so every surface — the main
 * chat, a split pane, the side panel, and an embedding app — reads the protocol from the
 * same place instead of re-deriving it. Keep it that way: a parser that lives in a
 * component is only available to surfaces that render that component.
 */
// `OPTION_MARKER_RE` is deliberately NOT re-exported: it is a g-flagged regex, so handing it
// across a module boundary hands out mutable `lastIndex` state that breaks this module's own
// parsing. In-tree callers that only ever `.replace()` with it import `./optionMarker` directly.
export { stripPartialOptionMarker } from './optionMarker'
export { parseOptions, deriveFollowUpOptions } from './options'
export type { ParsedOptions, FollowUpDerivation } from './options'
export { extractSteeringAcks } from './steering'
// `deriveFollowUpOptions` consumes these, so a caller can annotate its own transcript without
// reaching into the dashboard's own type tree. Type-only: it adds no runtime dependency.
export type { ChatMessage } from '../../types'
