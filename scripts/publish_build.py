#!/usr/bin/env python3
"""Publishes one build of a private app to this repo, for its CI to run after a green build.

    scripts/publish_build.py --app-id myapp --version 1.2.3 --build 57 --ipa MyApp.ipa \\
        --manifest myapp.manifest.json [--notes "what changed"] [--icon icon.png]

What it does, in one go:
  * puts the IPA on the `builds` branch as <app-id>/<IpaName>-<build>.ipa (one force-pushed
    commit holding every app's newest builds, like Treasurr's relay);
  * adds the build to apps/<app-id>.json on `main` (newest KEEP builds, same shape as Treasurr's).

It only ever writes under apps/<app-id>.json, icons/<app-id>.png and builds:<app-id>/. Both pushes
are retried from a fresh fetch if another publisher got in first, so several apps can publish at
once. `main` is never force-pushed; `builds` is pushed with --force-with-lease against the exact
commit this run read, so it can't discard another app's IPA.

Trust rules (scripts/build_source.py's, reused rather than copied): the app must already have a
`local` entry in sources.json naming its bundle id, which only the owner adds by hand. Every URL in
the entry is generated here, on pbordjadze's GitHub, never taken from the manifest. The IPA's own
Info.plist must carry that bundle id and the build number. Nothing is ever added to sources.json.

Credentials: none live here. Git uses whatever the environment gives it (the Mac's CI sets
GIT_SSH_COMMAND to point at its deploy key). Fetching uses the public https URL; pushing uses ssh.

Manifest (JSON, optional; the metadata that doesn't change per build). Allowed keys: name,
bundleIdentifier, developerName, subtitle, localizedDescription, tintColor, category, minOSVersion,
ipaName. name and bundleIdentifier are required the first time an app is published.
"""
import argparse
import json
import os
import plistlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_source  # noqa: E402 - the trust rules live there

KEEP = 5  # builds listed per app; build_source's README says the same
OWNER = build_source.TRUSTED_OWNER
REPO = "sidestore"
DEFAULT_FETCH = f"https://github.com/{OWNER}/{REPO}.git"
DEFAULT_PUSH = f"git@github.com:{OWNER}/{REPO}.git"
RAW = f"https://raw.githubusercontent.com/{OWNER}/{REPO}"
ATTEMPTS = 8

APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,30}$")
BUILD = re.compile(r"^[0-9]{1,9}$")
IPA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
MANIFEST_KEYS = ("name", "bundleIdentifier", "developerName", "subtitle", "localizedDescription",
                 "tintColor", "category", "minOSVersion", "ipaName")

_after_fetch = lambda: None  # noqa: E731 - tests hook in here to simulate a competing publisher


class Retry(Exception):
    """A push lost a race (or the network blinked); fetch again and redo it."""


def die(message):
    sys.exit(f"publish_build: {message}")


