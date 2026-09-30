{{/*
Read one environment fact, and fail the render, naming the key, if it is
missing or empty (ADR-0017 §8). Called as:
  include "system.env" (list $.Values "projectID")
*/}}
{{- define "system.env" -}}
{{- $values := index . 0 -}}
{{- $key := index . 1 -}}
{{- $env := $values.environment | default dict -}}
{{- required (printf "environment.%s is missing or empty. Every key in platform-config/environments/<env>.yaml is required (ADR-0017 §8)." $key) (get $env $key) -}}
{{- end -}}

{{/*
The System's tier. The tenant file may leave it out; standard is the default.
The default lives here, not in the schema: Helm does not apply a schema's
defaults (ADR-0017 §6).
*/}}
{{- define "system.tier" -}}
{{- .Values.spec.tier | default "standard" -}}
{{- end -}}

{{/*
The metadata spine, on every Kubernetes object this chart creates. It is also
what Kyverno's tenant-namespace rules match on (the system label) and what
ADR-0015's orphan runbook queries.
*/}}
{{- define "system.labels" -}}
platform.thecloudgeek.io/system: {{ .Values.metadata.name | quote }}
platform.thecloudgeek.io/team: {{ .Values.spec.owner.team | quote }}
platform.thecloudgeek.io/tier: {{ include "system.tier" . | quote }}
platform.thecloudgeek.io/security-tier: {{ .Values.spec.securityTier | quote }}
{{- end -}}

{{/*
The three annotations every Config Connector object carries from its first
apply (ADR-0017 §8). See charts/claims/templates/_helpers.tpl for why each one
is there. Called with the project ID.
*/}}
{{- define "system.cnrmAnnotations" -}}
cnrm.cloud.google.com/state-into-spec: absent
cnrm.cloud.google.com/management-conflict-prevention-policy: none
cnrm.cloud.google.com/project-id: {{ . | quote }}
{{- end -}}

{{/*
The retry block every platform-rendered Application carries: a backstop for
transient failures, never the ordering mechanism.
*/}}
{{- define "system.syncPolicy" -}}
automated:
  prune: true
  selfHeal: true
syncOptions:
  - ServerSideApply=true
retry:
  limit: 10
  backoff:
    duration: 15s
    factor: 2
    maxDuration: 5m
{{- end -}}
