/**
 * builtFromName — the name a crewmate's "Built from" shows, never an internal id.
 *
 * A crewmate bound to a shared template shows that template's name. A crewmate
 * that owns its config file is bound to the file, whose name is the crewmate's
 * own key (often a generated id), and its chat runs on a source-defined agent
 * (`kirocrew-mate-conductor`); neither is a template the user picked. It shows
 * the template its worker kit was last copied from, and the default template
 * when it was never copied from another.
 */
import { useQuery } from '@tanstack/react-query'
import { i18nT } from '../../i18n/t'
import { crewWorkerKitApi, crewWorkerKitKey, type WorkerKitView } from '../../api/crewCapabilities'
import { workerKitErrorText } from './workerKitErrors'

/** The built-in template a crewmate starts from unless another is picked. */
export const DEFAULT_TEMPLATE = 'kirocrew'

/** Source-defined agents a crewmate runs on that are not templates anyone picks. */
const SOURCE_DEFINED = new Set(['kirocrew-mate-conductor', 'kirocrew-mate-worker'])

/** A template's name as the user sees it: the default one is labelled as such. */
export function templateDisplayName(name: string): string {
  if (!name || name === DEFAULT_TEMPLATE || SOURCE_DEFINED.has(name)) {
    return i18nT('pages.membersPage.built_from_default', { agent: DEFAULT_TEMPLATE })
  }
  return name
}

/**
 * What a crewmate is built from, for display. `ownsFile` is whether its binding
 * is its own config file (the capability view's `mode === 'mate'`); then the
 * worker kit names the template, read only while `enabled`. A kit that cannot be
 * read is returned as `error` with no name, so the caller shows the failure
 * rather than a provenance it does not know.
 */
export function useBuiltFromName({
  member,
  kiroAgent,
  ownsFile,
  enabled,
}: {
  member: string
  kiroAgent: string
  ownsFile: boolean
  enabled: boolean
}): { name: string; error: string | null } {
  const kit = useQuery({
    queryKey: crewWorkerKitKey(member),
    queryFn: () => crewWorkerKitApi.get(member),
    enabled: enabled && ownsFile && !!member,
    retry: false,
  })
  if (!ownsFile) return { name: templateDisplayName(kiroAgent), error: null }
  if (kit.isError) return { name: '', error: workerKitErrorText(kit.error) }
  return { name: kitTemplateName(kit.data) ?? templateDisplayName(DEFAULT_TEMPLATE), error: null }
}

/**
 * The name a worker kit's template shows, or `null` when no kit was copied. A
 * private copy (`crew-<hex>`) is named by the template it was forked from, and
 * by "its own copy" when that is unknown, never by its id.
 */
export function kitTemplateName(kit: Pick<WorkerKitView, 'template' | 'origin' | 'private_copy'> | undefined): string | null {
  if (!kit?.template) return null
  if (kit.private_copy) {
    return kit.origin ? templateDisplayName(kit.origin) : i18nT('pages.membersPage.built_from_own_copy')
  }
  return templateDisplayName(kit.template)
}
