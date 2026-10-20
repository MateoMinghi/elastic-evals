{{- define "ee.fullname" -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "ee.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "ee.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "ee.collectorImage" -}}{{ .Values.collectorImage.repository }}:{{ .Values.collectorImage.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "ee.env" -}}
- name: EE_OPENSEARCH_URL
  value: {{ .Values.opensearch.url | quote }}
- name: EE_INDEX_PREFIX
  value: {{ .Values.opensearch.indexPrefix | quote }}
- name: EE_KAFKA_BROKERS
  value: {{ .Values.kafka.brokers | quote }}
- name: EE_KAFKA_TOPIC
  value: {{ .Values.kafka.topic | quote }}
- name: EE_POSTGRES_DSN
  valueFrom: { secretKeyRef: { name: {{ .Values.postgres.existingSecret }}, key: dsn } }
- name: EE_ARTIFACTS
  value: {{ .Values.artifacts.uri | default "file:///artifacts" | quote }}
{{- with .Values.uiUrl }}
- name: EE_UI_URL
  value: {{ . | quote }}
{{- end }}
{{- if .Values.hosted.existingSecret }}
- name: OPENROUTER_API_KEY
  valueFrom: { secretKeyRef: { name: {{ .Values.hosted.existingSecret }}, key: OPENROUTER_API_KEY } }
{{- end }}
{{- end -}}
{{- define "ee.artifactVolume" -}}
{{- if not .Values.artifacts.uri }}
volumes:
  - name: artifacts
    persistentVolumeClaim: { claimName: {{ include "ee.fullname" . }}-artifacts }
{{- end }}
{{- end -}}
{{- define "ee.artifactMount" -}}
{{- if not .Values.artifacts.uri }}
volumeMounts:
  - { name: artifacts, mountPath: /artifacts }
{{- end }}
{{- end -}}
