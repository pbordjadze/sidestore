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
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = ("name", "bundleIdentifier", "versions", "downloadURL", "iconURL")


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
            apps.extend(remote.get("apps", []))
        else:
            path = os.path.join(ROOT, spec["local"])
            if not os.path.exists(path):
                print(f"{spec['local']} not published yet; skipping")
                continue
            apps.append(json.load(open(path)))

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
