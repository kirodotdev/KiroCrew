"use strict";

const { createDisplayMediaHandler } = require("../../display-media");
const {
  createPermissionRequestHandler,
  createPermissionCheckHandler,
} = require("../../permission-handler");
const { isUntrustedContents } = require("../../browser-view");
const { createCaptureTrust } = require("../../capture-trust");

/**
 * The permission policy of every session a dashboard window uses: which
 * WebContents may capture a screen, which may use the microphone, and the
 * deny-all partition that hosts embedded browser pages. Also owns the macOS
 * Privacy-pane recovery dialogs, because a denied grant is only recoverable
 * there once macOS has spent its one-shot prompt.
 *
 * Registration is idempotent per process: every handler is installed once.
 */
function createSessionSecurity({
  session,
  webContents,
  desktopCapturer,
  systemPreferences,
  dialog,
  shell,
  isMac: IS_MAC,
  partition: BROWSER_PARTITION,
}) {
  let micDialogOpen = false;
  let sessionSecurityConfigured = false;

  function hardenBrowserPartition(sessionApi) {
    const browserSession = sessionApi.fromPartition(BROWSER_PARTITION);
    browserSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    browserSession.setPermissionCheckHandler(() => false);
    return browserSession;
  }

  function showScreenPermissionDialog() {
    const pane =
      "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
    dialog.showMessageBox({
      type: "info",
      title: "Screen Recording permission needed",
      message: "Allow Kiro Crew to capture the screen",
      detail:
        "The screen-snip tool needs macOS Screen Recording permission. "
        + "Open System Settings › Privacy & Security › Screen Recording, "
        + "enable Kiro Crew, then try the snip again.",
      buttons: ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0) shell.openExternal(pane);
    }).catch(() => {});
  }

  function showMicPermissionDialog(status = "denied") {
    // Dictation, streaming STT, settings test and meetings can race the same
    // denial. Latch one recovery dialog rather than stacking modal copies.
    if (micDialogOpen) return;
    micDialogOpen = true;
    const pane =
      "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
    const restricted = status === "restricted";
    dialog.showMessageBox({
      type: "info",
      title: "Microphone permission needed",
      message: restricted
        ? "Microphone access is blocked by a policy"
        : "Allow Kiro Crew to use the microphone",
      detail: restricted
        ? "Voice input needs macOS Microphone permission, but access is "
          + "restricted by a device-management policy on this Mac. Contact "
          + "whoever manages it to allow microphone access for Kiro Crew."
        : "Voice input needs macOS Microphone permission, and macOS will not "
          + "ask again once it has been denied. Open System Settings › Privacy "
          + "& Security › Microphone, enable Kiro Crew, then try the mic again.",
      buttons: restricted ? ["OK"] : ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: restricted ? 0 : 1,
    }).then(({ response }) => {
      if (!restricted && response === 0) shell.openExternal(pane);
    }).catch(() => {}).then(() => {
      micDialogOpen = false;
    });
  }

  function configureSessionSecurity() {
    if (sessionSecurityConfigured) return;

    // Screen capture has its own handler. Prefer the native system picker when
    // available and fall back to desktopCapturer elsewhere. WHO may be granted a
    // screen is decided by identity in capture-trust.js: a registered surface,
    // its own main frame, still on its registered origin. Without this dep the
    // handler denies everything, so the wiring is not optional.
    session.defaultSession.setDisplayMediaRequestHandler(
      createDisplayMediaHandler({
        isTrustedRequest: createCaptureTrust({
          fromFrame: (frame) => webContents.fromFrame(frame),
        }),
        getSources: () => desktopCapturer.getSources({
          types: ["screen", "window"],
        }),
        getScreenAccessStatus: () => (
          IS_MAC
            ? systemPreferences.getMediaAccessStatus("screen")
            : "granted"
        ),
        onPermissionNeeded: (reason) => {
          if (reason === "denied") return showScreenPermissionDialog();
          // No dialog for a trust refusal: an embedded pane or a browsed page
          // asked, and nothing the user can change in System Settings would
          // make that grantable. One breadcrumb instead, for the same reason
          // permission-handler.js logs its denials -- a silent refusal is
          // indistinguishable from an OS one when someone has to diagnose it.
          if (reason === "untrusted-frame") {
            // eslint-disable-next-line no-console -- see the note above
            console.warn(
              "[display-media] DENY capture: requester is not a registered capture surface",
            );
          }
        },
      }),
      { useSystemPicker: true },
    );

    // The dashboard receives only the media grant it needs. Untrusted browser
    // WebContents fail closed by identity before any localhost-origin heuristic
    // can grant them access.
    session.defaultSession.setPermissionRequestHandler(
      createPermissionRequestHandler({
        isUntrusted: isUntrustedContents,
        ...(IS_MAC
          ? {
              getMicAccessStatus: () =>
                systemPreferences.getMediaAccessStatus("microphone"),
              // Ask only on the user's mic gesture. Asking at launch spends
              // macOS TCC's one-shot prompt before the action has context.
              askForMicAccess: () =>
                systemPreferences.askForMediaAccess("microphone"),
              onMicBlocked: () => showMicPermissionDialog(),
            }
          : {}),
      }),
    );
    session.defaultSession.setPermissionCheckHandler(
      createPermissionCheckHandler({ isUntrusted: isUntrustedContents }),
    );

    // Permission handlers are per-session. Embedded pages use a separate
    // persistent partition, so the default-session policy above cannot cover
    // them; deny every permission explicitly before any such view can exist.
    hardenBrowserPartition(session);
    sessionSecurityConfigured = true;
  }

  function handleMicDenied() {
    if (!IS_MAC) return;
    try {
      const status = systemPreferences.getMediaAccessStatus("microphone");
      if (status === "denied" || status === "restricted") {
        showMicPermissionDialog(status);
      }
    } catch {
      // Stay silent when the OS status probe itself is unavailable.
    }
  }

  return { configureSessionSecurity, handleMicDenied };
}

module.exports = { createSessionSecurity };
