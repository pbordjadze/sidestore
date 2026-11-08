# SideStore source

One SideStore source for all my apps. In SideStore: **Sources → +**, then

    https://raw.githubusercontent.com/pbordjadze/sidestore/main/source.json

| App | Comes from |
|---|---|
| Paint by Moonlight | [paint-by-number](https://github.com/pbordjadze/paint-by-number)'s own source (`sidestore` branch) |
| Beanbox | [beanbox](https://github.com/pbordjadze/beanbox)'s own source (`sidestore` branch) |
| Treasurr | Private repo; the seedbox relays each green build: the IPA goes on the `builds` branch, the entry in `apps/treasurr.json` |

## How it works

- `sources.json` lists the apps in order: either another public repo's source
  (`remote`) or an entry file in `apps/` (`local`).
- `.github/workflows/build.yml` runs `scripts/build_source.py` whenever an entry
  here changes, when Paint by Moonlight's CI publishes a build (a
  `repository_dispatch`), and every 15 minutes (off the hour and half hour, where
  GitHub drops scheduled runs), and commits `source.json` if it changed. A remote
  source that can't be fetched leaves `source.json` as it was.
- Each app keeps only its five newest builds.
- `builds` holds IPAs for apps whose own repos are private (served from
  raw.githubusercontent.com). It's force-pushed, so it never grows.

GitHub disables scheduled workflows in public repos after 60 days without
activity. Any app build pushed here counts as activity; if it does get disabled,
re-enable it under Actions, or run it by hand (workflow_dispatch).

## Adding an app

Every entry in `sources.json` names the `bundleIdentifiers` it may publish, and
every URL in an app (IPA, icon, screenshots) must be on
`https://github.com/pbordjadze/` or `https://raw.githubusercontent.com/pbordjadze/`.
Anything else stops the build and leaves `source.json` as it was, so a remote
source whose repo or CI is compromised can't add an app or point one at an IPA
hosted elsewhere.

- **Public repo with its own source:** add its `source.json` URL to `sources.json`,
  with its bundle id.
- **Private repo:** something with push access has to put the IPA where SideStore
  can download it and write `apps/<name>.json`. Treasurr does it from the seedbox
  with a deploy key (its `ci/relay_sidestore.py`): IPAs live on the `builds`
  branch, one force-pushed commit with the newest five builds.

## Adding a private app

`scripts/publish_build.py` is what a private app's CI runs after a green build on its
default branch. It puts the IPA on the `builds` branch (`<app-id>/<IpaName>-<build>.ipa`)
and adds the build to `apps/<app-id>.json` on `main`, keeping the five newest builds.

**One-time, by hand** (the script never does this, so a stolen deploy key can't add an app):

1. Add the app to `sources.json`:
   `{"local": "apps/<app-id>.json", "bundleIdentifiers": ["com.pbordjadze.<name>"]}`.
   `<app-id>` is lowercase letters, digits and dashes.
2. Write a manifest next to the app's CI (the parts that don't change per build):

       {"name": "MyApp", "bundleIdentifier": "com.pbordjadze.myapp", "subtitle": "…",
        "localizedDescription": "…", "tintColor": "#7B3F7E", "category": "utilities",
        "minOSVersion": "18.0"}

   Allowed keys: `name`, `bundleIdentifier`, `developerName`, `subtitle`, `localizedDescription`,
   `tintColor`, `category`, `minOSVersion`, `ipaName`. No URLs: they're generated.
3. Give the CI a deploy key for this repo (write access, this repo only; the key stays on the
   Mac, never in a repo) and point git at it, e.g.
   `GIT_SSH_COMMAND='ssh -i ~/.ssh/sidestore_deploy -o IdentitiesOnly=yes'`.

**Every build:**

    git clone https://github.com/pbordjadze/sidestore.git && cd sidestore
    python3 scripts/publish_build.py --app-id myapp --version 1.2.3 --build 57 \
        --ipa MyApp.ipa --manifest myapp.manifest.json --notes "what changed" --icon icon.png

The Mac's CI passes `--version 1.0.$BUILD`, the release notes, and a manifest with
`"minOSVersion": "26.0"`. Re-running the same build with the same inputs changes nothing.

`--icon` (a PNG, becomes `icons/<app-id>.png`) is needed the first time and optional after.
`--build` must equal the IPA's `CFBundleVersion`, and the IPA's `CFBundleIdentifier` must be the
bundle id `sources.json` allows; privacy usage descriptions are read from its Info.plist.
Release notes are public, so don't put anything private in `--notes`. Fetches use the public
https URL and pushes use `git@github.com:pbordjadze/sidestore.git` (override with `--remote` /
`--push-remote`, or `SIDESTORE_FETCH_URL` / `SIDESTORE_PUSH_URL`).

Several apps can publish at once. `main` is only ever fast-forwarded, and `builds` is pushed
with `--force-with-lease` against the commit that run read; if either push loses a race, the
script fetches again and redoes it (8 attempts). It only touches its own app's entry, icon and
`builds` directory. Every entry it writes goes through `build_source.py`'s own checks (bundle id
allow-list, every URL on `pbordjadze`'s GitHub).

Tests (a local bare repo stands in for GitHub): `python3 -m unittest discover -s tests`.
