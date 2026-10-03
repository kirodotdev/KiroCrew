/**
 * Shared aws-control capture fixtures. Both `capture-aws-control-errors.mjs` and
 * `capture-aws-control-nightly-blocked.mjs` photograph the real SPA over the same
 * stubbed account/drive/consent shapes; keeping that boilerplate in one place is
 * what stops the two harnesses from being flagged as a copy by the duplicate gate.
 */
export const ACC = '111122223333'
export const B = '/api/apps/aws-control'
export const GiB = 1024 ** 3

const nullSummary = { storage: null, sites: null, tasks: null, costMonthToDate: null }

export const ACCOUNTS = {
  accounts: [{
    account: ACC, name: 'prod-main', health: 'ok', summary: nullSummary,
    profiles: [
      { name: 'prod-main', region: 'us-west-2', kind: 'sso', identityOk: true, account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`, detail: '', default: true },
    ],
  }],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
  generatedAt: '2026-09-03T22:00:00Z',
}

export const DRIVE = {
  exists: true, bucket: `kirocrew-drive-${ACC}-usw2`, region: 'us-west-2',
  usage: {
    bytes: 3.2 * GiB, objects: 128,
    sections: {
      drive: { objects: 97, bytes: 1.9 * GiB },
      library: { objects: 23, bytes: 0.4 * GiB },
      backup: { objects: 8, bytes: 0.9 * GiB },
    },
  },
}

export const CONSENT = (svc) => ({
  service: svc, serviceLabel: svc === 's3' ? 'Amazon S3' : 'AWS Cost Explorer',
  profile: 'prod-main', credentialSource: 'profile prod-main', region: 'us-west-2',
  account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`,
  identityResolved: true, identityDetail: '', granted: true, reason: '',
  revokedOnAccountChange: false,
  grant: { account: ACC, region: 'us-west-2', profile: 'prod-main', granted_at: '2026-08-20T09:00:00Z' },
})
