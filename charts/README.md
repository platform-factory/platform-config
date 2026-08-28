# Vendored charts

Charts here are copied from upstream, unmodified, and rendered by Argo CD
straight from this repo. They are not fetched from a chart repository at
sync time.

Why: the cluster's one allowed internet egress is Argo CD's git traffic to
GitHub (C-23, ADR-0010). A chart repository is a second internet endpoint
the repo-server would reach over Cloud NAT. Vendoring keeps the pinhole
singular, and makes a chart bump a reviewable PR — the same path every other
platform change takes. (Crossplane does not publish its chart as an OCI
artifact on a registry the Artifact Registry remotes front; if that changes,
an OCI source through the `ghcr-io` remote is the alternative.)

| Chart | Version | Source | sha256 of the tarball | Vendored |
|---|---|---|---|---|
| `crossplane` | 2.3.5 (app v2.3.5) | https://charts.crossplane.io/stable/crossplane-2.3.5.tgz | `ad9b5005c9b9885ed7c7db712c1fa78643892782d5b3120aa6e101567d3fb698` | 2026-08-27 |

## Upgrading a chart

1. Download the new tarball and record its sha256.
2. Delete the chart directory and extract the tarball in its place. Never
   edit files inside a vendored chart — overrides belong in the owning
   Application's `helm.valuesObject`, where the diff is visible.
3. Update the table above and open a PR. The diff is the upgrade review.
