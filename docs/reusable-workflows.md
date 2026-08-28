<!--
Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
SPDX-License-Identifier: BSD-3-Clause
-->
# Reusable RPM packaging workflows

This repo hosts the shared build tooling and reusable GitHub Actions workflows
for RPM packaging repositories created from
[`pkg-rpm-template`](https://github.com/qualcomm-linux/pkg-rpm-template).

A packaging repo (`pkg-rpm-*`) holds **one `*.spec` file** and a dist-git
**`sources`** pointer file at its root; the source tarball is *not* committed.
These workflows turn that into built (and, on release, published) RPMs.

## Components

| Path | Role |
|---|---|
| [`scripts/build-rpm.sh`](../scripts/build-rpm.sh) | Runs the prebuilt `rpm-builder` container over a bind-mounted workspace (builds for the runner's host architecture). |
| [`scripts/build-in-container.sh`](../scripts/build-in-container.sh) | The per-package build that runs *inside* the container: `dnf builddep` + `rpmbuild -ba`. |
| [`docker/Dockerfile.rpm-builder`](../docker/Dockerfile.rpm-builder) | The `rpm-builder` toolchain image, published to GHCR by [`publish-rpm-builder.yml`](../.github/workflows/publish-rpm-builder.yml). |
| [`scripts/resolve-sources.sh`](../scripts/resolve-sources.sh) | dist-git `sources` resolver: cache lookup → upstream fallback → checksum verify → cache-back. |
| [`.github/actions/rpm-artifactory-upload`](../.github/actions/rpm-artifactory-upload/action.yml) | Composite action that uploads RPMs (and source tarballs) to JFrog Artifactory. |
| [`.github/workflows/pkg-build-reusable-workflow.yml`](../.github/workflows/pkg-build-reusable-workflow.yml) | `workflow_call` build workflow. |
| [`.github/workflows/pkg-release-reusable-workflow.yml`](../.github/workflows/pkg-release-reusable-workflow.yml) | `workflow_call` release (build + publish) workflow. |

## The `sources` / lookaside cache model

This follows the Fedora/CentOS
[dist-git](https://github.com/release-engineering/dist-git) model. Each line of
`sources` is the BSD `shaNsum --tag` format:

```
SHA512 (mypackage-1.0.tar.gz) = 3a7bd3e2360a3d29...
```

`resolve-sources.sh` processes each entry:

1. **Cache lookup.** Compute the lookaside path
   (default `{filename}/{hashtype}/{hash}/{filename}`)
   under `--cache-base-url` and `HEAD`-query it.
2. **Cache hit** → download the tarball from the cache.
   **Cache miss** → expand the spec (`rpmspec -P`), find the `SourceN:` URL whose
   basename matches `filename`, and download from upstream.
3. **Verify** the staged tarball against the checksum in `sources`; fail on
   mismatch (whether from cache or upstream).
4. **Cache-back** (release builds only, `--emit-cache-uploads`): record
   upstream-fetched tarballs so the caller uploads them to
   `<target-repo>/sources/<lookaside-path>` for future builds.

This means a maintainer only ever edits `sources` (and the spec version) when
bumping versions; the first release build populates the cache automatically.

## `pkg-build-reusable-workflow.yml`

Build the RPM(s). Used by the PR workflow and by the release workflow.

**Key inputs:** `qcom-rpm-utils-ref`, `pkg-ref`, `publish-target`,
`cache-base-url` (optional override), `cache-path-template`, `builder-image`,
`extra-repo`, `release`, `server-url`, `target-repo`, `distro`,
`distro-version`, `channel`. **Secrets:** `ARTIFACTORY_ACCESS_TOKEN` and/or
`QSC_API_KEY`, plus the `PROD_*` pair (only needed when `release: true`, for
source cache-back — see [Authentication](#authentication)). **Outputs:**
`artifact-name`, `pkg-name`, `pkg-version`, `target-repo`.

`distro`/`distro-version`/`channel` exist here as well as on the release
workflow because the default extra dnf repo is built from them.

Caller example (PR build — everything else derives):

```yaml
jobs:
  build:
    uses: qualcomm-linux/qcom-rpm-utils/.github/workflows/pkg-build-reusable-workflow.yml@main
    with:
      qcom-rpm-utils-ref: main
```

## `pkg-release-reusable-workflow.yml`

Build then publish to Artifactory. Prod publishes run in the
**`pkg-release-approval`** environment, so a maintainer must approve the run
before anything is uploaded. Staging publishes are **not** gated and use no
environment at all — the gate is a separate `approve-prod` job that only exists
when `publish-target` is `prod`.

**Key inputs:** `qcom-rpm-utils-ref`, `publish-target` (`staging` default, or
`prod`), `cache-base-url` (optional override), `server-url`, `target-repo`
(override; normally derived), `distro` (default `centos`), `distro-version`
(default `10`), `channel` (default `os`).
**Secrets:** for staging, `ARTIFACTORY_ACCESS_TOKEN` and/or `QSC_API_KEY`; for
prod, `PROD_ARTIFACTORY_ACCESS_TOKEN` and/or `PROD_QSC_API_KEY` (**at least one
of the pair for the selected target is required** — see
[Authentication](#authentication)).

## Staging and prod

`publish-target` selects the environment. One proxy serves both, so only the
repo differs — and from that repo both read URLs are derived.

| | `staging` | `prod` |
|---|---|---|
| Target repo | `qsc-rpm-releases-stage` | `qsc-rpm-releases` |
| Read access | private, token required | **public, no token** |
| Approval gate | none (no environment) | `pkg-release-approval` |
| Credentials | `ARTIFACTORY_ACCESS_TOKEN` / `QSC_API_KEY` | `PROD_ARTIFACTORY_ACCESS_TOKEN` / `PROD_QSC_API_KEY` |


Because prod is publicly readable, a build with `publish-target: prod` resolves
`BuildRequires` from published RPMs **without** a token.

## Where artifacts are published

`target-repo` is derived from `publish-target` and is the root that **both** the
RPM upload and the source cache-back hang off — source tarballs are cached back
to `<target-repo>/sources/`.

RPMs are published into a standard YUM tree, split by architecture:

```
<target-repo>/<distro>/<distro-version>/<channel>/
├── aarch64/Packages/     <- binary RPMs
├── noarch/Packages/
├── x86_64/Packages/
└── SRPMS/Packages/       <- source RPMs (*.src.rpm)
```

With the defaults that is `<repo>/centos/10/os/<arch>/Packages/`. Architecture is
read from each filename, and `*.src.rpm` is routed to `SRPMS/` rather than an
`src/` directory.


`yumRootDepth` is a single repo-wide setting, so every tree in the repo must
publish at the same nesting level. The layout above is deliberately uniform —
`SRPMS/` sits at the same depth as the arch directories, so one depth value
indexes both.

## Preventing an overwrite

Before uploading anything, the publish step lists every RPM's final path and
checks Artifactory for it. If any already exists the release fails and **nothing
is uploaded** — the check is a pre-pass, not per-file, so a version can never be
left half-published.

Matching is on the exact filename, so `1.0.2-1` → `1.0.2-2` (a release bump) is
allowed; only a path collision that would overwrite is blocked.

Source tarballs are exempt: they are content-addressed, so re-uploading one
writes identical bytes to the identical path.

Caller example:

```yaml
on:
  workflow_dispatch:
    inputs:
      publish-target:
        description: "Artifactory environment to publish to"
        type: choice
        options: [staging, prod]
        default: staging

jobs:
  release:
    uses: qualcomm-linux/qcom-rpm-utils/.github/workflows/pkg-release-reusable-workflow.yml@main
    with:
      qcom-rpm-utils-ref: main
      publish-target: ${{ inputs.publish-target }}
    secrets:
      QSC_API_KEY: ${{ secrets.QSC_API_KEY }}
      ARTIFACTORY_ACCESS_TOKEN: ${{ secrets.RPM_ARTIFACTORY_ACCESS_TOKEN }}
      PROD_QSC_API_KEY: ${{ secrets.PROD_QSC_API_KEY }}
      PROD_ARTIFACTORY_ACCESS_TOKEN: ${{ secrets.PROD_RPM_ARTIFACTORY_ACCESS_TOKEN }}
```

All four secrets are forwarded unconditionally; the composite action selects the
pair for `publish-target` in shell.

## Authentication

Publishing needs Artifactory credentials.
`publish-target` picks the pair, then within that pair the composite action
resolves in this order:

1. **QSC API key** (`QSC_API_KEY` / `PROD_QSC_API_KEY`) — if set, exchanged for
   a short-lived Artifactory access token via the QSC token API. **Takes
   precedence** over the pre-generated token.
2. **Access token** (`ARTIFACTORY_ACCESS_TOKEN` /
   `PROD_ARTIFACTORY_ACCESS_TOKEN`) — used when the QSC key is not set.
3. If **neither** is set for the selected target, the release fails up front, in
   a validation job, before anything is built.

The token needs **read** as well as write on the target repo — the
overwrite guard has to be able to query what is already published.

## Required configuration (in the calling repo)

| Name | Kind | Purpose |
|---|---|---|
| `ARTIFACTORY_ACCESS_TOKEN` | Actions **secret** | Staging access token, for publishing / cache-back. Needs read **and** write. |
| `QSC_API_KEY` | Actions **secret** | Staging QSC API key; takes precedence over `ARTIFACTORY_ACCESS_TOKEN` when set. |
| `PROD_ARTIFACTORY_ACCESS_TOKEN` | Actions **secret** | Prod access token. Only needed by repos that publish to prod. |
| `PROD_QSC_API_KEY` | Actions **secret** | Prod QSC API key; takes precedence over `PROD_ARTIFACTORY_ACCESS_TOKEN` when set. |
| `pkg-release-approval` | Environment | Approval gate for **prod** publishes only. Add required reviewers, or the gate is a no-op. Staging needs no environment. |


Each target repo also needs `yumRootDepth: 4` on the Artifactory side.
