/**
 * Updater -> launch-log bridge, kept pure for testability.
 *
 * electron-updater (and auto-update.js's own lifecycle lines) log through a
 * {info, warn, error, debug} object. When main.js passes no `log` dep,
 * auto-update.js defaults to `console` AND assigns it as autoUpdater.logger --
 * and in a packaged app console output goes nowhere durable, so update
 * failures leave no trace on disk. main.js already owns a durable
 * single-line sink (glog -> gateway-launch.log); this module adapts the
 * updater's logger interface onto such a sink.
 *
 * Contract:
 * - Each call becomes exactly ONE line, tagged with its level
 *   ("[update:error] ..."), so the launch log stays greppable line-by-line.
 * - An Error serializes as its message plus the FIRST stack frame only:
 *   electron-updater's HttpError dumps are multi-line monsters that would
 *   otherwise flood the log while the first frame carries the signal.
 * - Any single argument is truncated to MAX_ARG_CHARS for the same reason.
 * - Never throws: logging must never be able to break the updater (the
 *   mirror of glog's own "never let logging break launch" rule).
 * - `debug` is included because electron-updater probes its logger for it.
 */

const MAX_ARG_CHARS = 500;

/** Render one logger argument as single-line text, bounded in length. */
function formatArg(arg) {
  let text;
  if (arg instanceof Error) {
    const stack = typeof arg.stack === "string" ? arg.stack.split("\n") : [];
    // stack[0] repeats "Name: message"; the first real frame is stack[1].
    const frame = stack.length > 1 ? ` ${stack[1].trim()}` : "";
    text = `${arg.name || "Error"}: ${arg.message}${frame}`;
  } else if (typeof arg === "string") {
    text = arg;
  } else {
    try { text = JSON.stringify(arg); } catch { text = String(arg); }
    if (text === undefined) text = String(arg);
  }
  // One call, one line -- collapse any embedded newlines.
  text = String(text).replace(/\s*\r?\n\s*/g, " ");
  if (text.length > MAX_ARG_CHARS) {
    text = `${text.slice(0, MAX_ARG_CHARS)}…[truncated]`;
  }
  return text;
}

/**
 * Build an electron-updater-compatible logger that writes tagged single
 * lines through `sink` (a function taking one string, e.g. main.js's glog).
 *
 * @param {(line: string) => void} sink
 * @returns {{info: Function, warn: Function, error: Function, debug: Function}}
 */
function makeUpdaterLogger(sink) {
  const emit = (level) => (...args) => {
    try {
      sink(`[update:${level}] ${args.map(formatArg).join(" ")}`);
    } catch { /* logging must never break the updater */ }
  };
  return {
    info: emit("info"),
    warn: emit("warn"),
    error: emit("error"),
    debug: emit("debug"),
  };
}

module.exports = { makeUpdaterLogger, MAX_ARG_CHARS };
