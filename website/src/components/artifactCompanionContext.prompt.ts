import type { Artifact } from '../types'

/**
 * The background context a companion chat is injected with, naming the artifact
 * the user is looking at. Addressed to the agent, so it names the slug and the
 * tool calls to make. A `*.prompt.ts` module, so it stays English by design:
 * translating it would change what the agent does, not the interface language.
 */
export function artifactCompanionContextPrompt(
  artifact: Pick<Artifact, 'slug' | 'name' | 'kind' | 'version' | 'source_path'>,
  commentCount: number,
): string {
  return (
    `Companion chat for artifact \`${artifact.slug}\` ("${artifact.name}", kind=${artifact.kind}, ` +
    `v${artifact.version}, source_path=${artifact.source_path || 'none'}, ` +
    `${commentCount} open comment${commentCount === 1 ? '' : 's'}).\n` +
    `Use artifact_get / artifact_update with this slug, and artifact_get_comments ` +
    `with this slug and exclude_resolved=true, so you read the same open comments ` +
    `counted here. ` +
    `Anchored comments carry the exact quoted span they refer to — treat each as ` +
    `an instruction about that span, and triage every one you act on.`
  )
}
