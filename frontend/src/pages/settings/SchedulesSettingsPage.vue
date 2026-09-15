<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Page, SiteAccount } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { formatDate } from '@/utils/format'

interface ScheduleSettings {
  timezone: string
  checkin_window_start: string
  checkin_window_end: string
  product_interval_minutes: number
}

interface ScheduleItem {
  id: string
  capability: 'checkin' | 'product_monitor' | string
  site_id: string
  site_name: string
  account_id?: string | null
  account_label?: string | null
  enabled: boolean
  policy_type: string
  timezone: string
  blocked_reason?: string | null
  next_action?: { label: string; href?: string | null; description?: string | null } | null
  next_run_at?: string | null
  current_job_id?: string | null
  current_status?: string | null
  current_job_at?: string | null
  last_run_at?: string | null
  last_job_id?: string | null
  last_status?: string | null
  last_error_kind?: string | null
  last_error_message?: string | null
  scraper_type?: string | null
  automatic_run_supported?: boolean
  automatic_run_reason?: string | null
  misfire_policy?: string
  max_attempts?: number
  policy: {
    mode?: string
    minutes?: number
    override?: boolean
    time?: string
    start?: string
    end?: string
    [key: string]: unknown
  }
}

interface ProductSettingsResult {
  enabled: boolean
  [key: string]: unknown
}

const app = useAppStore()
const settings = ref<ScheduleSettings>({
  timezone: 'Asia/Shanghai',
  checkin_window_start: '08:00',
  checkin_window_end: '22:00',
  product_interval_minutes: 60,
})
const schedules = ref<Page<ScheduleItem>>({ items: [], page: 1, page_size: 200, total: 0 })
const accounts = ref<Record<string, SiteAccount>>({})
const loading = ref(true)
const listLoading = ref(false)
const error = ref('')
const saving = ref(false)
const changing = ref<Set<string>>(new Set())
const capabilityFilter = ref('')
const enabledFilter = ref('')
const keyword = ref('')
const accountStateErrors = ref<Record<string, string>>({})

const filteredItems = computed(() => {
  const query = keyword.value.trim().toLocaleLowerCase('zh-CN')
  if (!query) return schedules.value.items
  return schedules.value.items.filter((item) =>
    `${item.site_name} ${item.account_label || ''}`.toLocaleLowerCase('zh-CN').includes(query),
  )
})

const windowValid = computed(() =>
  Boolean(settings.value.checkin_window_start)
  && Boolean(settings.value.checkin_window_end)
  && settings.value.checkin_window_start < settings.value.checkin_window_end,
)

function clockInput(value: string) {
  return value.slice(0, 5)
}

function clockPayload(value: string) {
  return value.length === 5 ? `${value}:00` : value
}

function setChanging(id: string, active: boolean) {
  const next = new Set(changing.value)
  if (active) next.add(id)
  else next.delete(id)
  changing.value = next
}

async function loadAccountStates(items: ScheduleItem[]) {
  const siteIds = [...new Set(items
    .filter((item) => item.capability === 'checkin' && item.account_id)
    .map((item) => item.site_id))]
  const errors: Record<string, string> = {}
  const entries = await Promise.all(siteIds.map(async (siteId) => {
    try {
      return await api.get<SiteAccount[]>(`/sites/${siteId}/accounts`)
    } catch (value) {
      errors[siteId] = apiErrorMessage(value)
      return []
    }
  }))
  accounts.value = Object.fromEntries(entries.flat().map((account) => [account.id, account]))
  accountStateErrors.value = errors
}

