"use strict";

const test = require("node:test");
const assert = require("node:assert");

const {
  readSystemProxyEnvironment,
  proxyEnvFromScutilOutput,
  SCUTIL,
} = require("../system-proxy-env");

// A representative `scutil --proxy` dump for a host behind a manual corporate
// proxy (System Settings → Network → Proxies), the configuration a proxy-only
// network actually ships. Note what is NOT in the default ExceptionsList:
// localhost — so the discovered NO_PROXY must add the loopback itself.
const MANUAL_HTTPS_FIXTURE = `<dictionary> {
  HTTPEnable : 1
  HTTPPort : 7890
  HTTPProxy : proxy.corp.example
  HTTPSEnable : 1
  HTTPSPort : 7890
  HTTPSProxy : proxy.corp.example
  ExceptionsList : <array> {
    0 : *.local
    1 : 169.254/16
  }
  ProxyAutoConfigEnable : 0
  ProxyAutoDiscoveryEnable : 0
  SOCKSEnable : 0
}
`;

test("a manual system proxy becomes the standard proxy environment", () => {
  const env = proxyEnvFromScutilOutput(MANUAL_HTTPS_FIXTURE);

  assert.deepStrictEqual(env, {
    HTTP_PROXY: "http://proxy.corp.example:7890",
    HTTPS_PROXY: "http://proxy.corp.example:7890",
    NO_PROXY: ".local,localhost,127.0.0.1,::1",
  });
});

test("an IPv6 proxy host is bracketed in the proxy URL", () => {
  const env = proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 1
  HTTPPort : 3128
  HTTPProxy : fd00::7
  HTTPSEnable : 1
  HTTPSPort : 3128
  HTTPSProxy : [fd00::8]
}
`);
  assert.equal(env.HTTP_PROXY, "http://[fd00::7]:3128");
  assert.equal(env.HTTPS_PROXY, "http://[fd00::8]:3128", "an already-bracketed host is kept");
});

test("a bare * exception means connect directly, so nothing is discovered", () => {
  assert.equal(
    proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 1
  HTTPPort : 3128
  HTTPProxy : 10.0.0.1
  ExceptionsList : <array> {
    0 : *.local
    1 : *
  }
}
`),
    null,
  );
});

test("the loopback is never proxied, even when the system store omits it", () => {
  // loopback_http.py documents why Kiro Crew's own requests must not ride a
  // proxy: the X-Internal-Secret would leave the machine in cleartext. The
  // macOS default ExceptionsList does not name localhost, so discovery has to
  // add the bypass itself rather than trust the store.
  const env = proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 1
  HTTPPort : 3128
  HTTPProxy : 10.0.0.1
  HTTPSEnable : 0
  SOCKSEnable : 0
}
`);
  assert.equal(env.HTTP_PROXY, "http://10.0.0.1:3128");
  assert.equal(env.HTTPS_PROXY, undefined);
  for (const entry of ["localhost", "127.0.0.1", "::1"]) {
    assert.ok(
      env.NO_PROXY.split(",").includes(entry),
      `NO_PROXY must bypass ${entry}`,
    );
  }
  assert.ok(!("HTTPS_PROXY" in env), "a disabled protocol must stay absent");
});

test("a SOCKS-only system proxy discovers nothing", () => {
  // Only the HTTP(S) proxy the issue reports is discovered; a SOCKS-only
  // store falls through to the inherited environment.
  assert.equal(
    proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 0
  HTTPSEnable : 0
  SOCKSEnable : 1
  SOCKSPort : 1080
  SOCKSProxy : 10.0.0.7
}
`),
    null,
  );
});

