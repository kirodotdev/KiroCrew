# Fixtures for semgrep/codeql-parity.yaml (the one Python rule), exercised by
# `semgrep --test` in the SAST job. `ruleid:` asserts the NEXT line MUST
# match; `ok:` asserts it must NOT. The negatives encode the precision
# defects found while the rule was written (PR #4838): a no-stream
# yaml.safe_dump only returns a string and persists nothing, and a
# credential-named *variable* is deliberately out of scope because this
# codebase legitimately persists its own gateway secret to a 0600 file.
#
# Test mode ignores the rule's `paths:` filters, so this file does not need
# to live under src/kiro_crew/. The normal scan never reads this directory
# (.semgrepignore), so the deliberately vulnerable code here cannot trip the
# SAST job itself.

import json

import yaml


def clear_text_credential_dump(fh, password, key, secret, url):
    # ruleid: kirocrew.clear-text-credential-dump
    json.dump({"password": password}, fh)
    # ruleid: kirocrew.clear-text-credential-dump
    yaml.dump({"api_key": key}, fh)
    # ruleid: kirocrew.clear-text-credential-dump
    yaml.safe_dump({"client_secret": secret}, fh)
    # The stream= keyword form persists just the same.
    # ruleid: kirocrew.clear-text-credential-dump
    yaml.safe_dump({"password": password}, stream=fh)
    # Without a stream, safe_dump returns a string and nothing reaches disk.
    # ok: kirocrew.clear-text-credential-dump
    rendered = yaml.safe_dump({"password": password})
    # json.dumps returns a string; only json.dump takes a file object.
    # ok: kirocrew.clear-text-credential-dump
    blob = json.dumps({"password": password})
    # A non-credential field name stays clean.
    # ok: kirocrew.clear-text-credential-dump
    json.dump({"endpoint": url}, fh)
    # Writing a credential-named VARIABLE is deliberately not flagged: the
    # 0600-file distinction is not visible syntactically.
    # ok: kirocrew.clear-text-credential-dump
    fh.write(password)
    return rendered, blob
