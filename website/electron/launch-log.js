"use strict";

// The main process's launch log: every line goes to gateway-launch.log, and
// warnings and errors are mirrored to the console.
//
// The console mirror can die under the process. A desktop app launched from a
// terminal inherits that terminal's tty as stdout/stderr; close the terminal
// and macOS revokes the tty, so every later write fails with EIO. Node reports
// a failed stdio write asynchronously, as an 'error' event on the stream. With
// no listener for it, Node raises it as an uncaughtException, and the
// last-resort handler logs that through gerror -- which mirrors to the same
// dead stream, raising the next one. That loop filled the log at ~6,000 lines
// per second (#15950).
//
// So this module listens for 'error' on stdout and stderr. The first one
// writes one diagnostic to the log file and turns the console mirror off for
// the rest of the process; later ones are swallowed. The file log carries on.

/**
 * @param {object} options
 * @param {(entry: string) => void} options.append  writes one entry to the log file; may throw
 * @param {Pick<Console, "warn" | "error">} [options.console]
 * @param {Array<NodeJS.EventEmitter | null | undefined>} [options.streams]
 *        the console's streams; a missing one is skipped
 * @param {() => Date} [options.now]
 */
function createLaunchLog({
  append,
  console: sink = console,
  streams = [process.stdout, process.stderr],
  now = () => new Date(),
}) {
  let consoleAlive = true;

  function writeFile(line) {
    append("[" + now().toISOString() + "] " + line + "\n");
  }

  function onStreamError(error) {
    if (!consoleAlive) {
      return;
    }
    consoleAlive = false;
    try {
      writeFile(
        "console mirror disabled: stdio write failed ("
          + (error && error.message ? error.message : error) + ")",
      );
    } catch {
      // The file sink is gone too; there is nowhere left to report this.
    }
  }

  for (const stream of streams) {
    if (stream && typeof stream.on === "function") {
      stream.on("error", onStreamError);
    }
  }

  function mirror(method, line) {
    if (consoleAlive) {
      sink[method]("[gateway-launch] " + line);
    }
  }

  function glog(line) {
    try {
      writeFile(line);
    } catch (error) {
      // Preserve the diagnostic when the file sink itself is unavailable.
      mirror(
        "error",
        line + " (log write failed: " + (error && error.message ? error.message : error) + ")",
      );
    }
  }

  function gwarn(line) {
    glog(line);
    mirror("warn", line);
  }

  function gerror(line) {
    glog(line);
    mirror("error", line);
  }

  return {
    glog,
    gwarn,
    gerror,
    isConsoleAlive: () => consoleAlive,
  };
}

module.exports = { createLaunchLog };
