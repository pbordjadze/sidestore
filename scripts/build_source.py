#!/usr/bin/env python3
"""Builds source.json, one SideStore/AltStore source listing every app.

    scripts/build_source.py            # writes source.json if anything changed

sources.json lists the apps in order. A "remote" app comes from another public
repo's own source (its CI keeps that up to date, so nothing changes there). A
"local" app is an entry file in apps/, pushed here by a private repo's CI along
with its IPA as a release of this repo. If a remote source can't be fetched, the
script stops without writing, so a hiccup never drops an app from the source.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = ("name", "bundleIdentifier", "versions", "downloadURL", "iconURL")
# Every URL in an app entry (IPA downloads, icons, screenshots) must be on my GitHub.
# A remote source is another repo's branch: if that repo's CI or token is ever
# compromised, it can't point the iPhone at an IPA hosted somewhere else.
TRUSTED_HOSTS = ("github.com", "raw.githubusercontent.com")
TRUSTED_OWNER = "pbordjadze"


def trusted(url):
    """https://github.com/pbordjadze/... or https://raw.githubusercontent.com/pbordjadze/..., with no
    `..` (which a client resolves to another owner) and nothing but the host in the authority."""
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    segments = urllib.parse.unquote(parts.path).split("/")
    return (
        parts.scheme == "https"
        and parts.netloc in TRUSTED_HOSTS
        and len(segments) > 2
        and segments[0] == ""
        and segments[1] == TRUSTED_OWNER
        and not any(seg in (".", "..") for seg in segments)
        and "\\" not in parts.path
    )


def urls(value, key=""):
    """(key, url) for every value under a key that names a URL (iconURL, downloadURL, screenshotURLs, ...)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from urls(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from urls(v, key)
    elif "url" in key.lower():
        yield key, value


def check_entry(spec, apps):
    """An entry in sources.json lists the bundle ids it may publish; nothing else gets in."""
    where = spec.get("remote") or spec.get("local")
    allowed = spec.get("bundleIdentifiers") or []
    for app in apps:
        if not isinstance(app, dict):
            sys.exit(f"{where}: an app entry isn't an object")
        if app.get("bundleIdentifier") not in allowed:
            sys.exit(
                f"{where} lists {app.get('bundleIdentifier')!r}, which isn't in its bundleIdentifiers "
                f"in sources.json ({', '.join(allowed) or 'none'}); add it there if it's really mine"
            )
        for key, url in urls(app):
            if not trusted(url):
                sys.exit(f"{where}: {app['bundleIdentifier']} {key} {url!r} isn't on {TRUSTED_OWNER}'s GitHub")


def fetch(url):
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    config = json.load(open(os.path.join(ROOT, "sources.json")))
    apps = []
    for spec in config["apps"]:
        if "remote" in spec:
            try:
                remote = fetch(spec["remote"])
            except Exception as error:  # noqa: BLE001 - any failure means "try again later"
                sys.exit(f"Couldn't fetch {spec['remote']}: {error}; leaving source.json as it is")
            found = remote.get("apps", []) if isinstance(remote, dict) else None
            if not isinstance(found, list):
                sys.exit(f"{spec['remote']} has no list of apps; leaving source.json as it is")
        else:
            path = os.path.join(ROOT, spec["local"])
            if not os.path.exists(path):
                print(f"{spec['local']} not published yet; skipping")
                continue
            found = [json.load(open(path))]
        check_entry(spec, found)
        apps.extend(found)

    seen = set()
    for app in apps:
        missing = [key for key in REQUIRED if not app.get(key)]
        if missing:
            sys.exit(f"{app.get('name', '?')} is missing {', '.join(missing)}")
        if app["bundleIdentifier"] in seen:
            sys.exit(f"{app['bundleIdentifier']} is listed twice")
        seen.add(app["bundleIdentifier"])

    source = {key: config[key] for key in ("name", "identifier", "subtitle", "website", "tintColor", "iconURL")}
    source["apps"] = apps
    source["news"] = []
    text = json.dumps(source, indent=2, ensure_ascii=False) + "\n"

    out = os.path.join(ROOT, "source.json")
    old = open(out).read() if os.path.exists(out) else ""
    if text == old:
        print("source.json unchanged")
        return
    with open(out, "w") as f:
        f.write(text)
    print(f"source.json: {', '.join(a['name'] + ' ' + a.get('version', '?') for a in apps)}")


if __name__ == "__main__":
    main()
