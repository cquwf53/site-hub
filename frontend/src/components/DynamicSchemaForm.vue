<script setup lang="ts">
import { computed } from 'vue'
import type { JsonSchema, UiField } from '@/api/types'

const props = withDefaults(defineProps<{
  schema?: JsonSchema
  uiSchema?: Record<string, UiField>
  modelValue: Record<string, unknown>
  secretFields?: string[]
  configuredSecretFields?: string[]
  clearSecrets?: string[]
  disabled?: boolean
}>(), { uiSchema: () => ({}), secretFields: () => [], configuredSecretFields: () => [], clearSecrets: () => [] })

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, unknown>]
  'update:clearSecrets': [value: string[]]
}>()

const fields = computed(() => Object.entries(props.schema?.properties || {})
  .filter(([key]) => !props.uiSchema[key]?.hidden)
  .sort(([a], [b]) => (props.uiSchema[a]?.order ?? 999) - (props.uiSchema[b]?.order ?? 999)))

function ui(key: string) { return props.uiSchema[key] || {} }
function required(key: string) { return props.schema?.required?.includes(key) || false }
function isSecret(key: string) {
  return props.secretFields.includes(key) || props.configuredSecretFields.includes(key)
}
function configured(key: string) { return props.configuredSecretFields.includes(key) && !props.clearSecrets.includes(key) }
function displayValue(key: string, field: JsonSchema) {
  const value = props.modelValue[key] ?? field.default
  if (field.type === 'array' && Array.isArray(value)) return value.join(', ')
  if (typeof value === 'string' || typeof value === 'number') return value
  return value === null || value === undefined ? '' : String(value)
}

function update(key: string, value: unknown) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function updateFromEvent(key: string, event: Event, type?: JsonSchema['type']) {
  const value = (event.target as HTMLInputElement | HTMLSelectElement).value
  if (type === 'number' || type === 'integer') update(key, value === '' ? null : Number(value))
  else if (type === 'array') update(key, value.split(',').map((item) => item.trim()).filter(Boolean))
  else update(key, value)
}

function toggleClear(key: string) {
  const next = new Set(props.clearSecrets)
  if (next.has(key)) next.delete(key)
  else {
    if (configured(key) && !window.confirm(`确认清除“${ui(key).label || key}”？保存后该密钥将无法恢复。`)) return
    next.add(key)
  }
  emit('update:clearSecrets', [...next])
  if (next.has(key)) {
    const value = { ...props.modelValue }
    delete value[key]
    emit('update:modelValue', value)
  }
}

function nestedNames(names: string[], key: string) {
  return names.filter((name) => name.startsWith(`${key}.`)).map((name) => name.slice(key.length + 1))
}

function updateNestedClear(key: string, names: string[]) {
  const siblings = props.clearSecrets.filter((name) => !name.startsWith(`${key}.`))
  emit('update:clearSecrets', [...siblings, ...names.map((name) => `${key}.${name}`)])
}
</script>

<template>
  <div v-if="!fields.length" class="schema-empty">此适配器无需额外配置。</div>
  <div v-else class="form-grid schema-form">
    <div v-for="[key, field] in fields" :key="key" class="field" :class="{ 'span-2': ui(key).widget === 'textarea' || field.type === 'object' }">
      <template v-if="field.type === 'boolean' || ui(key).widget === 'switch'">
        <div class="switch-row">
          <div><div class="field-label">{{ ui(key).label || field.title || key }}<span v-if="required(key)"> *</span></div><p v-if="ui(key).help || field.description" class="field-help">{{ ui(key).help || field.description }}</p></div>
          <button type="button" class="switch" :class="{ on: Boolean(modelValue[key]) }" :disabled="disabled" :aria-pressed="Boolean(modelValue[key])" @click="update(key, !modelValue[key])" />
        </div>
      </template>
      <template v-else-if="isSecret(key)">
        <label :for="`schema-${key}`">{{ ui(key).label || field.title || key }}<span v-if="required(key)"> *</span></label>
        <div class="secret-control">
          <input :id="`schema-${key}`" type="password" autocomplete="new-password" :disabled="disabled || clearSecrets.includes(key)" :placeholder="configured(key) ? '已配置；留空表示不修改' : '输入密钥'" @input="updateFromEvent(key, $event)" />
          <button type="button" class="text-button" :disabled="disabled" @click="toggleClear(key)">{{ clearSecrets.includes(key) ? '撤销清除' : '清除' }}</button>
        </div>
        <p class="field-help">{{ configured(key) ? '密钥已安全保存，页面不会回显。' : clearSecrets.includes(key) ? '保存后将清除此密钥。' : ui(key).help || field.description }}</p>
      </template>
      <template v-else-if="field.enum">
        <label :for="`schema-${key}`">{{ ui(key).label || field.title || key }}<span v-if="required(key)"> *</span></label>
        <select :id="`schema-${key}`" :value="modelValue[key] ?? field.default ?? ''" :disabled="disabled" @change="updateFromEvent(key, $event, field.type)">
          <option value="" disabled>请选择</option><option v-for="option in field.enum" :key="String(option)" :value="typeof option === 'boolean' ? String(option) : option">{{ option }}</option>
        </select>
        <p v-if="ui(key).help || field.description" class="field-help">{{ ui(key).help || field.description }}</p>
      </template>
      <template v-else-if="field.type === 'object'">
        <div class="nested-schema">
          <div class="field-label">{{ ui(key).label || field.title || key }}</div>
          <DynamicSchemaForm
            :schema="field"
            :ui-schema="uiSchema"
            :model-value="(modelValue[key] as Record<string, unknown>) || {}"
            :secret-fields="nestedNames(secretFields, key)"
            :configured-secret-fields="nestedNames(configuredSecretFields, key)"
            :clear-secrets="nestedNames(clearSecrets, key)"
            :disabled="disabled"
            @update:model-value="update(key, $event)"
            @update:clear-secrets="updateNestedClear(key, $event)"
          />
        </div>
      </template>
      <template v-else>
        <label :for="`schema-${key}`">{{ ui(key).label || field.title || key }}<span v-if="required(key)"> *</span></label>
        <textarea v-if="ui(key).widget === 'textarea'" :id="`schema-${key}`" :value="displayValue(key, field)" :disabled="disabled" :placeholder="ui(key).placeholder" @input="updateFromEvent(key, $event, field.type)" />
        <input v-else :id="`schema-${key}`" :type="field.type === 'number' || field.type === 'integer' ? 'number' : field.format === 'uri' ? 'url' : 'text'" :min="field.minimum" :max="field.maximum" :minlength="field.minLength" :maxlength="field.maxLength" :value="displayValue(key, field)" :disabled="disabled" :placeholder="ui(key).placeholder" @input="updateFromEvent(key, $event, field.type)" />
        <p v-if="ui(key).help || field.description" class="field-help">{{ ui(key).help || field.description }}</p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.schema-empty { padding: 18px; border: 1px dashed var(--line-strong); border-radius: 9px; color: var(--muted); text-align: center; font-size: 13px; }
.secret-control { display: flex; align-items: center; gap: 10px; }
.secret-control input { flex: 1; }
.nested-schema { padding: 15px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-soft); }
.nested-schema > .field-label { margin-bottom: 13px; }
</style>
