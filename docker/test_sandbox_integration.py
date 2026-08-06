"""
Integration test for sandbox Docker detection fix (issue #1617).

Run inside a container:
  Step 1 (default seccomp, no profile):
    docker run --rm -v .:/repo -w /repo python:3.12-slim \
        bash -c "pip install -e . -q && python docker/test_sandbox_integration.py step1"

  Step 2 (with kirocrew seccomp profile):
    docker run --rm --security-opt seccomp=docker/seccomp/kirocrew-seccomp.json \
        -v .:/repo -w /repo python:3.12-slim \
        bash -c "pip install -e . -q && python docker/test_sandbox_integration.py step2"

  Step 3 (unsandboxed consent via env var):
    docker run --rm -e KIROCREW_ALLOW_UNSANDBOXED=1 \
        -v .:/repo -w /repo python:3.12-slim \
        bash -c "pip install -e . -q && python docker/test_sandbox_integration.py step3"
"""
import sys


def banner(title):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def step1_reproduce_issue():
    """Default Docker seccomp blocks unshare → must show Docker-specific guidance."""
    banner("STEP 1: Reproduce issue #1617 (default seccomp)")

    from kiro_crew.sandbox import detect_backend, is_docker_container, wrap_argv

    print(f"  in_container : {is_docker_container()}")
    print(f"  backend      : {detect_backend()}")

    assert is_docker_container(), "FAIL: should be inside a container"
    assert detect_backend() == "none", f"FAIL: expected 'none', got {detect_backend()!r}"

    try:
        wrap_argv(["kiro-cli", "chat"], mode="auto")
        print("FAIL: expected SandboxUnavailableError but nothing was raised")
        sys.exit(1)
    except Exception as e:
        msg = str(e)
        print()
        print("Error message (first 600 chars):")
        print(msg[:600])

        # Validate the new Docker-specific guidance is present
        checks = {
            "mentions seccomp profile": "seccomp" in msg,
            "mentions KIROCREW_ALLOW_UNSANDBOXED": "KIROCREW_ALLOW_UNSANDBOXED" in msg,
            "does NOT say 'install a supported sandbox backend'": "install a supported sandbox backend" not in msg,
            "mentions docs/guides/docker.md": "docs/guides/docker.md" in msg,
        }
        print()
        all_ok = True
        for desc, ok in checks.items():
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {desc}")
            if not ok:
                all_ok = False

        if all_ok:
            print()
            print("STEP 1 PASSED — Docker-specific guidance is shown correctly")
        else:
            sys.exit(1)


def step2_with_seccomp_profile():
    """With the Kiro Crew seccomp profile, the inner sandbox should work."""
    banner("STEP 2: With kirocrew-seccomp.json profile")

    from kiro_crew.sandbox import detect_backend, is_docker_container, userns_available

    print(f"  in_container    : {is_docker_container()}")
    print(f"  userns_available: {userns_available()}")
    print(f"  backend         : {detect_backend()}")

    assert is_docker_container(), "FAIL: should be inside a container"

    backend = detect_backend()
    userns = userns_available()

    if backend == "namespace":
        print()
        print("STEP 2 PASSED — namespace backend active, inner sandbox works")
    elif not userns:
        # WSL2 kernel without CONFIG_USER_NS — expected failure mode
        print()
        print("NOTE: userns_available() = False — WSL2 kernel may lack CONFIG_USER_NS.")
        print("This is an expected failure on WSL2 Docker CE without user namespace support.")
        print("Use Option B (KIROCREW_ALLOW_UNSANDBOXED=1) instead. See docs/docker.md.")
    else:
        print(f"FAIL: expected backend='namespace', got {backend!r}")
        sys.exit(1)


def step3_unsandboxed_consent():
    """With sandbox_allow_unsandboxed_exec set, wrap_argv should pass through without error."""
    banner("STEP 3: Unsandboxed consent (sandbox_allow_unsandboxed_exec)")

    import os

    from kiro_crew.sandbox import detect_backend, is_docker_container, wrap_argv

    print(f"  in_container              : {is_docker_container()}")
    print(f"  backend                   : {detect_backend()}")
    print(f"  KIROCREW_ALLOW_UNSANDBOXED: {os.environ.get('KIROCREW_ALLOW_UNSANDBOXED', '(not set)')}")
    print()
    print("  Note: KIROCREW_ALLOW_UNSANDBOXED=1 is processed by the container")
    print("  entrypoint, which writes sandbox_allow_unsandboxed_exec=true into")
    print("  config.json. Testing the underlying config flag directly instead.")

    assert is_docker_container(), "FAIL: should be inside a container"

    # Patch the config-read function directly since we have no entrypoint here.
    import kiro_crew.sandbox as _sandbox
    original = _sandbox._allow_unsandboxed_exec
    _sandbox._allow_unsandboxed_exec = lambda: True
    try:
        argv, _ = wrap_argv(["kiro-cli", "chat"], mode="auto")
        print(f"  passed through argv: {argv}")
        print()
        print("STEP 3 PASSED — wrap_argv returned without error under unsandboxed consent")
    except Exception as e:
        print(f"FAIL: unexpected error: {e}")
        sys.exit(1)
    finally:
        _sandbox._allow_unsandboxed_exec = original


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "step1"
    if step == "step1":
        step1_reproduce_issue()
    elif step == "step2":
        step2_with_seccomp_profile()
    elif step == "step3":
        step3_unsandboxed_consent()
    else:
        print(f"Unknown step: {step}")
        sys.exit(1)
