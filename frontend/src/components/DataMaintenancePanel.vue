<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import AsyncState from './AsyncState.vue'
import type { MaintenanceSettings, MaintenanceStatus } from '@/api/types'
import { api, apiErrorMessage } from '@/api/client'
import { formatDate } from '@/utils/format'
import { useAppStore } from '@/stores/app'


const emit = defineEmits<{ changed: [] }>()
const app = useAppStore()
const settings = ref<MaintenanceSettings>({ automatic_backup_enabled: true, backup_interval_hours: 24, retention_enabled: false, backup_keep_count: 7, log_retention_days: 30, screenshot_retention_days: 30, price_history_retention_days: 365 })
const status = ref<MaintenanceStatus | null>(null)
const loading = ref(true); const error = ref(''); const busy = ref(false)
let retentionWasEnabled = false
let timer = 0
async function refreshStatus() {
  try { status.value = await api.get<MaintenanceStatus>('/maintenance/status') }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
async function load() {
  loading.value = true; error.value = ''
  try {
    const [policy, current] = await Promise.all([api.get<MaintenanceSettings>('/settings/maintenance'), api.get<MaintenanceStatus>('/maintenance/status')])
    settings.value = policy; status.value = current; retentionWasEnabled = policy.retention_enabled
  } catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
async function save() {
  if (settings.value.retention_enabled && !retentionWasEnabled && !window.confirm('启用后将按保留策略清理过期备份、日志、截图和价格记录。确认启用自动清理？')) return
  busy.value = true
  try {
    settings.value = await api.put<MaintenanceSettings>('/settings/maintenance', settings.value)
    retentionWasEnabled = settings.value.retention_enabled
    app.toast('数据维护策略已保存', 'success'); await refreshStatus()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { busy.value = false }
}
async function run() {
  busy.value = true
  try {
    status.value = await api.post<MaintenanceStatus>('/maintenance/run')
    if (status.value.last_error) app.toast(status.value.last_error, 'error')
    else app.toast('数据维护已完成', 'success')
    emit('changed')
  } catch (value) { app.toast(apiErrorMessage(value), 'error'); await refreshStatus() }
  finally { busy.value = false }
}
onMounted(() => { void load(); timer = window.setInterval(() => { if (status.value?.running) void refreshStatus() }, 5000) })
onUnmounted(() => window.clearInterval(timer))
</script>
<template>
  <section class="card maintenance-panel">
    <div class="card-header"><div><h2>自动备份与数据保留</h2><p class="subtle-text">每次维护先备份并校验，再执行已启用的清理策略。</p></div><button class="button secondary small" :disabled="busy || status?.running || loading" @click="run">{{ busy || status?.running ? '维护中…' : '立即维护' }}</button></div>
    <AsyncState :loading="loading" :error="error" @retry="load">
      <div v-if="status" class="card-body maintenance-status" aria-live="polite">
        <p>下次维护：{{ formatDate(status.next_run_at) }} · 最近成功：{{ formatDate(status.last_success_at) }}</p>
        <p v-if="status.latest_verification">最近校验：{{ formatDate(status.latest_verification.verified_at) }} · {{ status.latest_verification.integrity_check }} · 备份 {{ status.latest_verification.backup_id }}</p>
        <p v-if="status.last_error" role="alert">最近失败：{{ status.last_error }}</p>
        <p v-if="status.last_cleanup">最近清理：备份 {{ status.last_cleanup.backups }}，日志 {{ status.last_cleanup.logs }}，截图 {{ status.last_cleanup.screenshots }}，价格记录 {{ status.last_cleanup.price_history }}</p>
      </div>
      <form class="card-body form-grid" @submit.prevent="save">
        <label class="span-2"><input v-model="settings.automatic_backup_enabled" type="checkbox" /> 自动创建并校验数据库备份</label>
        <div class="field"><label for="backup-hours">备份间隔（小时）</label><input id="backup-hours" v-model.number="settings.backup_interval_hours" type="number" min="1" max="720" required /></div>
        <div class="field"><label for="backup-keep">保留最近备份数</label><input id="backup-keep" v-model.number="settings.backup_keep_count" type="number" min="1" max="365" required /></div>
        <label class="span-2"><input v-model="settings.retention_enabled" type="checkbox" /> 启用过期数据清理</label>
        <p class="field-help span-2">默认关闭。开启后删除超过保留期限的数据；浏览器登录 Profile 不在清理范围内。</p>
        <div class="field"><label for="log-days">日志保留天数</label><input id="log-days" v-model.number="settings.log_retention_days" type="number" min="1" max="3650" required /></div>
        <div class="field"><label for="screenshot-days">截图保留天数</label><input id="screenshot-days" v-model.number="settings.screenshot_retention_days" type="number" min="1" max="3650" required /></div>
        <div class="field"><label for="price-history-days">价格历史保留天数</label><input id="price-history-days" v-model.number="settings.price_history_retention_days" type="number" min="1" max="3650" required /></div>
        <div class="span-2"><button class="button" :disabled="busy || status?.running">保存维护策略</button></div>
      </form>
    </AsyncState>
  </section>
</template>
<style scoped>.maintenance-status { padding-bottom: 0; }.maintenance-status p { font-size: 12px; overflow-wrap: anywhere; }.maintenance-status [role=alert] { color: var(--negative); }</style>