def git(cwd, *args, check=True, env=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **(env or {}))
    result = subprocess.run(["git", "-c", "user.name=private-app-publisher",
                             "-c", "user.email=ci@users.noreply.github.com", *args],
                            cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def read_ipa(path, app_id):
    """Bundle id and build number from the IPA's own Info.plist."""
    try:
        with zipfile.ZipFile(path) as z:
            plists = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
            if len(plists) != 1:
                die(f"{path} doesn't look like an IPA (no single Payload/*.app/Info.plist)")
            info = plistlib.loads(z.read(plists[0]))
    except (zipfile.BadZipFile, plistlib.InvalidFileException, OSError) as error:
        die(f"can't read {path} as an IPA: {error}")
    privacy = {k: v for k, v in sorted(info.items()) if k.startswith("NS") and k.endswith("UsageDescription")}
    return info.get("CFBundleIdentifier"), str(info.get("CFBundleVersion")), privacy


def load_manifest(path):
    if not path:
        return {}
    try:
        manifest = json.load(open(path))
    except (OSError, ValueError) as error:
        die(f"can't read manifest {path}: {error}")
    if not isinstance(manifest, dict):
        die("manifest isn't a JSON object")
    extra = sorted(set(manifest) - set(MANIFEST_KEYS))
    if extra:
        die(f"manifest keys not allowed: {', '.join(extra)} (allowed: {', '.join(MANIFEST_KEYS)}); "
            "URLs are generated here, never taken from a manifest")
    for key, value in manifest.items():
        if not isinstance(value, str) or not value.strip():
            die(f"manifest {key} must be a non-empty string")
    return manifest


def make_versions(existing, new, build):
    """Existing builds plus the new one: newest KEEP, one per build number."""
    versions = [v for v in existing if str(v.get("buildVersion")) != build] + [new]
    versions.sort(key=lambda v: int(v["buildVersion"]), reverse=True)
    if new not in versions[:KEEP]:
        die(f"build {build} is older than the {KEEP} newest builds already published; not adding it")
    return versions[:KEEP]


def attempt(args, meta, ipa_info, work):
    fetch = lambda ref: git(work, "fetch", "-q", "origin", ref, check=False)  # noqa: E731
    if fetch("main").returncode:
        raise Retry("couldn't fetch main")
    have_builds = fetch("+refs/heads/builds:refs/remotes/origin/builds").returncode == 0
    _after_fetch()

    main_dir = os.path.join(work, "main-tree")
    shutil.rmtree(main_dir, ignore_errors=True)
    git(work, "worktree", "prune")
    git(work, "worktree", "add", "-q", "--detach", main_dir, "refs/remotes/origin/main")
    main_sha = git(main_dir, "rev-parse", "HEAD").stdout.strip()

    # --- what main says about this app, and whether the owner has allowed it
    config = json.load(open(os.path.join(main_dir, "sources.json")))
    rel = f"apps/{args.app_id}.json"
    spec = next((s for s in config["apps"] if s.get("local") == rel), None)
    if spec is None:
        die(f"sources.json has no {{\"local\": \"{rel}\"}} entry; add it (with the app's bundleIdentifiers) "
            "by hand first, as the README's \"Adding a private app\" says. This script never edits sources.json")
    entry_path = os.path.join(main_dir, rel)
    entry = json.load(open(entry_path)) if os.path.exists(entry_path) else {}
    if not entry and not ("name" in meta and "bundleIdentifier" in meta):
        die("first publish of this app: the manifest needs at least name and bundleIdentifier")
    entry.update(meta_no_ipa(meta))
    bundle = entry.get("bundleIdentifier")
    if bundle != ipa_info[0]:
        die(f"IPA's CFBundleIdentifier {ipa_info[0]!r} isn't the entry's {bundle!r}")
    if bundle not in (spec.get("bundleIdentifiers") or []):
        die(f"{bundle!r} isn't in the bundleIdentifiers sources.json allows for {rel}")

    # minOSVersion lives on each version, as in Treasurr's entry
    previous = (entry.get("versions") or [{}])[0].get("minOSVersion")
    min_os = meta.get("minOSVersion") or previous or "18.0"
    ipa_name = meta.get("ipaName") or re.sub(r"[^A-Za-z0-9]+", "", entry["name"]) or args.app_id
    if not IPA_NAME.match(ipa_name):
        die(f"bad ipaName {ipa_name!r}")
    ipa_file = f"{ipa_name}-{args.build}.ipa"
    new = {"version": args.version, "buildVersion": args.build,
           "date": args.date, "localizedDescription": args.notes or f"Build {args.build}",
           "downloadURL": f"{RAW}/builds/{args.app_id}/{ipa_file}",
           "size": os.path.getsize(args.ipa), "minOSVersion": min_os}
    entry["versions"] = make_versions(entry.get("versions", []), new, args.build)
    latest = entry["versions"][0]  # legacy top-level fields for clients that predate `versions`
    entry.update(version=latest["version"], versionDate=latest["date"], versionDescription=latest["localizedDescription"],
                 downloadURL=latest["downloadURL"], size=latest["size"])
    # privacy descriptions come from the built app's Info.plist, as the public apps' CI does
    entry["appPermissions"] = {"entitlements": (entry.get("appPermissions") or {}).get("entitlements", []),
                               "privacy": ipa_info[2]}
    entry.setdefault("developerName", OWNER)
    entry["iconURL"] = f"{RAW}/main/icons/{args.app_id}.png"
    icon_rel = f"icons/{args.app_id}.png"
    if not args.icon and not os.path.exists(os.path.join(main_dir, icon_rel)):
        die(f"first publish needs --icon (a PNG), which becomes {icon_rel}")

    # --- the same checks build_source.py applies when it builds source.json
    build_source.check_entry(spec, [entry])
    missing = [k for k in build_source.REQUIRED if not entry.get(k)]
    if missing:
        die(f"entry would be missing {', '.join(missing)}")
    others = set()
    for s in config["apps"]:
        p = os.path.join(main_dir, s["local"]) if "local" in s else None
        if p and p != entry_path and os.path.exists(p):
            others.add(json.load(open(p)).get("bundleIdentifier"))
    if bundle in others:
        die(f"{bundle} is already published by another entry")

    # --- builds branch: start from what's there, touch only <app-id>/ (index plumbing, no checkout)
    idx = {"GIT_INDEX_FILE": os.path.join(work, "builds.idx")}
    if os.path.exists(idx["GIT_INDEX_FILE"]):
        os.remove(idx["GIT_INDEX_FILE"])
    if have_builds:
        lease = git(work, "rev-parse", "refs/remotes/origin/builds").stdout.strip()
        old_tree = git(work, "rev-parse", lease + "^{tree}").stdout.strip()
        git(work, "read-tree", old_tree, env=idx)
    else:
        lease, old_tree = "", None
    # Keep the KEEP newest builds counting both main's entry and the IPAs already here: an IPA main
    # doesn't list yet belongs to a publisher that has pushed here but not to main, and must survive.
    prefix = args.app_id + "/"
    found = {}
    for path in git(work, "ls-files", env=idx).stdout.splitlines():
        m = re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_.-]+-([0-9]+)\.ipa", path)
        if m:
            found[int(m.group(1))] = path
    wanted = {int(v["buildVersion"]) for v in entry["versions"]} | set(found) | {int(args.build)}
    for build in sorted(wanted, reverse=True)[KEEP:]:
        if build in found:
            git(work, "update-index", "--force-remove", found[build], env=idx)
    blob = git(work, "hash-object", "-w", args.ipa).stdout.strip()
    git(work, "update-index", "--add", "--cacheinfo", f"100644,{blob},{prefix}{ipa_file}", env=idx)
    new_tree = git(work, "write-tree", env=idx).stdout.strip()
    if new_tree != old_tree:
        commit = git(work, "commit-tree", new_tree, "-m", f"builds: {args.app_id} {args.build}").stdout.strip()
        if git(work, "push", "-q", "--force-with-lease=refs/heads/builds:" + lease,
               "push-target", f"{commit}:refs/heads/builds", check=False).returncode:
            raise Retry("builds moved under us")

    # --- main: one commit on the fetched tip, never forced
    os.makedirs(os.path.join(main_dir, "icons"), exist_ok=True)
    os.makedirs(os.path.dirname(entry_path), exist_ok=True)
    with open(entry_path, "w") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if args.icon:
        shutil.copyfile(args.icon, os.path.join(main_dir, icon_rel))
    git(main_dir, "add", rel, *([icon_rel] if args.icon else []))
    if git(main_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        print("main already has this build")
        return
    git(main_dir, "commit", "-q", "-m", f"{entry['name']} {args.version} ({args.build})")
    if git(main_dir, "push", "-q", "push-target", f"HEAD:refs/heads/main", check=False).returncode:
        raise Retry("main moved under us")
    print(f"published {entry['name']} {args.version} ({args.build}) at {git(main_dir, 'rev-parse', 'HEAD').stdout.strip()[:10]}"
          f" (main was {main_sha[:10]})")


def meta_no_ipa(meta):
    return {k: v for k, v in meta.items() if k not in ("ipaName", "minOSVersion")}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--app-id", required=True, help="lowercase id; names apps/<id>.json and builds:<id>/")
    p.add_argument("--version", required=True)
    p.add_argument("--build", required=True, help="build number (digits); must match the IPA's CFBundleVersion")
    p.add_argument("--ipa", required=True)
    p.add_argument("--manifest")
    p.add_argument("--notes", help="release notes for this build; they become public")
    p.add_argument("--icon", help="PNG, copied to icons/<app-id>.png; required the first time")
    p.add_argument("--date", help="ISO 8601 UTC; default now")
    p.add_argument("--remote", default=os.environ.get("SIDESTORE_FETCH_URL", DEFAULT_FETCH))
    p.add_argument("--push-remote", default=os.environ.get("SIDESTORE_PUSH_URL"),
                   help=f"default {DEFAULT_PUSH}, or --remote if that was given")
    args = p.parse_args()

    if not APP_ID.match(args.app_id):
        die("app id must be lowercase letters, digits and dashes")
    if not VERSION.match(args.version):
        die("bad --version")
    if not BUILD.match(args.build):
        die("--build must be digits")
    args.build = str(int(args.build))
    if not os.path.isfile(args.ipa):
        die(f"no such IPA: {args.ipa}")
    if args.icon:
        if not os.path.isfile(args.icon) or open(args.icon, "rb").read(8) != b"\x89PNG\r\n\x1a\n":
            die("--icon must be a PNG file")
    args.date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", args.date):
        die("--date must look like 2026-11-08T16:42:20Z")
    push_remote = args.push_remote or (args.remote if args.remote != DEFAULT_FETCH else DEFAULT_PUSH)

    meta = load_manifest(args.manifest)
    ipa_info = read_ipa(args.ipa, args.app_id)
    if ipa_info[1] != args.build:
        die(f"IPA's CFBundleVersion is {ipa_info[1]}, not --build {args.build}")

    work = tempfile.mkdtemp(prefix="publish-build-")
    try:
        git(work, "init", "-q")
        git(work, "remote", "add", "origin", args.remote)
        git(work, "remote", "add", "push-target", push_remote)
        for n in range(1, ATTEMPTS + 1):
            try:
                attempt(args, meta, ipa_info, work)
                return
            except Retry as why:
                print(f"attempt {n}/{ATTEMPTS}: {why}; trying again", file=sys.stderr)
                time.sleep(random.uniform(0, min(0.2 * 2 ** n, 8)))
        die(f"gave up after {ATTEMPTS} attempts")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
