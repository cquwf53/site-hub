<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, toRaw } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import DynamicSchemaForm from '@/components/DynamicSchemaForm.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { AdapterManifest, Job, SiteAccount, TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { accountSchedulePayload, SITEHUB_TIMEZONE } from '@/utils/accountSchedule'
import { formatBytes, formatDate } from '@/utils/format'
import { useSiteDetail } from './siteContext'

const { site } = useSiteDetail(); const app = useAppStore(); const tasks = useTaskStore()
const accounts = ref<SiteAccount[]>([]); const manifests = ref<AdapterManifest[]>([]); const loading = ref(true); const error = ref(''); const saving = ref(false)
const editorOpen = ref(false); const editingId = ref<string | null>(null); const deleteTarget = ref<SiteAccount | null>(null)
const form = ref({ label: '', adapter: '', enabled: true, config: {} as Record<string, unknown>, clear_secrets: [] as string[], schedule_mode: 'inherit' as 'inherit' | 'fixed_time' | 'custom_window', fixed_time: '08:30', window_start: '07:30', window_end: '10:00' })
const verificationJobs = ref<Record<string, string>>({})
let refreshTimer = 0
let disposed = false
const verificationTimers = new Map<string, number>()
const activeJobStatuses = new Set(['queued', 'leased', 'running'])
const checkinManifests = computed(() => manifests.value.filter((item) => item.capabilities.includes('checkin')))
const manifest = computed(() => manifests.value.find((item) => item.id === form.value.adapter))
const usesBrowserProfile = (account: SiteAccount) => !['api', 'cookie'].includes(account.adapter || '')
const canEnableAccount = (account: SiteAccount) => account.enabled || account.auth_state === 'ready' || !usesBrowserProfile(account)
const canRunAccount = (account: SiteAccount) => account.enabled && (!usesBrowserProfile(account) || account.auth_state === 'ready')
const isProfileVerifying = (account: SiteAccount) => Boolean(verificationJobs.value[account.id]) || account.profile?.state === 'busy'
const profileStatus = (account: SiteAccount) => verificationJobs.value[account.id] ? 'verifying' : account.profile?.state
const editingAccount = computed(() => accounts.value.find((account) => account.id === editingId.value))
const canEnableForm = computed(() => ['api', 'cookie'].includes(form.value.adapter) || editingAccount.value?.auth_state === 'ready')

