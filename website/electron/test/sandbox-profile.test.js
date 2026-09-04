const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { describeSandboxProfileNeed } = require("../sandbox-profile");

// A sysctl reader that answers with `value` for the AppArmor knob and throws
// for anything else, so a test that reads the wrong path fails loudly.
const sysctl = (value) => (p) => {
  assert.equal(p, "/proc/sys/kernel/apparmor_restrict_unprivileged_userns");
  if (value === null) throw new Error("ENOENT");
  return value;
};

describe("describeSandboxProfileNeed", () => {
  const CLI = "/tmp/.mount_abc123/resources/backend-dist/kirocrew-backend/bin/kirocrew";
  const restricted = {
    platform: "linux",
    env: { APPIMAGE: "/home/u/Applications/kirocrew.AppImage" },
    readSysctl: sysctl("1\n"),
    cliBin: CLI,
  };

  it("advises on a restricted Linux host launched from an AppImage", () => {
    const need = describeSandboxProfileNeed(restricted);

    assert.ok(need);
    assert.equal(need.appImagePath, "/home/u/Applications/kirocrew.AppImage");
    assert.match(need.command, /sandbox install-profile --path /);
    assert.match(need.command, /kirocrew\.AppImage/);
    assert.match(need.reason, /fail closed/);
  });

  // This persona is documented as needing "no Python, pip, npm, or Node", so
  // there is no `kirocrew` on their PATH -- the CLI lives inside the bundle.
  // Emitting the bare command would hand exactly the affected user a
  // `command not found` and leave them with only the sandbox opt-out.
  it("names the bundled CLI by absolute path, not as a bare command", () => {
    const need = describeSandboxProfileNeed(restricted);

    assert.equal(need.command.startsWith(`'${CLI}'`), true, need.command);
    assert.equal(need.command.startsWith("kirocrew "), false);
  });

  it("falls back to the bare name only when no CLI path is known", () => {
    const need = describeSandboxProfileNeed({ ...restricted, cliBin: undefined });

    assert.match(need.command, /^'kirocrew' sandbox install-profile/);
  });

  it("quotes a CLI path containing spaces", () => {
    const need = describeSandboxProfileNeed({
      ...restricted,
      cliBin: "/opt/Kiro Crew/bin/kirocrew",
    });

    assert.equal(need.command.includes("'/opt/Kiro Crew/bin/kirocrew'"), true);
  });

  // The sysctl being exactly 1 is the discriminator for the whole feature, not
  // the presence of AppArmor: Debian 13 ships AppArmor and is unaffected. A
  // reader that keyed on AppArmor alone would nag every Debian user.
  it("says nothing when the kernel does not restrict user namespaces", () => {
    assert.equal(
      describeSandboxProfileNeed({ ...restricted, readSysctl: sysctl("0\n") }),
      null,
    );
  });

  it("says nothing when the sysctl does not exist", () => {
    assert.equal(
      describeSandboxProfileNeed({ ...restricted, readSysctl: sysctl(null) }),
      null,
    );
  });

  it("says nothing on macOS and Windows", () => {
    for (const platform of ["darwin", "win32"]) {
      assert.equal(describeSandboxProfileNeed({ ...restricted, platform }), null);
    }
  });

  // No $APPIMAGE means a dev run, a .deb install, or a reused gateway. There is
  // no safe path to attach to in those shapes, so the advisory would be wrong.
  it("says nothing when the launch is not an AppImage", () => {
    assert.equal(describeSandboxProfileNeed({ ...restricted, env: {} }), null);
    assert.equal(
      describeSandboxProfileNeed({ ...restricted, env: { APPIMAGE: "   " } }),
      null,
    );
  });

  it("tolerates a missing env object", () => {
    assert.equal(
      describeSandboxProfileNeed({ ...restricted, env: undefined }),
      null,
    );
  });

  // The command is meant to be pasted into a shell as-is, and an AppImage can
  // legitimately sit under a path with spaces.
  it("quotes a path containing spaces", () => {
    const need = describeSandboxProfileNeed({
      ...restricted,
      env: { APPIMAGE: "/home/u/My Apps/kirocrew.AppImage" },
    });

    assert.equal(need.command.includes("'/home/u/My Apps/kirocrew.AppImage'"), true);
  });

  // A single quote in the path would otherwise close the quoted argument and let
  // the rest of the path be read as shell syntax.
  it("escapes a single quote in the path", () => {
    const need = describeSandboxProfileNeed({
      ...restricted,
      env: { APPIMAGE: "/home/u/Bob's Apps/kirocrew.AppImage" },
    });

    assert.equal(need.command.includes("Bob'\\''s Apps"), true);
    assert.equal(need.command.includes("Bob's Apps"), false);
  });

  it("never throws when the reader blows up", () => {
    assert.equal(
      describeSandboxProfileNeed({
        ...restricted,
        readSysctl: () => {
          throw new Error("EACCES");
        },
      }),
      null,
    );
  });
});
