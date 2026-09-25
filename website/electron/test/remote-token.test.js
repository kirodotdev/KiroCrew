const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  DEFAULT_REMOTE_BIN,
  DEFAULT_REMOTE_PATH,
  REMOTE_BIN_CANDIDATES,
  buildCandidateTokenCommand,
  buildRemoteTokenCommand,
  buildRemoteTokenSshArgs,
  describeSshFailure,
  parseTokenFromStdout,
} = require("../remote-token");

describe("REMOTE_BIN_CANDIDATES", () => {
  it("lists the toolbox path first (most common install)", () => {
    assert.equal(REMOTE_BIN_CANDIDATES[0], "~/.toolbox/bin/kirocrew");
  });

  it("includes the legacy default path", () => {
    assert.ok(REMOTE_BIN_CANDIDATES.includes(DEFAULT_REMOTE_BIN));
  });

  it("uses only ~-prefixed or absolute paths (no PATH reliance)", () => {
    for (const c of REMOTE_BIN_CANDIDATES) {
      assert.ok(
        c.startsWith("~/") || c.startsWith("/"),
        `candidate ${c} must be absolute or ~-prefixed`,
      );
    }
  });
});

describe("buildCandidateTokenCommand", () => {
  it("produces a shell command that tries each candidate in order", () => {
    const cmd = buildCandidateTokenCommand(["~/a", "~/b"]);
    const aIdx = cmd.indexOf("~/a");
    const bIdx = cmd.indexOf("~/b");
    assert.ok(aIdx !== -1 && bIdx !== -1);
    assert.ok(aIdx < bIdx, "first candidate must appear before second");
  });

  it("sets PATH without referencing $PATH", () => {
    const cmd = buildCandidateTokenCommand(REMOTE_BIN_CANDIDATES);
    assert.match(cmd, /export PATH=/);
    assert.doesNotMatch(cmd, /:\$PATH/, "must not reference existing $PATH (spaces cause remote shell errors)");
  });

  it("includes KIROCREW_PORT when port option is provided", () => {
    const cmd = buildCandidateTokenCommand(REMOTE_BIN_CANDIDATES, { port: "7778" });
    assert.match(cmd, /KIROCREW_PORT=7778/);
  });

  it("uses custom remotePath when provided", () => {
    const cmd = buildCandidateTokenCommand(REMOTE_BIN_CANDIDATES, { remotePath: "~/custom/bin:/usr/bin" });
    assert.match(cmd, /export PATH=~\/custom\/bin:\/usr\/bin/);
  });

  it("tests -x directly on $b", () => {
    const cmd = buildCandidateTokenCommand(REMOTE_BIN_CANDIDATES);
    assert.match(cmd, /\[ -x "\$b" \]/);
    assert.doesNotMatch(cmd, /eval echo/);
  });

  it("exits with 127 and prints all candidates when none are executable", () => {
    const cmd = buildCandidateTokenCommand(["~/a", "~/b"]);
    assert.match(cmd, /exit 127/);
    assert.match(cmd, /~\/a, ~\/b/);
  });
});