test("macOS exception syntax becomes NO_PROXY syntax", () => {
  // Env-var matchers (urllib, requests, curl) read NO_PROXY as host
  // suffixes: `*.corp.example` must become `.corp.example`, and a CIDR entry
  // they cannot read is dropped instead of posing as a bypass.
  const env = proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 1
  HTTPPort : 3128
  HTTPProxy : 10.0.0.1
  ExceptionsList : <array> {
    0 : *.local
    1 : *.corp.example
    2 : 169.254/16
    3 : intranet.example
  }
}
`);
  assert.equal(
    env.NO_PROXY,
    ".local,.corp.example,intranet.example,localhost,127.0.0.1,::1",
  );
});

test("a wildcard NO_PROXY cannot express discovers nothing", () => {
  // `*.*` or `10.*` bypasses hosts a NO_PROXY suffix list would proxy. Once
  // HTTP_PROXY is set, urllib stops asking the system matcher, so exporting a
  // narrower bypass would send direct-only traffic through the proxy.
  for (const wildcard of ["*.*", "10.*", "host*.corp.example", "*."]) {
    assert.equal(
      proxyEnvFromScutilOutput(`<dictionary> {
  HTTPSEnable : 1
  HTTPSPort : 3128
  HTTPSProxy : 10.0.0.1
  ExceptionsList : <array> {
    0 : *.local
    1 : ${wildcard}
  }
}
`),
      null,
      `exception ${wildcard} must stop discovery`,
    );
  }
});

test("a private CIDR bypass NO_PROXY cannot express discovers nothing", () => {
  // urllib matches NO_PROXY by name; the macOS matcher it replaces checks the
  // resolved address against the range. Only the default link-local entry is
  // dropped; any other range stops discovery.
  for (const cidr of ["10/8", "192.168.0.0/16", "172.16/12", "fd00::/8"]) {
    assert.equal(
      proxyEnvFromScutilOutput(`<dictionary> {
  HTTPSEnable : 1
  HTTPSPort : 3128
  HTTPSProxy : 10.0.0.1
  ExceptionsList : <array> {
    0 : *.local
    1 : 169.254/16
    2 : ${cidr}
  }
}
`),
      null,
      `exception ${cidr} must stop discovery`,
    );
  }
});

test("ExcludeSimpleHostnames discovers nothing, since NO_PROXY cannot say it", () => {
  const dump = (flag) => `<dictionary> {
  ExcludeSimpleHostnames : ${flag}
  HTTPSEnable : 1
  HTTPSPort : 3128
  HTTPSProxy : 10.0.0.1
}
`;
  assert.equal(proxyEnvFromScutilOutput(dump(1)), null);
  assert.equal(proxyEnvFromScutilOutput(dump(0)).HTTPS_PROXY, "http://10.0.0.1:3128");
});

test("no manual proxy (PAC or disabled) discovers nothing", () => {
  // A .pac script is a program this shell must not interpret; claiming no
  // proxy is the honest answer, the same fall-through an absent store gets.
  assert.equal(
    proxyEnvFromScutilOutput(`<dictionary> {
  ProxyAutoConfigEnable : 1
  ProxyAutoConfigURLString : http://wpad.corp.example/wpad.dat
  HTTPEnable : 0
  HTTPSEnable : 0
  SOCKSEnable : 0
}
`),
    null,
  );
  assert.equal(
    proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 0
  HTTPSEnable : 0
  SOCKSEnable : 0
}
`),
    null,
  );
  assert.equal(proxyEnvFromScutilOutput(""), null);
  assert.equal(proxyEnvFromScutilOutput(null), null);
});

