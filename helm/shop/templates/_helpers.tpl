{{/* Common labels on every object. */}}
{{- define "shop.labels" -}}
app.kubernetes.io/part-of: shop
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Labels that select the pods of one component (must never change). */}}
{{- define "shop.selector" -}}
app.kubernetes.io/name: shop
app.kubernetes.io/component: {{ . }}
{{- end -}}

{{/* Full image reference for a service. */}}
{{- define "shop.image" -}}
{{- $img := .root.Values.image -}}
{{- if $img.registry -}}
{{ $img.registry }}/{{ $img.repositoryPrefix }}/{{ .name }}:{{ $img.tag }}
{{- else -}}
{{ $img.repositoryPrefix }}/{{ .name }}:{{ $img.tag }}
{{- end -}}
{{- end -}}
