// electron-builder custom Windows sign hook: Authenticode-sign via AWS Signer.
//
// WHY A HOOK RATHER THAN A POST-BUILD STEP: the NSIS installer is a
// self-extracting archive. electron-builder signs the app executable, compresses
// it into the installer payload, and signs the installer and its generated
// uninstaller -- so signing after the build would mean unpacking and rebuilding
// that structure by hand. A hook signs each file at the moment electron-builder
// would have called signtool, so the ordering stays theirs.
//
// There is no certificate on this machine: the private key lives in the signing
// service and never leaves it. Signing is an S3 round-trip -- upload the file,
// a Signer-owned Lambda picks it up and calls StartSigningJob, we read the
// signed bytes back. See docs/windows-signing.md in KiroCrewPublishCDK.
//
// Credentials come from the OIDC role the workflow assumes; nothing is stored
// here. Like scripts/notarize.js, this SKIPS cleanly when the environment is
// not configured so credential-less local and fork builds still produce an
// (unsigned) installer instead of failing. build-windows.yml sets the five
// values below only when the signing secret is present, so an unconfigured CI
// run reaches the same skip path rather than calling the AWS CLI without
// credentials. A PARTIAL environment is a misconfiguration and throws --
// silently shipping unsigned from a build meant to sign is worse than failing.
//
// Env (all required to enable signing):
//   WINDOWS_SIGNING_UNSIGNED_BUCKET  where we upload; a Lambda watches it
//   WINDOWS_SIGNING_SIGNED_BUCKET    where Signer writes the signed artifact
//   WINDOWS_SIGNING_PROFILE_ID       profile identifier, e.g. KiroCrewWindowsExe
//   WINDOWS_SIGNING_ARTIFACT_ROLE    ArtifactAccessRole to assume
//   WINDOWS_SIGNING_EXTERNAL_ID      sts:ExternalId (equals the Signer app name)
// Optional:
//   WINDOWS_SIGNING_PLATFORM         default AuthenticodeSigner-SHA256-RSA
//   AWS_REGION                       default us-west-2 (the signing service
//                                    supports only a few regions)

const { execFileSync } = require('child_process')
const crypto = require('crypto')
const fs = require('fs')
const os = require('os')
const path = require('path')

const REGION = process.env.AWS_REGION || 'us-west-2'
const PLATFORM = process.env.WINDOWS_SIGNING_PLATFORM || 'AuthenticodeSigner-SHA256-RSA'

// The signing Lambda tags the source object with `signer-job-id` only AFTER it
// has started the job, and that tag lags the upload. Poll rather than assume it
// is already there.
const TAG_POLL_ATTEMPTS = 60
const TAG_POLL_INTERVAL_MS = 5000
// Separate budget for the signed object appearing, which happens after the job
// itself completes.
const OBJECT_POLL_ATTEMPTS = 60
const OBJECT_POLL_INTERVAL_MS = 5000

const log = (msg) => console.log(`[sign-windows] ${msg}`)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Run the AWS CLI under the ArtifactAccessRole profile.
 *
 * The role is reached with an `~/.aws/credentials` profile rather than an
 * inline `sts assume-role`: ArtifactAccessRole requires an ExternalId, and a
 * named profile with `credential_source = Environment` lets the CLI chain from
 * the workflow's OIDC credentials and pass that ExternalId on every call
 * without us handling temporary keys ourselves.
 *
 * `--output json` is pinned here rather than at the one call site that parses
 * the response: the CLI's output format is ambient config (AWS_DEFAULT_OUTPUT,
 * or `output = text` in a runner image's ~/.aws/config), so a caller that
 * assumes JSON is one environment away from feeding `text` to JSON.parse. It
 * costs nothing on the calls whose output we ignore.
 */
function aws(args, { capture = true } = {}) {
  return execFileSync(
    'aws',
    ['--profile', 'signer-artifact-access', '--region', REGION, '--output', 'json', ...args],
    {
      encoding: 'utf8',
      stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
      maxBuffer: 64 * 1024 * 1024,
    }
  )
}

/** Write the credential profile that carries the ExternalId. Idempotent. */
function ensureCredentialProfile(roleArn, externalId) {
  const awsDir = path.join(os.homedir(), '.aws')
  const credsPath = path.join(awsDir, 'credentials')
  const existing = fs.existsSync(credsPath) ? fs.readFileSync(credsPath, 'utf8') : ''
  if (existing.includes('[signer-artifact-access]')) {
    return
  }
  fs.mkdirSync(awsDir, { recursive: true })
  fs.appendFileSync(
    credsPath,
    [
      '',
      '[signer-artifact-access]',
      `role_arn = ${roleArn}`,
      'credential_source = Environment',
      `external_id = ${externalId}`,
      '',
    ].join('\n')
  )
  log('wrote the signer-artifact-access credential profile')
}

/** The `signer-job-id` tag the signing Lambda attaches once the job starts. */
async function waitForJobId(bucket, key) {
  for (let attempt = 1; attempt <= TAG_POLL_ATTEMPTS; attempt++) {
    let raw
    try {
      raw = aws(['s3api', 'get-object-tagging', '--bucket', bucket, '--key', key])
    } catch {
      // Tagging can 404 briefly right after the upload.
      await sleep(TAG_POLL_INTERVAL_MS)
      continue
    }
    const tag = (JSON.parse(raw).TagSet || []).find((t) => t.Key === 'signer-job-id')
    if (tag && tag.Value) {
      log(`signing job id: ${tag.Value}`)
      return tag.Value
    }
    if (attempt === 1) {
      log('waiting for the signing Lambda to start the job…')
    }
    await sleep(TAG_POLL_INTERVAL_MS)
  }
  throw new Error(
    `no signer-job-id tag on s3://${bucket}/${key} after ` +
      `${(TAG_POLL_ATTEMPTS * TAG_POLL_INTERVAL_MS) / 1000}s — the signing Lambda may not have ` +
      'fired (check the object landed with --acl bucket-owner-full-control) or the job failed to start'
  )
}

