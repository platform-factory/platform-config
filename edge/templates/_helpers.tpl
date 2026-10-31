{{/*
Read one environment fact, and fail the render, naming the key, if it is
missing or empty (ADR-0017 §8). Called as:
  include "edge.env" (list $.Values "externalAddress")
*/}}
{{- define "edge.env" -}}
{{- $values := index . 0 -}}
{{- $key := index . 1 -}}
{{- $env := $values.environment | default dict -}}
{{- required (printf "environment.%s is missing or empty. Every key in platform-config/environments/<env>.yaml is required (ADR-0017 §8)." $key) (get $env $key) -}}
{{- end -}}
