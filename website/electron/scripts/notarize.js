// electron-builder afterSign hook: notarize the macOS .app with Apple.
//
// Runs only on macOS and only when notarization credentials are present in the
// environment, so credential-less local/dev builds still succeed (producing an
// unsigned/ad-hoc DMG) instead of erroring. No secrets live in the repo — the
// builder supplies them at build time via env vars:
//
//   Option A (App Store Connect API key — recommended for CI):
//     APPLE_API_KEY=/abs/path/AuthKey_XXXX.p8
//     APPLE_API_KEY_ID=XXXXXXXXXX
//     APPLE_API_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
//
//   Option B (Apple ID + app-specific password):
//     APPLE_ID=you@example.com
//     APPLE_APP_SPECIFIC_PASSWORD=abcd-efgh-ijkl-mnop
//     APPLE_TEAM_ID=XXXXXXXXXX
//
// The signing identity itself is supplied to electron-builder separately via
// CSC_LINK (path/base64 of the Developer ID Application .p12) + CSC_KEY_PASSWORD.

const path = require('path')

exports.default = async function notarize(context) {
  const { electronPlatformName, appOutDir } = context
  if (electronPlatformName !== 'darwin') {
    return
  }

  const appName = context.packager.appInfo.productFilename
  const appPath = path.join(appOutDir, `${appName}.app`)
  const appBundleId = context.packager.appInfo.id

  const hasApiKey =
    process.env.APPLE_API_KEY && process.env.APPLE_API_KEY_ID && process.env.APPLE_API_ISSUER
  const hasAppleId =
    process.env.APPLE_ID && process.env.APPLE_APP_SPECIFIC_PASSWORD && process.env.APPLE_TEAM_ID

  if (!hasApiKey && !hasAppleId) {
    console.log(
      '[notarize] skipped — no Apple credentials in env ' +
        '(set APPLE_API_KEY/APPLE_API_KEY_ID/APPLE_API_ISSUER or ' +
        'APPLE_ID/APPLE_APP_SPECIFIC_PASSWORD/APPLE_TEAM_ID to enable).'
    )
    return
  }

  // Lazy require so the dependency is only needed on signing builds.
  const { notarize } = require('@electron/notarize')

  const opts = hasApiKey
    ? {
        appBundleId,
        appPath,
        appleApiKey: process.env.APPLE_API_KEY,
        appleApiKeyId: process.env.APPLE_API_KEY_ID,
        appleApiIssuer: process.env.APPLE_API_ISSUER,
      }
    : {
        appBundleId,
        appPath,
        appleId: process.env.APPLE_ID,
        appleIdPassword: process.env.APPLE_APP_SPECIFIC_PASSWORD,
        teamId: process.env.APPLE_TEAM_ID,
      }

  console.log(`[notarize] submitting ${appName}.app to Apple notary service…`)
  await notarize(opts)
  console.log(`[notarize] done — ${appName}.app is notarized and stapled.`)
}
