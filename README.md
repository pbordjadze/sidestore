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
  here changes and every 30 minutes, and commits `source.json` if it changed. A
  remote source that can't be fetched leaves `source.json` as it was.
- Each app keeps only its five newest builds.
- `builds` holds IPAs for apps whose own repos are private (served from
  raw.githubusercontent.com). It's force-pushed, so it never grows.

GitHub disables scheduled workflows in public repos after 60 days without
activity. Any app build pushed here counts as activity; if it does get disabled,
re-enable it under Actions, or run it by hand (workflow_dispatch).

## Adding an app

- **Public repo with its own source:** add its `source.json` URL to `sources.json`.
- **Private repo:** something with push access has to put the IPA where SideStore
  can download it and write `apps/<name>.json`. Treasurr does it from the seedbox
  with a deploy key (its `ci/relay_sidestore.py`): IPAs live on the `builds`
  branch, one force-pushed commit with the newest five builds.
