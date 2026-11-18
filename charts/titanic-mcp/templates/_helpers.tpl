{{- define "tmcp.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "tmcp.fullname" -}}{{ default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "tmcp.labels" -}}
app.kubernetes.io/name: {{ include "tmcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "tmcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tmcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "tmcp.secretName" -}}{{ default (printf "%s-secrets" (include "tmcp.fullname" .)) .Values.secrets.existingSecret }}{{- end -}}