async function loadSchedules() {
  listLoading.value = true
  try {
    const result = await api.get<Page<ScheduleItem>>('/schedules', {
      page: 1,
      page_size: 200,
      capability: capabilityFilter.value || undefined,
      enabled: enabledFilter.value === '' ? undefined : enabledFilter.value === 'true',
    })
    schedules.value = result
    await loadAccountStates(result.items)
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    listLoading.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const value = await api.get<ScheduleSettings>('/settings/schedules')
    settings.value = {
      ...value,
      checkin_window_start: clockInput(value.checkin_window_start),
      checkin_window_end: clockInput(value.checkin_window_end),
    }
    await loadSchedules()
  } catch (value) {
    error.value = apiErrorMessage(value)
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  if (!windowValid.value) {
    app.toast('签到结束时间必须晚于开始时间', 'error')
    return
  }
  saving.value = true
  try {
    const value = await api.put<ScheduleSettings>('/settings/schedules', {
      timezone: 'Asia/Shanghai',
      checkin_window_start: clockPayload(settings.value.checkin_window_start),
      checkin_window_end: clockPayload(settings.value.checkin_window_end),
      product_interval_minutes: settings.value.product_interval_minutes,
    })
    settings.value = {
      ...value,
      checkin_window_start: clockInput(value.checkin_window_start),
      checkin_window_end: clockInput(value.checkin_window_end),
    }
    app.toast('全局调度策略已保存', 'success')
    await loadSchedules()
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    saving.value = false
  }
}

function accountFor(item: ScheduleItem) {
  return item.account_id ? accounts.value[item.account_id] : undefined
}

function accountStateError(item: ScheduleItem) {
  return item.capability === 'checkin' ? accountStateErrors.value[item.site_id] : undefined
}

function canToggle(item: ScheduleItem) {
  if (changing.value.has(item.id) || item.enabled) return !changing.value.has(item.id)
  if (item.capability !== 'checkin') return true
  if (accountStateError(item)) return false
  return accountFor(item)?.auth_state === 'ready'
}

function toggleTitle(item: ScheduleItem) {
  if (item.enabled) return '停用此计划'
  const stateError = accountStateError(item)
  if (stateError) return `账号状态加载失败：${stateError}。请刷新后再试`
  if (item.capability === 'checkin' && accountFor(item)?.auth_state !== 'ready') {
    return '账号登录状态不是 ready，请先到网站详情验证登录'
  }
  return '启用此计划'
}

async function toggle(item: ScheduleItem) {
  if (!canToggle(item)) return
  const enabled = !item.enabled
  setChanging(item.id, true)
  try {
    if (item.capability === 'product_monitor') {
      await api.put<ProductSettingsResult>(`/sites/${item.site_id}/product-settings`, { enabled })
    } else if (item.capability === 'checkin' && item.account_id) {
      await api.patch<SiteAccount>(`/accounts/${item.account_id}`, { enabled })
    } else {
      throw new Error('该能力暂不支持从此页面启停')
    }
    item.enabled = enabled
    app.toast(`${item.site_name} 已${enabled ? '启用' : '停用'}`, 'success')
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    setChanging(item.id, false)
  }
}

function capabilityLabel(value: string) {
  if (value === 'checkin') return '自动签到'
  if (value === 'product_monitor') return '商品监控'
  return value
}

function policyLabel(item: ScheduleItem) {
  if (item.policy_type === 'interval') {
    const minutes = item.policy.override
      ? Number(item.policy.minutes || settings.value.product_interval_minutes)
      : settings.value.product_interval_minutes
    return `每 ${minutes} 分钟${item.policy.override ? '（站点覆盖）' : '（继承全局）'}`
  }
  if (item.policy.mode === 'fixed_time') return `每天 ${String(item.policy.time || '—')}`
  if (item.policy.mode === 'custom_window') {
    return `${String(item.policy.start || '—')}–${String(item.policy.end || '—')} 内随机`
  }
  if (item.policy_type === 'daily_random_window') {
    return `${settings.value.checkin_window_start}–${settings.value.checkin_window_end} 内随机（全局）`
  }
  return item.policy_type
}

function nextRunLabel(item: ScheduleItem) {
  if (item.blocked_reason) return item.blocked_reason
  if (!item.enabled) return '已停用'
  if (item.capability === 'product_monitor' && item.automatic_run_supported === false) {
    return item.automatic_run_reason === 'manual_catalog' ? '手工目录，不自动抓取' : '等待配置抓取器'
  }
  if (item.next_run_at) return formatDate(item.next_run_at)
  if (item.capability === 'checkin') return '由每日批次安排'
  return '等待调度器计算'
}

function lastResultLabel(item: ScheduleItem) {
  return resultStatusLabel(item.last_status)
}

function resultStatusLabel(status?: string | null) {
  if (!status) return '尚未运行'
  if (status === 'succeeded') return '成功'
  if (status === 'failed') return '失败'
  if (status === 'skipped') return '已跳过'
  if (status === 'queued') return '已排队'
  if (status === 'running' || status === 'leased') return '运行中'
  if (status === 'effect_unknown') return '结果待确认'
  return status
}

onMounted(load)
</script>

<template>
  <div class="schedule-settings stack">
    <section class="card">
      <div class="card-header">
        <div>
          <h2>全局调度策略</h2>
          <p class="subtle-text">未设置单独覆盖的能力会继承这里的时间。</p>
        </div>
        <StatusBadge status="active" label="Asia/Shanghai" />
      </div>
      <AsyncState :loading="loading" :error="error" @retry="load">
        <form class="card-body strategy-form" @submit.prevent="saveSettings">
          <div class="strategy-block">
            <div class="strategy-heading">
              <span class="strategy-icon">签</span>
              <div><strong>每日签到窗口</strong><small>默认账号每天在窗口内随机安排一次</small></div>
            </div>
            <div class="time-range">
              <div class="field"><label for="checkin-start">开始</label><input id="checkin-start" v-model="settings.checkin_window_start" type="time" required /></div>
              <span>至</span>
              <div class="field"><label for="checkin-end">结束</label><input id="checkin-end" v-model="settings.checkin_window_end" type="time" required /></div>
            </div>
            <p v-if="!windowValid" class="field-error">结束时间必须晚于开始时间。</p>
          </div>
          <div class="strategy-block">
            <div class="strategy-heading">
              <span class="strategy-icon product">价</span>
              <div><strong>商品监控周期</strong><small>未单独设置周期的网站使用此值</small></div>
            </div>
            <div class="interval-field"><input id="product-interval" v-model.number="settings.product_interval_minutes" type="number" min="1" max="10080" required /><label for="product-interval">分钟</label></div>
          </div>
          <div class="strategy-footer">
            <span>时区固定为 {{ settings.timezone }}</span>
            <button class="button" :disabled="saving || !windowValid">{{ saving ? '保存中…' : '保存全局策略' }}</button>
          </div>
        </form>
      </AsyncState>
    </section>

    <section class="card">
      <div class="card-header schedule-list-header">
        <div>
          <h2>能力计划</h2>
          <p class="subtle-text">共 {{ schedules.total }} 项；启停会同步更新对应的网站能力或签到账号。</p>
        </div>
        <button class="button secondary small" data-testid="refresh-schedules" :disabled="listLoading" @click="loadSchedules">{{ listLoading ? '刷新中…' : '刷新' }}</button>
      </div>
      <div class="schedule-toolbar">
        <input v-model.trim="keyword" class="schedule-search" type="search" placeholder="搜索网站或账号" aria-label="搜索网站或账号" />
        <select v-model="capabilityFilter" aria-label="能力类型" @change="loadSchedules">
          <option value="">全部能力</option>
          <option value="product_monitor">商品监控</option>
          <option value="checkin">自动签到</option>
        </select>
        <select v-model="enabledFilter" aria-label="启用状态" @change="loadSchedules">
          <option value="">全部状态</option>
          <option value="true">已启用</option>
          <option value="false">已停用</option>
        </select>
      </div>
      <div v-if="Object.keys(accountStateErrors).length" class="account-state-alert" role="alert">
        <strong>部分签到账号状态加载失败</strong>
        <span>无法确认登录状态的计划已禁止启用，请刷新后重试。</span>
      </div>
      <div v-if="listLoading && !schedules.items.length" class="state-panel"><span class="spinner" /><p>正在加载计划…</p></div>
      <div v-else-if="!filteredItems.length" class="empty-compact">没有符合条件的调度计划。</div>
      <div v-else class="table-wrap">
        <table class="data-table schedule-table">
          <thead><tr><th>网站与能力</th><th>执行策略</th><th>下次运行</th><th>最近结果</th><th>状态</th><th /></tr></thead>
          <tbody>
            <tr v-for="item in filteredItems" :key="item.id">
              <td><div class="schedule-name"><strong>{{ item.site_name }}</strong><small>{{ capabilityLabel(item.capability) }}<template v-if="item.account_label"> · {{ item.account_label }}</template></small><small v-if="accountStateError(item)" class="account-state-error" :title="accountStateError(item)">账号状态加载失败</small></div></td>
              <td><div class="policy-cell"><strong>{{ policyLabel(item) }}</strong><small v-if="item.capability === 'product_monitor' && item.automatic_run_supported === false" class="unsupported-run">{{ item.automatic_run_reason === 'manual_catalog' ? '当前为手工商品目录' : '尚未配置自动抓取器' }}</small><small v-else>{{ item.timezone }} · {{ item.misfire_policy === 'recover' ? '错过后恢复' : item.misfire_policy || '标准策略' }}</small></div></td>
              <td><div class="policy-cell"><span class="next-run" :class="{ muted: !item.next_run_at }">{{ nextRunLabel(item) }}</span><small v-if="item.blocked_reason && item.next_run_at">预计 {{ formatDate(item.next_run_at) }}（解除阻止后）</small><RouterLink v-if="item.next_action?.href" class="text-button" :to="item.next_action.href" :title="item.next_action.description || undefined">{{ item.next_action.label }}</RouterLink><small v-else-if="item.next_action">{{ item.next_action.description || item.next_action.label }}</small></div></td>
              <td><div class="last-result"><div v-if="item.current_status" class="current-job"><span>当前</span><StatusBadge :status="item.current_status" :label="resultStatusLabel(item.current_status)" /></div><StatusBadge v-if="item.last_status" :status="item.last_status" :label="lastResultLabel(item)" /><span v-else-if="!item.current_status" class="muted">尚未运行</span><small v-if="item.last_error_message" :title="item.last_error_message">{{ item.last_error_message }}</small><small v-else-if="item.last_run_at">{{ formatDate(item.last_run_at) }}</small></div></td>
              <td><StatusBadge :status="item.enabled ? 'enabled' : 'disabled'" /></td>
              <td class="actions"><RouterLink class="text-button" :to="item.capability === 'checkin' ? `/sites/${item.site_id}/accounts` : `/sites/${item.site_id}/settings`">配置</RouterLink><button class="switch schedule-switch" :class="{ on: item.enabled }" :aria-label="`${item.site_name} ${item.enabled ? '停用' : '启用'}`" :aria-pressed="item.enabled" :data-testid="`toggle-schedule-${item.id}`" :disabled="!canToggle(item)" :title="toggleTitle(item)" @click="toggle(item)" /></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="schedules.total > schedules.page_size" class="list-limit-note">当前显示前 {{ schedules.page_size }} 项，请使用能力和状态筛选缩小范围。</div>
    </section>
  </div>
</template>

<style scoped>
.schedule-settings { width: min(920px, 100%); }
.strategy-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.strategy-block { padding: 16px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-soft); }
.strategy-heading { display: flex; align-items: center; gap: 11px; margin-bottom: 16px; }
.strategy-heading > div { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.strategy-heading small, .policy-cell small, .schedule-name small { color: var(--muted); font-size: 11px; font-weight: 400; }
.strategy-icon { display: grid; width: 34px; height: 34px; flex: 0 0 auto; place-items: center; border-radius: 9px; color: var(--accent); background: var(--accent-soft); font-size: 12px; font-weight: 700; }
.strategy-icon.product { color: var(--positive); background: var(--positive-soft); }
.time-range { display: grid; grid-template-columns: 1fr auto 1fr; align-items: end; gap: 10px; }
.time-range > span { padding-bottom: 10px; color: var(--subtle); font-size: 11px; }
.interval-field { display: flex; align-items: center; gap: 9px; }
.interval-field input { width: 130px; }
.interval-field label { color: var(--muted); font-size: 12px; }
.field-error { margin: 7px 0 0; color: var(--negative); font-size: 11px; }
.strategy-footer { display: flex; grid-column: 1 / -1; align-items: center; justify-content: space-between; gap: 15px; padding-top: 2px; }
.strategy-footer span { color: var(--subtle); font-size: 11px; }
.schedule-list-header { align-items: center; }
.schedule-toolbar { display: grid; grid-template-columns: minmax(180px, 1fr) 150px 130px; gap: 9px; padding: 12px 16px; border-bottom: 1px solid var(--line); background: var(--surface-soft); }
.account-state-alert { display: flex; flex-wrap: wrap; gap: 5px 10px; padding: 10px 16px; border-bottom: 1px solid #e7b9b9; color: var(--negative); background: #fff7f7; font-size: 11px; }
.account-state-error { color: var(--negative) !important; }
.unsupported-run { color: var(--warning) !important; }
.schedule-name, .policy-cell { display: flex; min-width: 155px; flex-direction: column; gap: 4px; }
.policy-cell { min-width: 205px; }
.policy-cell strong { font-size: 12px; font-weight: 600; }
.next-run { font-size: 12px; white-space: nowrap; }
.next-run.muted { color: var(--muted); }
.last-result { display: flex; max-width: 190px; min-width: 105px; flex-direction: column; align-items: flex-start; gap: 4px; }.last-result small { overflow: hidden; width: 100%; color: var(--subtle); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.current-job { display: flex; align-items: center; gap: 5px; }.current-job > span { color: var(--subtle); font-size: 9px; }
.schedule-switch { vertical-align: middle; }
.schedule-switch:disabled { cursor: not-allowed; opacity: .45; }
.list-limit-note { padding: 11px 16px; border-top: 1px solid var(--line); color: var(--warning); font-size: 11px; }
@media (max-width: 760px) {
  .strategy-form { grid-template-columns: 1fr; }
  .strategy-footer { grid-column: auto; flex-direction: column; align-items: stretch; }
  .schedule-toolbar { grid-template-columns: 1fr; }
}
</style>
