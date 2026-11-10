"use strict";

async function showLoadingThenConnect(deps,
    window,
    targetBackendUrl = deps.BACKEND_URL,
    { reconnect = false, initialPath = "" } = {},
  ) {
    const healthUrl = `${targetBackendUrl}/api/status`;
    const webContents = window.webContents;
    const painted = (0, deps.paintLoadingScreen)(window, { reconnect });
    // Cold boot and user-clicked retries raise. Autonomous liveness recovery
    // loads into the existing hidden/minimized window without touching focus.
    (0, deps.revealWindowForConnect)(window, { reconnect });
    // A message sent before the splash loads reaches the page it replaces.
    await painted;

    try {
      await (0, deps.waitForBackend)(window, healthUrl, {
        watchSpawn: targetBackendUrl === deps.BACKEND_URL,
      });
      if (window.isDestroyed()) return;

      // A newly started gateway regenerates .local_secret. /api/status can answer
      // just before local mint accepts that secret, so retry only an own-gateway
      // 403; foreign/SSH gateways can never be minted from this machine.
      for (let attempt = 0; ; attempt += 1) {
        let token = await (0, deps.mintLocalToken)(targetBackendUrl);
        if (!token) {
          ({ token } = await (0, deps.fetchRemoteToken)((0, deps.defaultedPort)(targetBackendUrl)));
        }
        if (window.isDestroyed()) return;

        if (token) {
          await (0, deps.fadeLoadingScreen)(webContents);
          if (window.isDestroyed()) return;
          await (0, deps.loadDashboardWithToken)(webContents, (0, deps.dashboardEntryUrl)(targetBackendUrl, initialPath), token);
          if (window.isDestroyed()) return;
          if (targetBackendUrl === deps.BACKEND_URL && window === (0, deps.mainWindow)()) {
            (0, deps.startLivenessMonitor)(window);
          }
          return;
        }

        // No token may be legitimate when auth is disabled. Probe the dashboard
        // origin itself before classifying a 403 and asking where to mint.
        const status = await new Promise((resolve) => {
          deps.http.get(targetBackendUrl, (response) => {
            response.resume();
            resolve(response.statusCode);
          }).on("error", () => resolve(0));
        });
        if (window.isDestroyed()) return;

        if (status !== 403) {
          webContents.loadURL((0, deps.dashboardEntryUrl)(targetBackendUrl, initialPath));
          if (targetBackendUrl === deps.BACKEND_URL && window === (0, deps.mainWindow)()) {
            (0, deps.startLivenessMonitor)(window);
          }
          return;
        }

        // URL.port is empty on a default-port URL. Normalize it before looking
        // up per-port remote settings or probing the local LISTEN owner.
        const promptPort = (0, deps.defaultedPort)(targetBackendUrl);
        const remoteHost = (0, deps.getRemoteHostConfig)(deps.store, promptPort)?.host || "";
        const localOwner = remoteHost
          ? "foreign"
          : await (0, deps.probeGatewayPortOwner)(promptPort);
        const kind = (0, deps.classifyAuthBlock)({ localOwner, remoteHost });

        if ((0, deps.shouldRetryLocalTokenMint)({ kind, attempt })) {
          (0, deps.glog)(`token mint: transient 403 on own gateway (kind=${kind}, attempt=${attempt + 1}/${deps.TOKEN_MINT_MAX_RETRIES + 1}) — retrying after backoff`);
          await new Promise((resolve) => {
            setTimeout(resolve, (0, deps.tokenMintRetryDelayMs)(attempt));
          });
          if (window.isDestroyed()) return;
          continue;
        }

        (0, deps.glog)(`token prompt: kind=${kind} owner=${localOwner} port=${promptPort} host=${remoteHost || "(none)"}`);
        if (window.isDestroyed()) return;
        // Token input is a needs-user state. Reveal before leaving fullscreen so
        // a pending fullscreen-exit tray hide cannot re-hide the prompt.
        (0, deps.revealForUserDecision)(window);
        (0, deps.leaveImmersiveModes)(window);
        // A packaged AppImage installs no `kirocrew` on PATH, so the page's
        // "run kirocrew token" instruction is unrunnable for an AppImage-only
        // user (#16258). The launcher ships inside the image, and
        // resolveGatewayBin() resolves its absolute path; pass it so the page
        // can name a command the user can actually run. Only for an AppImage
        // ($APPIMAGE set) with an absolute, bundled launcher path — a normal
        // install keeps the on-PATH wording, and a bare "kirocrew" fallback is
        // not forwarded. The bundled CLI mints only against OUR gateway's own
        // secret, so this changes the LOCAL recovery wording only and does not
        // touch the refusal of a silent mint against an adopted gateway.
        const bundledCliPath = (0, deps.appImageBundledCliPath)(deps.processObj.env, deps.resolveGatewayBin);
        webContents.loadFile(deps.path.join(deps.dirname, "token-prompt.html"), {
          query: {
            port: promptPort,
            kind,
            host: remoteHost,
            ...(bundledCliPath ? { cli: bundledCliPath } : {}),
          },
        });
        return;
      }
    } catch (error) {
      if (window.isDestroyed()) return;
      const failedToStart = error && error.kind === "failed";
      const launchLogPath = (0, deps.gatewayLogPath)();
      // Bounded tail read, as in failure-dialog.js: only the file's last 256 KiB
      // are read, however large the launch log has grown.
      const logTail = (0, deps.readLogTail)(launchLogPath, { fs: deps.fs, lines: 60 });

      // A pre-spawn integrity refusal is already authoritative. Otherwise only
      // a bundled current-attempt stdlib crash may be relabelled as installation;
      // current-attempt EADDRINUSE wins because its remedy is force-stop.
      // Only a pre-spawn refusal may auto-retry. Its probe sees exactly what
      // refused, so "complete" there means the launcher will spawn. A crash the
      // matcher reclassifies as installing died on a module the probe never
      // inspects (a sibling file, a name outside REQUIRED_STDLIB_PARTS); were
      // that dialog to auto-retry, a permanently truncated bundle -- disk full,
      // an interrupted installer, a quarantined file -- would respawn and crash
      // on every tick with no exit. That dialog keeps the manual Retry.
      const refusedBeforeSpawn = !!(error.failure && error.failure.incompleteBundle);
      const failureRecord = (0, deps.shouldReclassifyAsInstalling)({
        failedToStart,
        failure: error.failure,
        logTail,
        portInUseInLog: (0, deps.isPortInUse)((0, deps.currentAttemptLog)(logTail)),
        bundled: !!(error.failure && error.failure.bundled),
      })
        ? { ...error.failure, incompleteBundle: true }
        : error.failure;
      const failureKind = (0, deps.classifyStartFailure)({
        failedToStart,
        failure: failureRecord,
        isOwnPort: targetBackendUrl === deps.BACKEND_URL,
        portInUseInLog: (0, deps.isPortInUse)(logTail),
      });
      const localGatewayOff = failureKind === "client-only";
      const remoteTarget = localGatewayOff ? (failureRecord?.remoteHost || "") : "";
      // Which local-start outcome this dialog is reporting, if any. Read in the
      // same precedence the message uses, so the title and the body cannot name
      // different states: the unreadable probe is newest, then a lost restart,
      // then a port that was already served, then a port reserved for a crew.
      const localStartOutcome = !localGatewayOff
        ? ""
        : (failureRecord?.localStartUnverifiable
          ? "could not identify what is on the port"
          : (failureRecord?.localStartFailed
            ? "the local gateway did not finish starting"
            : (failureRecord?.localStartPortBusy
              ? `port ${failureRecord.localStartPortBusy} is already in use`
              : (failureRecord?.localStartBlocked === "crew-configured"
                ? `port ${deps.PORT} is reserved for ${failureRecord?.remoteHost || "a crew"}`
                : (failureRecord?.localStartBlocked === "foreign-holder"
                  ? `port ${deps.PORT} is in use by another program`
                  : "")))));
      // The crew's own port when it has one; PORT is only this end of the link.
      const remoteTargetPort = (localGatewayOff && failureRecord?.remotePort) || deps.PORT;
      const portConflict = failureKind === "port-conflict";

      let title;
      let message;
      if (failureKind === "installing") {
        title = failureRecord?.backendPruned ? deps.PRUNED_DIALOG_TITLE : deps.INSTALLING_DIALOG_TITLE;
        message = refusedBeforeSpawn
          ? error.message
          : (0, deps.describeIncompleteBundle)([]);
      } else if (localGatewayOff) {
        title = remoteTarget
          ? `Kiro Crew — nothing answering at ${remoteTarget}:${remoteTargetPort}`
          : `Kiro Crew — no gateway on port ${deps.PORT}`;
        // After a click, the news is what happened to the local start, and the
        // title is what a reader takes in first. Leaving it on the crew made the
        // busy-port and lost-restart dialogs indistinguishable at a glance --
        // same title, same buttons, one word different in the body.
        if (localStartOutcome) title = `Kiro Crew — ${localStartOutcome}`;
        message = error.message;
      } else if (portConflict) {
        title = `Kiro Crew — port ${deps.PORT} already in use`;
        message = `Another Kiro Crew gateway is already using port ${deps.PORT} (it may be wedged). `
          + `Force-stop it and retry, or quit. From a terminal you can also run: `
          + `kirocrew stop --port ${deps.PORT}`;
      } else if (failedToStart) {
        title = "Kiro Crew — gateway failed to start";
        message = error.message;
      } else if ((0, deps.isLiveSpawnedTimeout)({
        error,
        ownTarget: targetBackendUrl === deps.BACKEND_URL,
        ownership: deps.gatewayOwnership,
        child: deps.gatewayProcess,
      })) {
        // Our own child, still running: naming `kirocrew gateway` would send
        // the user to start a process that is already up. Retry keeps that
        // child and waits again (it respawns only when none is running).
        title = "Kiro Crew — gateway is not answering";
        message = (0, deps.describeSpawnedGatewayTimeout)(error.waitedMs);
      } else {
        title = "Kiro Crew — can't reach the gateway";
        message = "Could not connect to the Kiro Crew backend. Make sure "
          + "'kirocrew gateway' is running, or check kirocrew doctor.";
      }

      (0, deps.revealForUserDecision)(window);
      // Reveal Log reopens the dialog after showing the file. Every other action
      // either retries the complete boot state machine or terminates this window.
      for (;;) {
        const action = await (0, deps.showGatewayErrorDialog)(window, {
          title,
          message,
          logTail,
          logPath: launchLogPath,
          portConflict,
          port: deps.PORT,
          localGatewayOff,
          // This launch probed the port's holder and logged its pid and command,
          // so the log is this launch's own evidence and is shown.
          ...(failureRecord?.localStartBlocked === "foreign-holder" ? { showLog: true } : {}),
          // Withheld for the foreign-holder state: its message names freeing the
          // port or recording the crew, never this button, and pressing it only
          // flips a setting that is already on and re-enters the same refusal --
          // the identical dialog, one click later. A control that returns the user
          // to where they started is worse than an absent one.
          offerLocalStart: (0, deps.canOfferLocalStart)(localGatewayOff, remoteTarget)
            && deps.gatewayStartFailure?.localStartBlocked !== "foreign-holder",
          // The one state whose paragraph asks for Quit: the ownership probe could
          // not run, so both action buttons reach the same check and quitting and
          // reopening is the route that recovers.
          ...(deps.gatewayStartFailure?.localStartUnverifiable ? { accentQuit: true } : {}),
          // The pruned-backend paragraph asks for Quit first, so the accent
          // follows it.
          ...(failureRecord?.backendPruned ? { accentQuit: true, backendPruned: true } : {}),
          // Retry reaches the crew in every one of these states, so it says which
          // crew in every one of them. Labelling it only where the accent moves
          // made the same control read as two: "Retry" in one opening of a dialog
          // and "Retry <host>" in the next, with nothing in the window to explain
          // the difference.
          //
          // The foreign-holder state has no crew to name, and a bare "Retry" gave
          // a reader nothing either -- retry what, when the message says the port
          // belongs to another program? It names the port instead, which is the
          // thing that has to change for the click to work.
          ...(remoteTarget
            ? { retryLabel: `Retry ${remoteTarget}` }
            : (deps.gatewayStartFailure?.localStartBlocked === "foreign-holder"
              ? { retryLabel: `Retry port ${deps.PORT}` }
              : {})),
          // Both local-start states name Start Local Gateway as the action to
          // take, and Retry reaches only the crew that is already unreachable, so
          // in both the accent follows the sentence. The condition is the two
          // states together rather than either one alone: an accent resting on
          // Retry while the paragraph asks for the other button spends the
          // reader's first click on the control that cannot help.
          //
          // The dialog demotes Retry to a secondary rather than dropping it,
          // which is also what keeps this from offering Start Local Gateway
          // twice.
          ...(localGatewayOff
            && (deps.gatewayStartFailure?.localStartPortBusy
              || deps.gatewayStartFailure?.localStartFailed)
            && (0, deps.canOfferLocalStart)(localGatewayOff, remoteTarget)
            ? {
              primaryAction: "enable-retry",
              primaryLabel: "Start Local Gateway",
            }
            : {}),
          crewAction: (0, deps.remoteCrewAction)({
            localGatewayOff,
            remoteHost: remoteTarget,
          }),
          // The one failure state known to clear on its own, and only where the
          // probe can see what is missing. Every other dialog stays manual.
          autoRetryProbe: failureKind === "installing" && refusedBeforeSpawn
            ? deps.probeLaunchBlockingParts
            : undefined,
        });
        if (window.isDestroyed()) return;
        if (action === "reveal") {
          try { deps.shell.showItemInFolder(launchLogPath); }
          catch { /* best effort */ }
          continue;
        }
        // An update install dispatched while this dialog was open owns the
        // gateway: no action may save a crew, relaunch the app, force-stop the
        // port or start a backend under it. The dialog stays, so a Retry after
        // a failed install still works.
        if (
          deps.installingUpdate && targetBackendUrl === deps.BACKEND_URL
          && deps.RETRY_ACTIONS.has(action)
        ) {
          (0, deps.glog)(`${action}: an update install is in flight; leaving the dialog to the user`);
          continue;
        }
        if (action === "configure-remote") {
          let draft = (0, deps.remoteCrewDraft)((0, deps.getRemoteHostConfig)(deps.store, deps.PORT) || {}, { tunnelByDefault: !deps.IS_WIN });
          let configured = false;
          for (;;) {
            const fields = await (0, deps.promptRemoteCrew)(window, deps.PORT, draft);
            if (window.isDestroyed()) return;
            // Dismissed: nothing about the launch changed, so the failure
            // dialog is where this returns to.
            if (!fields) break;
            const { saved, error: saveError } = (0, deps.saveRemoteCrewConfig)(deps.store, deps.PORT, fields);
            if (saved) {
              configured = true;
              (0, deps.glog)(`remote crew for :${deps.PORT} configured from the error dialog`);
              // The saved choice stands for the rest of this launch, so the
              // Retry below never reopens a forward the form left unticked.
              deps.tunnelKeeper.applySavedChoice();
              break;
            }
            // Reopen on what the user typed. A refused save writes nothing, so
            // the store holds no copy, and one bad field must not cost the
            // other three.
            draft = fields;
            await deps.dialog.showMessageBox(
              window.isDestroyed() ? null : window,
              { type: "error", title: "Invalid Input", message: saveError },
            );
            if (window.isDestroyed()) return;
          }
          if (!configured) continue;
        }
        if (action === "enable-retry") {
          (0, deps.setLocalGatewayEnabled)(deps.store, true);
          (0, deps.glog)("local gateway turned back on from the error dialog");
          if (remoteTarget) {
            // PORT is fixed for this process and is the local end of the link to
            // the crew, so a gateway started here would bind the crew's port.
            // Port selection reads the setting once per process, so only a fresh
            // process can pick a local port. The successor therefore lands on a
            // port this one never served, and this process chooses that port and
            // pins it into the successor's environment, so the port it watches is
            // the port the successor binds. This process stays client-only: the
            // setting is persisted either way, so a manual launch also recovers.
            const successorPort = (0, deps.predictLocalPort)();
            (0, deps.glog)(`re-execing so port selection runs again; expecting the successor on :${successorPort}`);
            void (0, deps.relaunchViaConfirmedSuccessor)(({ reason, port: busyPort } = {}) => {
              if (reason === "successor-unverifiable") {
                // The restart ran and its gateway answered; what failed was this
                // app's check of who holds the port, and that outcome is stable
                // on this host -- so the same click repeats it exactly. Saying
                // "the restarted app never served one" would deny what the user
                // watched, and offering the button again would send them round the
                // same loop. The state carries the port and its own flag, and the
                // message names the route that does recover: quit and reopen,
                // which works because the setting is already persisted.
                deps.localStartUnverifiable = busyPort || successorPort;
                deps.localStartRelaunchFailed = false;
                deps.localStartPortBusy = 0;
                if (deps.gatewayStartFailure) {
                  deps.gatewayStartFailure.localStartUnverifiable = deps.localStartUnverifiable;
                  deps.gatewayStartFailure.localStartFailed = false;
                  deps.gatewayStartFailure.localStartPortBusy = 0;
                }
                (0, deps.glog)(`could not establish who holds :${deps.localStartUnverifiable}; the setting stays on so the next launch starts a gateway`);
                (0, deps.showLoadingThenConnect)(window, targetBackendUrl, { initialPath })
                  .catch((error) => (0, deps.glog)(`resurfacing the gateway failure failed: ${error && error.message}`));
                return;
              }
              if (reason === "port-busy") {
                // Nothing restarted, so claiming one did would deny what the
                // user saw. The port is held by something outside this app and
                // can be freed, so the button stays available rather than being
                // refused over a condition that is not this app's to fix. The
                // setting is already persisted, which is why a later launch
                // still picks a local port on its own.
                deps.localStartPortBusy = busyPort;
                if (deps.gatewayStartFailure) deps.gatewayStartFailure.localStartPortBusy = busyPort;
                // And retire an earlier failed attempt, the mirror of what that
                // path does to an earlier busy port. The message reads the failed
                // flag first, so a leftover would report "the restarted app never
                // served one" for a click that spawned nothing and would never
                // name the port to free -- and the likeliest occupant of that port
                // is an orphan from the very attempt that set the flag.
                deps.localStartRelaunchFailed = false;
                if (deps.gatewayStartFailure) deps.gatewayStartFailure.localStartFailed = false;
                // And the unreadable-probe flag, for the same reason and more
                // sharply: BOTH readers test it first, so a leftover would keep
                // reporting a successor this click never spawned, would never name
                // the port to free, and would keep the accent on Quit while the
                // paragraph asks for a port to be freed. Every one of these three
                // branches retires the other two.
                deps.localStartUnverifiable = 0;
                if (deps.gatewayStartFailure) deps.gatewayStartFailure.localStartUnverifiable = 0;
                (0, deps.glog)(`:${busyPort} is already served by something else; nothing was restarted and the setting stays on`);
                (0, deps.showLoadingThenConnect)(window, targetBackendUrl, { initialPath })
                  .catch((error) => (0, deps.glog)(`resurfacing the gateway failure failed: ${error && error.message}`));
                return;
              }
              deps.localStartRelaunchFailed = true;
              // An earlier busy port is no longer the story: this attempt gets to
              // the restart and loses it, which is the newer outcome, and the
              // message reads that field first. Cleared on the state, not only on
              // the record, so a later rebuild does not bring it back.
              deps.localStartPortBusy = 0;
              // Same for the unreadable-probe flag: this attempt reached the
              // restart and lost it, which is the newer outcome, and that field is
              // read ahead of this one.
              deps.localStartUnverifiable = 0;
              // The reopened wait reuses this same record rather than building a
              // new one, so the flag is set here too or the message cannot tell a
              // failed attempt from an app that never offered the restart.
              //
              // canStartHere is deliberately NOT forced false: the button is
              // offered again after a failure, and the record has to agree with
              // the gate or the message would deny a control the window renders.
              if (deps.gatewayStartFailure) {
                deps.gatewayStartFailure.localStartFailed = true;
                deps.gatewayStartFailure.localStartPortBusy = 0;
                deps.gatewayStartFailure.localStartUnverifiable = 0;
              }
              (0, deps.glog)("successor never served; this process stays client-only and the setting is on for the next launch");
              (0, deps.showLoadingThenConnect)(window, targetBackendUrl, { initialPath })
                .catch((error) => (0, deps.glog)(`resurfacing the gateway failure failed: ${error && error.message}`));
            }, { expectPort: successorPort, pinPort: true, restartingStatus: deps.RESTARTING_FOR_LOCAL_GATEWAY_STATUS });
            return;
          }
          deps.runLocalGateway = true;
        }
        if (action === "force-retry") {
          let freed = true;
          let probeFailed = false;
          let ownerUnknown = false;
          const epoch = deps.installEpoch;
          try {
            ({ freed, probeFailed = false, ownerUnknown = false } = await (0, deps.forceStopGatewayPort)(deps.PORT, {
              onBindWait: () => (0, deps.paintLoadingScreen)(window),
              isAborted: () => epoch !== deps.installEpoch,
            }));
          } catch (probeError) {
            (0, deps.glog)(`force-stop: failed unexpectedly (${probeError && probeError.message}); retrying the start`);
          }
          if (window.isDestroyed() || epoch !== deps.installEpoch) return;
          if (!freed) {
            // An owner netstat does not list is as unverifiable as a failed probe.
            return (0, deps.showUnrecoverableGatewayError)(window, deps.PORT, { probeFailed: probeFailed || ownerUnknown });
          }
        }
        if (deps.RETRY_ACTIONS.has(action)) {
          deps.gatewayStartFailure = null;
          // A primary own-port retry respawns only when no child remains. Timeouts
          // may leave a live child; connection tabs never own one at all. The
          // owner joins a recovery that is already starting one. An install
          // that overtakes the launch hands the dialog back, as above: a failed
          // install's recovery reconnects only the main window.
          if (
            targetBackendUrl === deps.BACKEND_URL && !deps.gatewayProcess
            && (await (0, deps.respawnGateway)()) === deps.RESPAWN_SUPERSEDED
          ) {
            if (window.isDestroyed()) return;
            (0, deps.glog)(`${action}: an update install took over this start; leaving the dialog to the user`);
            continue;
          }
          if (window.isDestroyed()) return;
          return (0, deps.showLoadingThenConnect)(window, targetBackendUrl, { initialPath });
        }

        if (window === (0, deps.mainWindow)()) (0, deps.quitApp)();
        else window.destroy();
        return;
      }
    }
  }

module.exports = { showLoadingThenConnect };
