# Fixtures for semgrep/supply-chain.yaml (the four Python rules), exercised
# by `semgrep --test` in the SAST job. `ruleid:` asserts the NEXT line MUST
# match; `ok:` asserts it must NOT.
#
# Test mode ignores the rules' `paths:` filters (the `test/*` excludes), so
# these fixtures fire in test mode regardless. The normal scan never reads
# this directory (.semgrepignore), so the deliberately vulnerable code here
# cannot trip the SAST job itself.

import subprocess
import urllib.request
from urllib.request import urlretrieve

import requests


def download_and_exec_subprocess(url, dest_file):
    # ruleid: kirocrew.download-and-exec-subprocess
    subprocess.run(["curl", "-o", dest_file, url], check=True)
    # ruleid: kirocrew.download-and-exec-subprocess
    subprocess.Popen(["wget", url])
    # An ordinary non-download subprocess stays clean.
    # ok: kirocrew.download-and-exec-subprocess
    subprocess.run(["ls", "-la"], check=True)


def download_and_exec_urlretrieve(url, dest_file):
    # ruleid: kirocrew.download-and-exec-urlretrieve
    urllib.request.urlretrieve(url, dest_file)
    # ruleid: kirocrew.download-and-exec-urlretrieve
    urlretrieve(url, dest_file)
    # Reading a response without persisting a file is a different shape.
    # ok: kirocrew.download-and-exec-urlretrieve
    urllib.request.urlopen(url)


def download_then_exec(url):
    # The canonical download+execute supply-chain pattern.
    # ruleid: kirocrew.download-then-exec
    resp = requests.get(url)
    exec(resp.text)


def download_then_parse(url):
    # Parsing a response without executing it stays clean.
    # ok: kirocrew.download-then-exec
    resp = requests.get(url)
    return resp.json()


def download_then_subprocess(fetch):
    # A path whose NAME says it was downloaded, then executed.
    # ruleid: kirocrew.download-then-subprocess
    download_path = fetch()
    subprocess.run([download_path], check=True)


def run_pinned_binary(config_path):
    # A path with no download/tmp/temp marker in its name stays clean.
    # ok: kirocrew.download-then-subprocess
    editor_bin = "/usr/bin/vim"
    subprocess.run([editor_bin, config_path], check=True)