describe("buildRemoteTokenCommand", () => {
  it("uses candidate list when binPath is the default sentinel", () => {
    const cmd = buildRemoteTokenCommand(DEFAULT_REMOTE_BIN);
    assert.match(cmd, /for b in /);
  });

  it("uses candidate list when binPath is empty", () => {
    const cmd = buildRemoteTokenCommand("");
    assert.match(cmd, /for b in /);
  });

  it("respects a user-customized binPath", () => {
    const cmd = buildRemoteTokenCommand("/opt/custom/kirocrew");
    assert.doesNotMatch(cmd, /for b in /);
    assert.match(cmd, /"\/opt\/custom\/kirocrew" token/);
  });

  it("includes KIROCREW_PORT for custom binPath", () => {
    const cmd = buildRemoteTokenCommand("/opt/custom/kirocrew", { port: "7778" });
    assert.match(cmd, /KIROCREW_PORT=7778/);
    assert.match(cmd, /"\/opt\/custom\/kirocrew" token/);
  });

  it("rewrites a leading ~/ to $HOME/ so it expands inside double quotes", () => {
    // Use a non-default custom path so this takes the user-binPath branch
    // (the default sentinel would take the candidate-sweep branch instead).
    const cmd = buildRemoteTokenCommand("~/apps/kirocrew");
    assert.match(cmd, /"\$HOME\/apps\/kirocrew" token/);
    assert.doesNotMatch(cmd, /"~\//);
  });

  it("leaves absolute and $HOME-prefixed paths untouched", () => {
    assert.match(buildRemoteTokenCommand("/opt/x/kirocrew"), /"\/opt\/x\/kirocrew" token/);
    assert.match(buildRemoteTokenCommand("$HOME/x/kirocrew"), /"\$HOME\/x\/kirocrew" token/);
  });

  it("passes port through to candidate command", () => {
    const cmd = buildRemoteTokenCommand(DEFAULT_REMOTE_BIN, { port: "8080" });
    assert.match(cmd, /KIROCREW_PORT=8080/);
  });

  it("accepts a custom candidate list via options", () => {
    const cmd = buildRemoteTokenCommand(DEFAULT_REMOTE_BIN, { candidates: ["~/x"] });
    assert.match(cmd, /~\/x/);
  });
});

describe("parseTokenFromStdout", () => {
  it("extracts token from standard URL", () => {
    const url = "http://localhost:5476?token=eyJhbGciOiJIUzI1NiJ9";
    assert.equal(parseTokenFromStdout(url), "eyJhbGciOiJIUzI1NiJ9");
  });

  it("extracts token when it's not the only query param", () => {
    const url = "http://host/?foo=bar&token=abc123";
    assert.equal(parseTokenFromStdout(url), "abc123");
  });

  it("handles trailing whitespace/newlines", () => {
    assert.equal(parseTokenFromStdout("http://x?token=xyz\n"), "xyz");
  });

  it("returns empty string when no token is present", () => {
    assert.equal(parseTokenFromStdout("random output"), "");
    assert.equal(parseTokenFromStdout(""), "");
  });

  it("stops at ampersand (doesn't eat following params)", () => {
    const url = "http://x?token=abc&session_exp=99999";
    assert.equal(parseTokenFromStdout(url), "abc");
  });
});

describe("buildRemoteTokenSshArgs", () => {
  it("closes ssh's stdin and fails fast instead of prompting", () => {
    assert.deepStrictEqual(
      buildRemoteTokenSshArgs("devbox", "kirocrew token"),
      ["-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "devbox", "kirocrew token"],
    );
  });

  it("places the host and remote command last, after every option", () => {
    const args = buildRemoteTokenSshArgs("user@host", "cmd");
    assert.deepStrictEqual(args.slice(-2), ["user@host", "cmd"]);
  });
});

describe("describeSshFailure", () => {
  const context = { sshBin: "ssh.exe", remoteHost: "devbox", timeoutMs: 20000 };
  const failure = (fields) => Object.assign(new Error("Command failed"), fields);

  it("names the missing ssh binary on a spawn ENOENT", () => {
    assert.equal(
      describeSshFailure(failure({ code: "ENOENT" }), "", context),
      "ssh client not found: ssh.exe",
    );
  });

  it("reports a timeout kill as a timeout, with any stderr", () => {
    assert.equal(
      describeSshFailure(failure({ killed: true, signal: "SIGTERM" }), "", context),
      "ssh devbox timed out after 20000 ms",
    );
    assert.equal(
      describeSshFailure(failure({ killed: true }), "slow proxy\n", context),
      "ssh devbox timed out after 20000 ms: slow proxy",
    );
  });

  it("returns ssh's stderr for an ordinary failure, else the error message", () => {
    assert.equal(
      describeSshFailure(failure({ code: 255 }), "Permission denied (publickey).\n", context),
      "Permission denied (publickey).",
    );
    assert.equal(describeSshFailure(failure({ code: 1 }), "", context), "Command failed");
  });
});
