import { useId } from 'react'
import { useMutation } from '@tanstack/react-query'

import { api } from '../../api/client'
import { ApiError, apiErrorCode } from '../../api/apiError'
import type { DecisionsSageMakerTestResult, DecisionsSignedHttpsTarget } from '../../api/client/decisions'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Input } from '../../components/ui'
import { fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** The picker's id for an HTTPS endpoint signed with AWS SigV4; the gateway's `PRESET_SIGNED_HTTPS`. */
export const PRESET_AWS_HTTPS = 'aws_https'
/** What the option asks to install when the gateway has no botocore. */
const BOTO3_INSTALL = 'pip install "boto3>=1.34,<2"'
export const EMPTY_AWS_HTTPS: DecisionsSignedHttpsTarget = { url: '', service: '', region: '', profile: '', model: '' }

export const sameSignedHttps = (a: DecisionsSignedHttpsTarget, b: DecisionsSignedHttpsTarget | null | undefined) =>
  !!b &&
  a.url === b.url &&
  a.service === b.service &&
  a.region === b.region &&
  a.profile === b.profile &&
  a.model === b.model

/** Whether the parts the gateway needs are filled in. */
export const signedHttpsComplete = (v: DecisionsSignedHttpsTarget) =>
  v.url.trim() !== '' && v.service.trim() !== '' && v.region.trim() !== ''

function refusedField(e: unknown): string {
  if (!(e instanceof ApiError)) return ''
  try {
    const field = (JSON.parse(e.body) as { field?: unknown }).field
    return typeof field === 'string' ? field : ''
  } catch {
    return ''
  }
}

/**
 * The sentence for a refused save or test of this option, from the gateway's code and
 * field, or `null` when the refusal is not about these fields.
 */
export function signedHttpsRefusal(e: unknown): string | null {
  const code = apiErrorCode(e)
  if (code === 'decisions_provider_aws_https_unknown_profile') {
    return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_unknown_profile')
  }
  if (code === 'decisions_provider_aws_https_unavailable') {
    return i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_boto3_missing', { command: BOTO3_INSTALL })
  }
  if (code !== 'decisions_provider_aws_https_invalid') return null
  switch (refusedField(e)) {
    case 'service':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_invalid_service')
    case 'region':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_invalid_region')
    case 'profile':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_invalid_profile')
    case 'model':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_invalid_model')
    default:
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_invalid_url')
  }
}

/** The sentence for a connection test that ran and got no answer, by the gateway's fixed word. */
function testFailure(error: string): string {
  switch (error) {
    case 'credentials':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_credentials')
    case 'timeout':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_timeout')
    case 'unavailable':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_test_unavailable')
    case 'http_error':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_test_http_error')
    case 'protocol':
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_protocol')
    default:
      return i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_failed')
  }
}

/**
 * The fields that name an HTTPS endpoint signed with AWS SigV4, and the connection test.
 *
 * The card sends the parts, never an address: the gateway checks each one and builds
 * the `aws-https://` address itself.
 */
export function DecisionsSignedHttpsFields({
  value,
  onChange,
  disabled,
  available,
  notSending,
}: {
  value: DecisionsSignedHttpsTarget
  onChange: (next: DecisionsSignedHttpsTarget) => void
  disabled: boolean
  available: boolean
  /** The saved endpoint is shown and has no consent yet, so nothing reaches it. */
  notSending: boolean
}) {
  const baseId = useId()
  const testMut = useMutation<DecisionsSageMakerTestResult, unknown, DecisionsSignedHttpsTarget>({
    mutationFn: target => api.testDecisionsSignedHttps(target),
  })
  // A result describes the fields it was run with; editing them retires it.
  const edit = (patch: Partial<DecisionsSignedHttpsTarget>) => {
    testMut.reset()
    onChange({ ...value, ...patch })
  }
  const field = (key: keyof DecisionsSignedHttpsTarget, label: string, placeholder: string) => (
    <label htmlFor={`${baseId}-${key}`} className="flex flex-col gap-0.5 text-[12px] text-text">
      {label}
      <Input
        id={`${baseId}-${key}`}
        type="text"
        autoComplete="off"
        spellCheck={false}
        className="font-mono text-[12px]"
        placeholder={placeholder}
        value={value[key]}
        disabled={disabled || !available}
        onChange={e => edit({ [key]: e.target.value.trim() })}
      />
    </label>
  )
  const result = testMut.data
  return (
    <div
      className="flex flex-col gap-1.5 rounded-md border border-border bg-bg-accent px-2.5 py-1.5"
      data-testid="decisions-aws-https-fields"
    >
      {!available && (
        <p className="text-[12px] text-warn m-0" role="status">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_boto3_missing', { command: BOTO3_INSTALL })}
        </p>
      )}
      {notSending && (
        <p className="text-[12px] text-warn m-0" role="status">
          {i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_not_sending', {
            switch: i18nT('pages.developer.featurePreviewsTab.decisions'),
          })}
        </p>
      )}
      {field('url', i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_url'), 'https://decider.example.com/v1/decisions')}
      {field('service', i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_service'), 'execute-api')}
      <p className="text-[12px] text-muted m-0">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_service_hint')}
      </p>
      {field('region', i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_region'), 'us-west-2')}
      {/* No placeholder: a greyed "default" reads as a value already set. */}
      {field('profile', i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_profile'), '')}
      <p className="text-[12px] text-muted m-0">
        {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_profile_hint')}
      </p>
      {field('model', i18nT('pages.developer.featurePreviewsTab.decisions_provider_aws_https_model'), '')}
      <div className="flex flex-wrap items-center gap-2">
        <Btn
          disabled={disabled || !available || !signedHttpsComplete(value) || testMut.isPending}
          onClick={() => testMut.mutate(value)}
        >
          {testMut.isPending
            ? i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_testing')
            : i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test')}
        </Btn>
        {result?.ok && (
          <span className="text-[12px] text-accent" role="status">
            {i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_ok', {
              latency: fmtUnit(result.latency_ms / 1000, 'second', { maximumFractionDigits: 1 }),
            })}
          </span>
        )}
      </div>
      {/* No hand-off: the typed fields are an unsaved draft, and asking the agent
          unmounts the card. */}
      {result && !result.ok && (
        <ErrorNotice
          variant="block"
          title={result.message ? testFailure(result.error) : undefined}
          message={result.message || testFailure(result.error)}
          messagePlacement="below"
          messageClassName="font-mono text-[11px] break-words"
        />
      )}
      {testMut.isError && (
        <ErrorNotice
          variant="inline"
          message={
            signedHttpsRefusal(testMut.error) ??
            i18nT('pages.developer.featurePreviewsTab.decisions_provider_sagemaker_test_request_failed')
          }
        />
      )}
    </div>
  )
}