test("an enabled protocol without a usable host:port pair is skipped", () => {
  const env = proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 1
  HTTPProxy : proxy.corp.example
  HTTPPort : 0
  HTTPSEnable : 1
  HTTPSProxy : proxy.corp.example
  SOCKSEnable : 0
}
`);
  assert.equal(env, null, "a port of 0 is not a proxy endpoint");
});

test("a proxy scoped to a domain or interface is never exported globally", () => {
  // A VPN can set an HTTPS proxy only for its corporate domains, under
  // __SUPPLEMENTAL__. Exporting it as HTTPS_PROXY would send every public
  // model request through a proxy meant for those domains only.
  assert.equal(
    proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 0
  HTTPSEnable : 0
  __SCOPED__ : <dictionary> {
    utun3 : <dictionary> {
      HTTPEnable : 1
      HTTPPort : 3128
      HTTPProxy : scoped.corp.example
    }
  }
  __SUPPLEMENTAL__ : <array> {
    0 : <dictionary> {
      HTTPSEnable : 1
      HTTPSPort : 8443
      HTTPSProxy : vpn-proxy.corp.example
      ExceptionsList : <array> {
        0 : *
      }
      MatchDomain : corp.example
    }
  }
}
`),
    null,
  );

  // A top-level proxy still wins, and nested keys never leak into it.
  const env = proxyEnvFromScutilOutput(`<dictionary> {
  ExceptionsList : <array> {
    0 : *.local
  }
  __SUPPLEMENTAL__ : <array> {
    0 : <dictionary> {
      ExceptionsList : <array> {
        0 : *
      }
      HTTPSEnable : 1
      HTTPSPort : 8443
      HTTPSProxy : vpn-proxy.corp.example
      MatchDomain : corp.example
    }
  }
  HTTPSEnable : 1
  HTTPSPort : 3128
  HTTPSProxy : proxy.corp.example
}
`);
  assert.deepStrictEqual(env, {
    HTTPS_PROXY: "http://proxy.corp.example:3128",
    NO_PROXY: ".local,localhost,127.0.0.1,::1",
  });
});

test("an older inline ExceptionsList form parses too", () => {
  const env = proxyEnvFromScutilOutput(`<dictionary> {
  HTTPEnable : 1
  HTTPPort : 8080
  HTTPProxy : 192.168.1.254
  HTTPSEnable : 0
  SOCKSEnable : 0
  ExceptionsList : { 127.0.0.1, 169.254/16 }
}
`);
  assert.equal(env.HTTP_PROXY, "http://192.168.1.254:8080");
  assert.ok(
    !env.NO_PROXY.split(",").includes("169.254/16"),
    "a CIDR entry is not NO_PROXY syntax",
  );
  assert.ok(env.NO_PROXY.split(",").includes("127.0.0.1"));
});

test("the scutil read is bounded, local, and macOS-only", () => {
  const calls = [];
  const execFileSync = (file, args, options) => {
    calls.push({ file, args, options });
    return MANUAL_HTTPS_FIXTURE;
  };

  const env = readSystemProxyEnvironment({ execFileSync, platform: "darwin" });

  assert.equal(env.HTTPS_PROXY, "http://proxy.corp.example:7890");
  assert.equal(calls.length, 1);
  // Absolute path, never a bare name — the same rule mac-env.js states for
  // launchctl, and it is cheap insurance against a shuffled PATH.
  assert.equal(calls[0].file, SCUTIL);
  assert.match(calls[0].file, /\/scutil$/);
  assert.deepStrictEqual(calls[0].args, ["--proxy"]);
  assert.equal(calls[0].options.encoding, "utf8");
  assert.ok(calls[0].options.timeout > 0, "the read is bounded");
  assert.ok(calls[0].options.maxBuffer > 0, "the read is size-bounded");
  assert.deepStrictEqual(calls[0].options.stdio, ["ignore", "pipe", "ignore"]);
});

test("off macOS, a missing scutil, or a wedged read, discover nothing", () => {
  const execFileSync = () => {
    throw new Error("scutil is gone");
  };
  assert.equal(readSystemProxyEnvironment({ execFileSync, platform: "darwin" }), null);
  assert.equal(readSystemProxyEnvironment({ execFileSync, platform: "win32" }), null);

  const untouchable = () => assert.fail("non-darwin must not shell out");
  assert.equal(readSystemProxyEnvironment({ execFileSync: untouchable, platform: "linux" }), null);
  assert.equal(readSystemProxyEnvironment({ platform: "darwin" }), null);
});
