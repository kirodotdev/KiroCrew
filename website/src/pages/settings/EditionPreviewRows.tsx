import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'
import { getFeaturePreviewRows } from './featurePreviewRows'

export const EDITION_PREVIEW_VALUES_KEY = ['featurePreviewEdition'] as const

type Values = { values: Record<string, boolean | null> }

/** A gateway without the route, or a caller who is not the owner: no rows, not an error. */
function isRefusal(err: unknown): boolean {
  const status = (err as { status?: unknown } | null)?.status
  return status === 403 || status === 404
}

/**
 * Settings > Developer > Feature Previews: the rows an edition registered with
 * `registerFeaturePreviewRows`. Each switch reads and writes its config key
 * through `/api/feature-previews/edition`, which serves only the keys the
 * edition named on the gateway; a row whose key is not in the answer is not
 * drawn. With no rows registered nothing is fetched. A gateway without the
 * route (404) or a caller who is not the owner (403) renders nothing; any
 * other failed read says so, so it never looks like a gateway without rows.
 */
export function EditionPreviewRows() {
  const rows = getFeaturePreviewRows()
  const qc = useQueryClient()
  const valuesQ = useQuery({
    queryKey: EDITION_PREVIEW_VALUES_KEY,
    queryFn: () => api.featurePreviewEditionValues(),
    enabled: rows.length > 0,
    retry: false,
  })
  const [saved, setSaved] = useState<Record<string, boolean>>({})
  const [failed, setFailed] = useState<string | null>(null)
  const toggle = useMutation({
    mutationFn: ({ configKey, enabled }: { configKey: string; enabled: boolean }) =>
      api.setFeaturePreviewEditionValue(configKey, enabled),
    onMutate: () => setFailed(null),
    onSuccess: res => {
      qc.setQueryData<Values>(EDITION_PREVIEW_VALUES_KEY, old =>
        old ? { values: { ...old.values, [res.config_key]: res.enabled } } : old)
      setSaved(s => ({ ...s, [res.config_key]: true }))
    },
    onError: (_e, vars) => setFailed(vars.configKey),
  })
  if (valuesQ.isError && !isRefusal(valuesQ.error)) {
    return (
      <SettingsCard>
        <ErrorNotice
          variant="inline"
          askAgent
          message={i18nT('sharedCrew.adv_load_failed')}
          testId="edition-preview-rows-load-error"
        />
      </SettingsCard>
    )
  }
  const values = valuesQ.data?.values
  if (!values) return null

  return (
    <>
      {rows.filter(row => row.configKey in values).map(row => (
        <SettingsCard key={row.id}>
          <div data-testid={`edition-preview-row-${row.id}`}>
            <SettingsToggle
              label={row.label}
              description={row.description}
              checked={values[row.configKey] ?? row.defaultValue}
              disabled={toggle.isPending}
              onChange={enabled => toggle.mutate({ configKey: row.configKey, enabled })}
            />
            {row.appliesOnRestart && saved[row.configKey] && (
              <p className="text-[12px] text-warn pt-1" data-testid={`edition-preview-row-${row.id}-restart`}>
                {i18nT('pages.settings.slackPanel.saved_restart_the_gateway_to_apply')}
              </p>
            )}
            {failed === row.configKey && (
              <ErrorNotice variant="inline" askAgent message={i18nT('pages.settings.securityPanel.third_party_apps_save_failed')} />
            )}
          </div>
        </SettingsCard>
      ))}
    </>
  )
}