/** Signer writes the signed artifact as `<key>-<job-id>`. */
async function downloadSigned(bucket, key, destination) {
  for (let attempt = 1; attempt <= OBJECT_POLL_ATTEMPTS; attempt++) {
    try {
      aws(['s3api', 'get-object', '--bucket', bucket, '--key', key, destination])
      return
    } catch (err) {
      if (attempt === 1) {
        log('waiting for the signed artifact…')
      }
      if (attempt === OBJECT_POLL_ATTEMPTS) {
        throw new Error(
          `signed artifact never appeared at s3://${bucket}/${key} after ` +
            `${(OBJECT_POLL_ATTEMPTS * OBJECT_POLL_INTERVAL_MS) / 1000}s. The signing job likely ` +
            `FAILED. Last error: ${err.message}`
        )
      }
      await sleep(OBJECT_POLL_INTERVAL_MS)
    }
  }
}

const sha256 = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')

/**
 * electron-builder calls this once per file it wants signed, with
 * `configuration.path` as the file. Replacing that file in place is what makes
 * the signature part of the build.
 */
exports.default = async function signWindows(configuration) {
  const filePath = configuration.path
  const fileName = path.basename(filePath)

  // Treat the five as ONE unit, and distinguish "none set" from "some set".
  //
  // None set is the expected unconfigured state (fork, local build, any repo
  // without the signing secret): skip and let the build ship an unsigned
  // installer, exactly as it did before signing existed.
  //
  // A PARTIAL set is a misconfiguration, and skipping on it would be the worst
  // outcome available -- someone who wired up signing on purpose gets a
  // silently unsigned installer that looks like a success. Fail loudly instead.
  // build-windows.yml gates all five on one flag so CI can only ever produce
  // all-or-nothing; this catches a hand-rolled or half-migrated environment.
  const REQUIRED_ENV = [
    'WINDOWS_SIGNING_UNSIGNED_BUCKET',
    'WINDOWS_SIGNING_SIGNED_BUCKET',
    'WINDOWS_SIGNING_PROFILE_ID',
    'WINDOWS_SIGNING_ARTIFACT_ROLE',
    'WINDOWS_SIGNING_EXTERNAL_ID',
  ]
  const missing = REQUIRED_ENV.filter((name) => !process.env[name])

  if (missing.length === REQUIRED_ENV.length) {
    log(
      `skipped ${fileName} — Windows signing is not configured ` +
        `(set ${REQUIRED_ENV.join(' / ')} to enable).`
    )
    return
  }
  if (missing.length > 0) {
    throw new Error(
      `Windows signing is partially configured: ${missing.join(', ')} ` +
        `${missing.length === 1 ? 'is' : 'are'} missing while the rest are set. ` +
        'Refusing to skip, because that would ship an unsigned installer from a ' +
        'build that was meant to sign. Set all five or none.'
    )
  }

  const unsignedBucket = process.env.WINDOWS_SIGNING_UNSIGNED_BUCKET
  const signedBucket = process.env.WINDOWS_SIGNING_SIGNED_BUCKET
  const profileId = process.env.WINDOWS_SIGNING_PROFILE_ID
  const artifactRole = process.env.WINDOWS_SIGNING_ARTIFACT_ROLE
  const externalId = process.env.WINDOWS_SIGNING_EXTERNAL_ID

  // A colon is legal in a POSIX filename but not a Windows one, and Signer
  // rejects keys that are not valid Windows filenames. The nightly product name
  // ("KiroCrew Nightly") also carries a space, which is legal but awkward in a
  // key, so collapse to a safe set.
  const safeName = fileName.replace(/[^A-Za-z0-9._-]/g, '-')
  // Unique per invocation: electron-builder signs several files per build and
  // may sign the same basename more than once (the app exe is signed both in
  // win-unpacked and again as the installer payload's copy).
  const key = `${profileId}/${PLATFORM}/${Date.now()}-${crypto.randomBytes(4).toString('hex')}-${safeName}`

  ensureCredentialProfile(artifactRole, externalId)

  const before = sha256(filePath)
  log(`signing ${fileName} (sha256 ${before.slice(0, 12)}…)`)

  // bucket-owner-full-control is REQUIRED: without it the Signer-owned Lambda
  // cannot read the object and the job never starts.
  aws([
    's3api',
    'put-object',
    '--bucket',
    unsignedBucket,
    '--key',
    key,
    '--body',
    filePath,
    '--acl',
    'bucket-owner-full-control',
  ])

  const jobId = await waitForJobId(unsignedBucket, key)

  const tmp = path.join(os.tmpdir(), `signed-${crypto.randomBytes(6).toString('hex')}-${safeName}`)
  await downloadSigned(signedBucket, `${key}-${jobId}`, tmp)

  const after = sha256(tmp)
  if (after === before) {
    // Fail closed. Identical bytes mean we round-tripped the unsigned file and
    // would ship it believing it was signed.
    fs.rmSync(tmp, { force: true })
    throw new Error(
      `signed artifact for ${fileName} is byte-identical to the unsigned input — ` +
        'refusing to continue, as that means it was not actually signed'
    )
  }

  fs.copyFileSync(tmp, filePath)
  fs.rmSync(tmp, { force: true })
  log(`signed ${fileName} (job ${jobId}, sha256 ${after.slice(0, 12)}…)`)
}