async function load(silent = false) {
  if (!site.value) return
  if (!silent) loading.value = true
  error.value = ''
  try { ;[accounts.value, manifests.value] = await Promise.all([api.get<SiteAccount[]>(`/sites/${site.value.id}/accounts`), api.get<AdapterManifest[]>('/adapters')]) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false; scheduleRefresh() }
}
function scheduleRefresh() {
  window.clearTimeout(refreshTimer)
  if (accounts.value.some((account) => ['opening', 'awaiting_user', 'verifying'].includes(account.auth_state) || account.profile?.state === 'busy')) refreshTimer = window.setTimeout(() => void load(true), 3000)
}
function openCreate() {
  editingId.value = null
  const first = checkinManifests.value[0]
  form.value = { label: `账号 ${accounts.value.length + 1}`, adapter: first?.id || '', enabled: false, config: structuredClone(toRaw(first?.defaults || {})), clear_secrets: [], schedule_mode: 'inherit', fixed_time: '08:30', window_start: '07:30', window_end: '10:00' }
  editorOpen.value = true
}
function openEdit(account: SiteAccount) {
  editingId.value = account.id
  const schedule = account.schedule_override
  form.value = { label: account.label, adapter: account.adapter || '', enabled: account.enabled, config: structuredClone(toRaw(account.config || account.config_json || {})), clear_secrets: [], schedule_mode: schedule?.mode || 'inherit', fixed_time: schedule?.mode === 'fixed_time' ? schedule.time : '08:30', window_start: schedule?.mode === 'custom_window' ? schedule.start : '07:30', window_end: schedule?.mode === 'custom_window' ? schedule.end : '10:00' }
  editorOpen.value = true
}
function adapterChanged() {
  const selected = manifests.value.find((item) => item.id === form.value.adapter)
  form.value.config = structuredClone(toRaw(selected?.defaults || {}))
  form.value.clear_secrets = []
  if (!['api', 'cookie'].includes(form.value.adapter)) form.value.enabled = false
}
async function save() {
  if (!site.value || !form.value.label.trim() || !form.value.adapter) return
  if (form.value.enabled && !canEnableForm.value) {
    app.toast('请先验证浏览器 Profile，确认登录有效后再启用自动化', 'error')
    return
  }
  saving.value = true
  try {
    const schedule = accountSchedulePayload(form.value)
    const payload: Record<string, unknown> = { label: form.value.label, enabled: form.value.enabled, config: form.value.config, clear_secrets: form.value.clear_secrets, ...schedule }
    if (!editingId.value) payload.adapter = form.value.adapter
    if (editingId.value) await api.patch<SiteAccount>(`/accounts/${editingId.value}`, payload)
    else await api.post<SiteAccount>(`/sites/${site.value.id}/accounts`, payload)
    editorOpen.value = false; app.toast(editingId.value ? '账号设置已保存' : '签到账号已添加', 'success'); await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}
async function toggleEnabled(account: SiteAccount) {
  if (!canEnableAccount(account)) {
    app.toast('请先验证浏览器 Profile，确认登录有效后再启用自动化', 'error')
    return
  }
  const previous = account.enabled; account.enabled = !previous
  try { await api.patch<SiteAccount>(`/accounts/${account.id}`, { enabled: account.enabled }) }
  catch (value) { account.enabled = previous; app.toast(apiErrorMessage(value), 'error') }
}
async function startLogin(account: SiteAccount) {
  try { const task = await api.post<TaskRef>(`/accounts/${account.id}/login-session`); tasks.track(task, `${site.value?.name} · ${account.label} 登录`); app.taskDrawerOpen = true; account.auth_state = 'opening'; scheduleRefresh(); app.toast('登录窗口正在打开', 'success') }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
async function verifyProfile(account: SiteAccount) {
  const previous = account.profile?.state
  if (account.profile) account.profile.state = 'busy'
  scheduleRefresh()
  try {
    const task = await api.post<TaskRef>(`/accounts/${account.id}/login-session/verify`)
    tasks.track(task, `${site.value?.name} · ${account.label} 验证 Profile`)
    verificationJobs.value = { ...verificationJobs.value, [account.id]: task.jobId }
    void pollProfileVerification(account.id, task.jobId)
    app.taskDrawerOpen = true
    app.toast('Profile 验证任务已创建，不会执行签到', 'success')
  } catch (value) {
    if (account.profile && previous) account.profile.state = previous
    scheduleRefresh()
    app.toast(apiErrorMessage(value), 'error')
  }
}
function scheduleVerificationPoll(accountId: string, jobId: string) {
  if (disposed || verificationJobs.value[accountId] !== jobId) return
  const timer = window.setTimeout(() => void pollProfileVerification(accountId, jobId), 2000)
  verificationTimers.set(accountId, timer)
}
function finishVerificationPoll(accountId: string) {
  const timer = verificationTimers.get(accountId)
  if (timer !== undefined) window.clearTimeout(timer)
  verificationTimers.delete(accountId)
  const next = { ...verificationJobs.value }
  delete next[accountId]
  verificationJobs.value = next
}
async function pollProfileVerification(accountId: string, jobId: string) {
  if (disposed || verificationJobs.value[accountId] !== jobId) return
  try {
    const job = await api.get<Job>(`/jobs/${jobId}`)
    if (activeJobStatuses.has(String(job.status))) {
      scheduleVerificationPoll(accountId, jobId)
      return
    }
    finishVerificationPoll(accountId)
    await load(true)
  } catch {
    scheduleVerificationPoll(accountId, jobId)
  }
}
async function runNow(account: SiteAccount) {
  try { const task = await api.post<TaskRef>(`/accounts/${account.id}/run`); tasks.track(task, `${site.value?.name} · ${account.label} 签到`); app.taskDrawerOpen = true; app.toast('签到任务已创建', 'success') }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
async function remove() {
  if (!deleteTarget.value) return
  saving.value = true
  try { await api.delete<void>(`/accounts/${deleteTarget.value.id}`); deleteTarget.value = null; app.toast('账号已删除', 'success'); await load() }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}
onMounted(load); onUnmounted(() => {
  disposed = true
  window.clearTimeout(refreshTimer)
  verificationTimers.forEach((timer) => window.clearTimeout(timer))
  verificationTimers.clear()
})
</script>

<template>
  <div class="row-between section-title"><div><h2>签到账号</h2><p>同一网站可添加多个独立账号；登录凭据不会在页面回显。</p></div><button class="button" @click="openCreate">＋ 添加账号</button></div>
  <AsyncState :loading="loading" :error="error" :empty="!accounts.length" empty-title="还没有签到账号" empty-text="添加账号后，分别完成登录并设置自动签到。" @retry="load()"><template #empty-action><button class="button" @click="openCreate">添加第一个账号</button></template>
    <div class="account-grid">
      <article v-for="account in accounts" :key="account.id" class="card account-card">
        <div class="row-between"><div class="account-title"><span class="account-avatar">{{ account.label.slice(0, 1).toUpperCase() }}</span><div><h3>{{ account.label }}</h3><p>{{ account.adapter || '签到适配器' }}</p></div></div><button class="switch" :class="{ on: account.enabled }" :disabled="!canEnableAccount(account)" :title="!canEnableAccount(account) ? '请先验证 Profile' : ''" :aria-label="account.enabled ? '停用账号' : '启用账号'" :aria-pressed="account.enabled" @click="toggleEnabled(account)" /></div>
        <div class="account-status"><StatusBadge :status="account.auth_state" /><span v-if="account.configured" class="secret-status">🔒 凭据已配置</span><span v-else class="secret-status warning">凭据未配置</span><span v-if="!canEnableAccount(account)" class="secret-status warning">请先验证 Profile 再启用</span></div>
        <dl><div><dt>上次验证</dt><dd>{{ formatDate(account.last_verified_at || account.profile?.last_verified_at) }}</dd></div><div><dt>密钥字段</dt><dd>{{ account.configured_secret_fields.length ? `${account.configured_secret_fields.length} 项已保存` : '无' }}</dd></div><div><dt>浏览器 Profile</dt><dd v-if="usesBrowserProfile(account) && account.profile" class="profile-summary"><StatusBadge :status="profileStatus(account)" /><span>{{ account.profile.file_count ?? 0 }} 个文件</span></dd><dd v-else-if="usesBrowserProfile(account)">尚未创建</dd><dd v-else><StatusBadge status="ready" label="无需 Profile" /></dd></div><div><dt>Profile 大小</dt><dd>{{ usesBrowserProfile(account) ? formatBytes(account.profile?.total_bytes) : '不适用' }}</dd></div></dl>
        <p v-if="account.profile?.verification_error" class="profile-error">{{ account.profile.verification_error }}</p>
        <div class="account-actions"><button class="button secondary small" @click="openEdit(account)">配置</button><button class="button secondary small" @click="startLogin(account)">{{ account.auth_state === 'ready' ? '重新登录' : '登录' }}</button><button v-if="usesBrowserProfile(account)" class="button secondary small verify-profile" :disabled="isProfileVerifying(account)" @click="verifyProfile(account)">{{ isProfileVerifying(account) ? '验证中…' : '验证 Profile' }}</button><button class="button small run-now" :disabled="!canRunAccount(account)" @click="runNow(account)">立即签到</button><button class="text-button delete-link" @click="deleteTarget = account">删除</button></div>
      </article>
    </div>
  </AsyncState>

  <ModalDialog v-if="editorOpen" :title="editingId ? '配置签到账号' : '添加签到账号'" :busy="saving" wide @close="editorOpen = false">
    <form id="account-editor" class="stack" @submit.prevent="save">
      <div class="form-grid"><div class="field"><label for="account-label">账号名称 *</label><input id="account-label" v-model.trim="form.label" required placeholder="例如：主账号" /></div><div class="field"><label for="account-adapter">适配器 *</label><select id="account-adapter" v-model="form.adapter" required :disabled="Boolean(editingId)" @change="adapterChanged"><option value="" disabled>选择适配器</option><option v-for="item in checkinManifests" :key="item.id" :value="item.id">{{ item.name || item.id }} · v{{ item.version }}</option></select><p v-if="!editingId && !checkinManifests.length" class="field-help">签到 Worker 尚未提供可用适配器，请等待 Worker 恢复后重试。</p></div></div>
      <div v-if="manifest" class="adapter-note"><strong>{{ manifest.name || manifest.id }}</strong><span>{{ manifest.description || `适配器版本 ${manifest.version}` }}</span></div>
      <DynamicSchemaForm v-if="manifest" v-model="form.config" v-model:clear-secrets="form.clear_secrets" :schema="manifest.account_config_schema" :ui-schema="manifest.ui_schema" :secret-fields="manifest.secret_fields" :configured-secret-fields="editingId ? accounts.find((account) => account.id === editingId)?.configured_secret_fields : []" :disabled="saving" />
      <fieldset class="schedule-fields"><legend>执行时间</legend><div class="schedule-modes"><label><input v-model="form.schedule_mode" type="radio" value="inherit" /> 继承全局随机批次</label><label><input v-model="form.schedule_mode" type="radio" value="fixed_time" /> 固定时间</label><label><input v-model="form.schedule_mode" type="radio" value="custom_window" /> 自定义时段</label></div><p v-if="form.schedule_mode === 'inherit'" class="field-help">每天在全局 08:00–22:00 窗口内随机安排，并与其他账号串行执行。</p><div v-else-if="form.schedule_mode === 'fixed_time'" class="field"><label for="fixed-time">每天执行时间</label><input id="fixed-time" v-model="form.fixed_time" type="time" required /></div><div v-else class="form-grid"><div class="field"><label for="window-start">开始时间</label><input id="window-start" v-model="form.window_start" type="time" required /></div><div class="field"><label for="window-end">结束时间</label><input id="window-end" v-model="form.window_end" type="time" required /></div></div><p v-if="form.schedule_mode !== 'inherit'" class="field-help">时区：{{ SITEHUB_TIMEZONE }}。SiteHub 会在固定时间或指定时段内安排任务，无需填写 cron 表达式。</p></fieldset>
      <div class="switch-row"><div><div class="field-label">启用自动化</div><p class="field-help">{{ canEnableForm ? '停用后保留配置，但不会创建签到任务。' : '请先保存账号并验证浏览器 Profile；验证成功后才能启用。' }}</p></div><button type="button" class="switch" :class="{ on: form.enabled }" :disabled="!canEnableForm" :aria-pressed="form.enabled" @click="form.enabled = !form.enabled" /></div>
    </form>
    <template #footer><button class="button secondary" :disabled="saving" @click="editorOpen = false">取消</button><button form="account-editor" class="button" :disabled="saving || !form.adapter">{{ saving ? '正在保存…' : '保存账号' }}</button></template>
  </ModalDialog>
  <ModalDialog v-if="deleteTarget" title="删除签到账号" :busy="saving" @close="deleteTarget = null"><p>将删除“{{ deleteTarget.label }}”及其本地凭据和计划。历史运行记录仍会保留。</p><template #footer><button class="button secondary" @click="deleteTarget = null">取消</button><button class="button danger" :disabled="saving" @click="remove">确认删除</button></template></ModalDialog>
</template>

<style scoped>
.section-title { margin-bottom: 14px; }.section-title h2 { margin-bottom: 3px; }.section-title p { margin: 0; font-size: 12px; }.account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px; }.account-card { padding: 18px; }.account-title { display: flex; align-items: center; gap: 11px; }.account-avatar { display: grid; width: 36px; height: 36px; place-items: center; border-radius: 9px; color: var(--accent); background: var(--accent-soft); font-weight: 700; }.account-title h3 { margin: 0; }.account-title p { margin: 3px 0 0; font-size: 11px; }.account-status { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 18px 0; }.secret-status { color: var(--positive); font-size: 11px; }.secret-status.warning { color: var(--warning); }.account-card dl { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 0 0 12px; padding: 13px; border-radius: 8px; background: var(--surface-soft); }.account-card dl div { min-width: 0; }.account-card dt { color: var(--subtle); font-size: 10px; }.account-card dd { overflow: hidden; margin: 4px 0 0; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }.profile-summary { display: flex; align-items: center; gap: 6px; }.profile-error { margin: 0 0 12px; color: var(--negative); font-size: 11px; }.account-actions { display: flex; flex-wrap: wrap; gap: 7px; }.delete-link { margin-left: auto; color: var(--negative); }.adapter-note { display: flex; flex-direction: column; gap: 4px; padding: 12px 14px; border-left: 3px solid var(--accent); border-radius: 5px; background: var(--accent-soft); font-size: 12px; }.adapter-note span { color: var(--muted); }.schedule-fields { display: flex; flex-direction: column; gap: 13px; margin: 0; padding: 16px; border: 1px solid var(--line); border-radius: 9px; }.schedule-fields legend { padding: 0 6px; color: var(--text); font-size: 13px; font-weight: 650; }.schedule-modes { display: flex; flex-wrap: wrap; gap: 18px; }.schedule-modes label { display: flex; align-items: center; gap: 6px; font-size: 13px; }.schedule-modes input { width: 15px; min-height: 15px; }
@media (max-width: 900px) { .account-grid { grid-template-columns: 1fr; } } @media (max-width: 460px) { .account-actions .button { flex: 0 0 calc(50% - 4px); }.delete-link { width: 100%; margin: 4px 0 0; } }
</style>
