{{/*
Read one environment fact, and fail the render, naming the key, if it is
missing or empty (ADR-0017 §8). Called as:
  include "route.env" (list $.Values "externalZone")
The schema already requires every key; this is the second of the two gates
ADR-0017 §8 names, and the one that runs if the schema is ever loosened.
*/}}
{{- define "route.env" -}}
{{- $values := index . 0 -}}
{{- $key := index . 1 -}}
{{- $env := $values.environment | default dict -}}
{{- required (printf "environment.%s is missing or empty. Every key in platform-config/environments/<env>.yaml is required (ADR-0017 §8)." $key) (get $env $key) -}}
{{- end -}}

{{/*
The labels every object of one route carries. The system label is the one that
matters: it is what `require-system-label` compares with the namespace's name
(ADR-0021 §9), and what ADR-0015's orphan runbook queries. The tenant's team,
tier and security tier are not here: the edge ApplicationSets read a route
file, not the tenant file, so those three are not known to this chart
(ADR-0021 §3 says "the System label").
*/}}
{{- define "route.labels" -}}
platform.thecloudgeek.io/system: {{ .Values.platform.system | quote }}
platform.thecloudgeek.io/route: {{ .Values.platform.route | quote }}
platform.thecloudgeek.io/exposure: {{ .Values.platform.exposure | quote }}
{{- end -}}
