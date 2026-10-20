{{/*
Read one environment fact, and fail the render, naming the key, if it is
missing or empty (ADR-0017 §8). Called as:
  include "claims.env" (list $.Values "projectID")
The schema already requires every key; this is the second of the two gates
ADR-0017 §8 names, and the one that runs if the schema is ever loosened.
*/}}
{{- define "claims.env" -}}
{{- $values := index . 0 -}}
{{- $key := index . 1 -}}
{{- $env := $values.environment | default dict -}}
{{- required (printf "environment.%s is missing or empty. Every key in platform-config/environments/<env>.yaml is required (ADR-0017 §8)." $key) (get $env $key) -}}
{{- end -}}

{{/*
The three annotations every Config Connector object carries from its first
apply (ADR-0017 §8), stamped in one place so no object can miss one:
  - state-into-spec: absent. Otherwise Config Connector copies the cloud's
    values into spec, Argo CD reads that as drift, and the annotation can
    never be changed on a live object.
  - management-conflict-prevention-policy: none. Its forty-minute lease would
    stall every rebuild.
  - project-id: the project every object is created in. The Namespace carries
    it too, as a fallback.
Called with the project ID.
*/}}
{{- define "claims.cnrmAnnotations" -}}
cnrm.cloud.google.com/state-into-spec: absent
cnrm.cloud.google.com/management-conflict-prevention-policy: none
cnrm.cloud.google.com/project-id: {{ . | quote }}
{{- end -}}

{{/*
The labels every object of one claim carries. Called with (list system claim).
*/}}
{{- define "claims.labels" -}}
platform.thecloudgeek.io/system: {{ index . 0 | quote }}
platform.thecloudgeek.io/database: {{ index . 1 | quote }}
{{- end -}}
