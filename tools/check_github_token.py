#!/usr/bin/env python3
"""
Pre-flight check for a GitHub token, before building a workflow around it.

Why this exists: setting up Release Manager's real-GitHub testing cost two
separate debugging rounds purely from token-scope confusion — first
assuming "Releases" was its own fine-grained-PAT permission (it's bundled
under "Contents"), then discovering the token was still read-only only
once a live `publish` call failed with a 403. Both would have been caught
in seconds by a check like this one, run BEFORE writing a single line of
integration code — the same principle `llm_client._get_client()` already
applies to FACTORY_API_KEY (fail fast and specifically, before doing
anything else), just for a new external integration instead of the one
this harness already had.

No dependencies beyond the standard library, so it can run standalone —
you shouldn't need this project's own venv just to sanity-check a token
before deciding whether to build anything against it at all.

Usage:
    GITHUB_TOKEN=... python tools/check_github_token.py <owner>/<repo>
"""
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.github.com"


def _request(token: str, path: str):
    """Returns (status_code, parsed_json_or_None)."""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return e.code, None
    except urllib.error.URLError as e:
        return None, {"error": str(e)}


def main():
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        print("Usage: GITHUB_TOKEN=... python tools/check_github_token.py <owner>/<repo>")
        sys.exit(2)
    owner_repo = sys.argv[1]

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("FAIL: GITHUB_TOKEN is not set in the environment.")
        sys.exit(1)

    checks_failed = 0

    # 1. Is the token valid at all?
    status, data = _request(token, "/rate_limit")
    if status == 200:
        print("PASS: token is valid and authenticates.")
    elif status == 401:
        print("FAIL: token is invalid or expired (401 on an authenticated endpoint).")
        sys.exit(1)
    else:
        print(f"FAIL: could not reach GitHub to validate the token (status={status}, {data}).")
        checks_failed += 1
        # Nothing further can be meaningfully checked without a working token.
        sys.exit(1)

    # 2. Can it see the target repo at all?
    status, data = _request(token, f"/repos/{owner_repo}")
    if status == 200:
        print(f"PASS: token can read {owner_repo}.")
    elif status == 404:
        print(
            f"FAIL: {owner_repo} is not visible to this token (404) — either it doesn't "
            f"exist, the owner/name is wrong, or this fine-grained PAT's repository "
            f"access doesn't include it."
        )
        checks_failed += 1
        sys.exit(1 if checks_failed else 0)
    else:
        print(f"FAIL: unexpected response reading {owner_repo} (status={status}, {data}).")
        checks_failed += 1
        sys.exit(1)

    # 3. Does it actually have write access? (the exact thing that bit us:
    # a token that can READ a repo fine but is still Contents:Read-only,
    # which only surfaces as a failure much later during a real publish
    # call unless checked explicitly, here, up front.)
    permissions = (data or {}).get("permissions") or {}
    if permissions.get("push") is True:
        print("PASS: token has push/write access to this repository (covers creating releases).")
    elif permissions.get("push") is False:
        print(
            "FAIL: token does NOT have push/write access to this repository. "
            "For a fine-grained PAT, set Contents: Read and write (this also covers "
            "releases — there is no separate 'Releases' permission)."
        )
        checks_failed += 1
    else:
        print(
            "WARN: GitHub didn't report an explicit push permission for this token "
            "(field absent) — cannot confirm write access from this check alone. "
            "A real `publish` call is the only way to be certain."
        )

    # 4. Can it read pull requests? (Release Manager's scan needs this.)
    status, _data = _request(token, f"/repos/{owner_repo}/pulls?per_page=1&state=all")
    if status == 200:
        print("PASS: token can list pull requests on this repository.")
    else:
        print(f"FAIL: could not list pull requests (status={status}) — scan would fail on this.")
        checks_failed += 1

    print()
    if checks_failed:
        print(f"{checks_failed} check(s) failed — fix these before building against this token.")
        sys.exit(1)
    print("All checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
