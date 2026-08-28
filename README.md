# platform-config

The Argo CD root of the Platform Factory reference implementation: an
app-of-apps (one root Application that creates other Applications) that
installs and configures the shared platform layer. Everything the running
platform is made of arrives through this repo, as a PR. `platform-bootstrap`
(Terraform) stops at installing Argo CD and pointing it here — that is the
"Terraform's last job" boundary (claim C-01).

## How the root finds this repo

`platform-bootstrap`'s `3-argocd` layer installs Argo CD plus a single
`root` Application whose source is this repo, path `apps/`, revision `main`,
automated sync with prune and self-heal. Every YAML file in `apps/` is one
child Application; each child owns one component and syncs it from
elsewhere in this repo.

```
apps/                          one Application per component — the root syncs this directory
├── crossplane.yaml            wave 0: Crossplane core, Helm chart rendered from charts/crossplane
└── crossplane-providers.yaml  wave 1: everything under crossplane/providers/
charts/
└── crossplane/                vendored upstream chart 2.3.5, unmodified (see charts/README.md)
crossplane/
└── providers/                 ImageConfig (mirror rule) + Provider packages, in waves
```

## Order, and why it holds

Argo CD applies sync waves lowest first and waits for a wave to be Healthy
before starting the next. That is used at two levels:

1. **Between components** — `crossplane` (wave 0) before
   `crossplane-providers` (wave 1). Everything under providers is a
   Crossplane CRD type, so it can't be validated until Crossplane core is
   running. This gating only works because `3-argocd` restores Argo CD's
   health check for `Application` resources (removed from the built-ins in
   Argo CD 1.8, and without it a parent counts an unhealthy child as
   Healthy). Each child also carries a retry policy as the backstop.
2. **Inside providers** — `ImageConfig` (wave 0), then `provider-family-gcp`
   (wave 1), then the service providers (wave 2). The mirror rule has to
   exist before the first package pull; the family has to be Healthy before
   the packages that depend on it install. Argo CD's built-in health check
   for `pkg.crossplane.io/Provider` is what makes "Healthy" mean *installed
   and running*, not merely *object created*.

## The image plane

ADR-0010: nodes are private and pull images through Artifact Registry
remote repositories, never from the internet. Two places honor that here:

- **Crossplane core** — `apps/crossplane.yaml` overrides the chart's
  `image.repository` to the `ghcr-io` remote.
- **Packages** — `crossplane/providers/image-config.yaml` is a single
  `ImageConfig` rewriting the `xpkg.crossplane.io` prefix to the `ghcr-io`
  remote. Provider manifests keep their canonical upstream names; the
  rewrite covers them *and* the family dependency they bake in, which
  Crossplane would otherwise resolve straight from the internet.

Crossplane pulls packages itself — from its own pod, not through kubelet —
so it needs its own read access to Artifact Registry. `platform-bootstrap`'s
`0-foundation/iam.tf` grants `artifactregistry.reader` to the
`crossplane-system/crossplane` service account via Workload Identity; no
pull secret exists anywhere.

## Pinned versions

| Component | Version | Verified against | Date |
|---|---|---|---|
| Crossplane | 2.3.5 | `crossplane/crossplane` GitHub releases; `charts.crossplane.io/stable` index | 2026-08-27 |
| provider-upjet-gcp family (`provider-family-gcp`, `-storage`, `-sql`, `-cloudplatform`, `-artifact`, `-dns`) | v3.0.0 | repo release list and v3.0.0 release notes; every package pulled through the `ghcr-io` remote by registry probe | 2026-08-11, 2026-08-27 |

Crossplane 2.4.0 shipped on 2026-08-20; 2.3.5 is the latest patch of the
minor the v3.0.0 providers were built against (`CROSSPLANE_VERSION = 2.3.4`
in their Makefile). Bumping either is a PR here, and the M4 dependency-bump
change class is expected to make exactly this kind of PR.

## Adding a component

1. Put its manifests (or a vendored chart) in a directory named for it.
2. Add one Application in `apps/` pointing at that directory, with a sync
   wave that places it after anything whose CRDs it needs.
3. If it pulls images, route them through an Artifact Registry remote; add
   the upstream to `platform-bootstrap`'s `0-foundation/registry.tf` if no
   existing remote covers it.

## Not here yet

XRDs and Compositions, the `System` XR, Kyverno, External Secrets Operator,
external-dns, and the Gateway are M2/M3 work. `ProviderConfig` — the GCP
identity the providers use to create real resources — arrives with the
first Composition in M2. Nothing in this repo creates cloud resources yet.

## Part of the Platform Factory

This repo is one of seven that make up the reference implementation of the
**Platform Factory** pattern. The design seed — pattern docs, ADRs, and the
build plan — lives at [https://github.com/thecloudgeek/platform-factory](https://github.com/thecloudgeek/platform-factory).

This repo is built out in **M1**.

## Status

**Status:** M1 — app-of-apps authored: Crossplane core plus the GCP provider
family (storage, sql, cloudplatform, artifact, dns), ordered by sync waves,
images and packages routed through Artifact Registry. Not yet exercised on a
fresh cluster: the first live run is C-02 cycle 2, which doubles as the
C-04 test ("fresh cluster reaches all-synced from empty with no manual
ordering"). Results land in the design seed repo's
`docs/build-log/m1-spine.md`.
