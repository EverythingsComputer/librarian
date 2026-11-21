#!/usr/bin/env python3
"""Publish dist/ assets to the GitHub Release for TAG (get-or-create).

Idempotent: an existing asset with the same name is deleted, then re-uploaded.
Retries uploads with backoff. Needs GITHUB_TOKEN + GITHUB_REPOSITORY env.
Failures raise, so the calling workflow step goes red."""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"


def req(method: str, url: str, body: bytes | None = None,
        content_type: str = "application/json") -> tuple[int, bytes]:
    r = urllib.request.Request(url, data=body, method=method)
    r.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    r.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        r.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dist", default="dist")
    args = ap.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]

    status, body = req("GET", f"{API}/repos/{repo}/releases/tags/{args.tag}")
    if status == 200:
        rel_id = json.loads(body)["id"]
        print(f"release exists: id {rel_id}")
    else:
        payload = json.dumps({
            "tag_name": args.tag, "name": args.tag,
            "draft": False, "prerelease": args.tag.endswith("-beta")
            or args.tag.endswith("-rc")}).encode()
        status, body = req("POST", f"{API}/repos/{repo}/releases", payload)
        if status not in (200, 201):
            print(f"::error::could not create release: {status} {body[:400]!r}")
            return 1
        rel_id = json.loads(body)["id"]  # id is a JSON number — parse it as one
        print(f"release created: id {rel_id}")

    files = sorted(Path(args.dist).glob("*"))
    if not files:
        print("::error::dist/ is empty — nothing to publish")
        return 1

    for f in files:
        name = f.name
        # drop any previous copy of this asset (reruns otherwise 422)
        status, body = req("GET", f"{API}/repos/{repo}/releases/{rel_id}/assets")
        if status == 200:
            for a in json.loads(body):
                if a["name"] == name:
                    req("DELETE", a["url"])
                    print(f"deleted stale asset {name}")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        url = f"{UPLOAD}/repos/{repo}/releases/{rel_id}/assets?name={name}"
        data = f.read_bytes()
        for attempt in range(1, 4):
            status, body = req("POST", url, data, ctype)
            if status in (200, 201):
                dl = json.loads(body)["browser_download_url"]
                print(f"uploaded {name} ({len(data)/1e6:.1f} MB) -> {dl}")
                break
            print(f"attempt {attempt} for {name} failed: {status} {body[:200]!r}")
            time.sleep(5 * attempt)
        else:
            print(f"::error::gave up uploading {name}")
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # surface the reason as a step annotation, not just a traceback
        print(f"::error::publish failed: {type(e).__name__}: {e}")
        raise
