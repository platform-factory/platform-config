# platform-config

This repo is the Argo CD root (an app-of-apps: one Argo CD Application that points at other Applications) for the Platform Factory. It installs and configures the shared platform layer — Crossplane, Kyverno, External Secrets Operator (ESO), external-dns, the Gateway, and the XRDs/Compositions (Crossplane's building blocks for self-service infrastructure) — and defines the golden paths teams build on.

## Part of the Platform Factory

This repo is one of seven that make up the reference implementation of the
**Platform Factory** pattern. The design seed — pattern docs, ADRs, and the
build plan — lives at [https://github.com/thecloudgeek/platform-factory](https://github.com/thecloudgeek/platform-factory).

This repo is built out in **M1**.

## Status

**Status:** scaffold — build in progress, following the pre-registered build plan in the design seed repo.
