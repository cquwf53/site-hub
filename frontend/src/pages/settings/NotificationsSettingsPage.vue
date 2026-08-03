<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { NotificationChannel, NotificationChannelPage } from '@/api/types'
import { useAppStore } from '@/stores/app'

interface NotificationSettings {
  enabled: boolean
  price_drop: boolean
  back_in_stock: boolean
  checkin_failed: boolean
  checkin_success: boolean
  product_sync_failed?: boolean
  quiet_hours_enabled: boolean
  quiet_hours_start: string
  quiet_hours_end: string
  [key: string]: unknown
}

const EVENT_OPTIONS = [
  { value: 'price_drop', label: '商品降价' },
  { value: 'back_in_stock', label: '商品补货' },
  { value: 'checkin_failed', label: '签到失败' },
  { value: 'checkin_success', label: '签到成功' },
  { value: 'product_sync_failed', label: '商品同步失败' },
]
const SEVERITY_OPTIONS = [
  { value: 'success', label: '全部级别' },
  { value: 'info', label: '信息及以上' },
  { value: 'warning', label: '警告及以上' },
  { value: 'danger', label: '仅异常' },
]

const app = useAppStore()
const form = ref<NotificationSettings>({
  enabled: true,
  price_drop: true,
  back_in_stock: true,
  checkin_failed: true,
  checkin_success: false,
  product_sync_failed: true,
  quiet_hours_enabled: false,
  quiet_hours_start: '23:00',
  quiet_hours_end: '08:00',
})
const channels = ref<NotificationChannel[]>([])
const loading = ref(true)
const error = ref('')
const savingGlobal = ref(false)
const savingChannel = ref(false)
const channelBusyId = ref<string | null>(null)
const editorOpen = ref(false)
const editingId = ref<string | null>(null)
const deleteTarget = ref<NotificationChannel | null>(null)
const channelForm = ref({
  name: '',
  endpoint: '',
  enabled: true,
  event_types: [] as string[],
  min_severity: 'success',
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [settings, page] = await Promise.all([
      api.get<NotificationSettings>('/settings/notifications'),
      api.get<NotificationChannelPage>('/notification-channels', { page: 1, page_size: 200 }),
    ])
    form.value = settings
    channels.value = page.items
  } catch (value) {
    error.value = apiErrorMessage(value)
  } finally {
    loading.value = false
  }
}

async function saveGlobal() {
  savingGlobal.value = true
  try {
    form.value = await api.put<NotificationSettings>('/settings/notifications', form.value)
    app.toast('通知设置已保存', 'success')
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    savingGlobal.value = false
  }
}

function openCreate() {
  editingId.value = null
  channelForm.value = { name: '', endpoint: '', enabled: true, event_types: [], min_severity: 'success' }
  editorOpen.value = true
}

function openEdit(channel: NotificationChannel) {
  editingId.value = channel.id
  channelForm.value = {
    name: channel.name,
    endpoint: '',
    enabled: channel.enabled,
    event_types: [...channel.config.event_types],
    min_severity: channel.config.min_severity,
  }
  editorOpen.value = true
}

function toggleEventType(eventType: string) {
  const selected = new Set(channelForm.value.event_types)
  if (selected.has(eventType)) selected.delete(eventType)
  else selected.add(eventType)
  channelForm.value.event_types = [...selected]
}

async function saveChannel() {
  if (!channelForm.value.name.trim() || (!editingId.value && !channelForm.value.endpoint.trim())) return
  savingChannel.value = true
  const payload: Record<string, unknown> = {
    name: channelForm.value.name,
    enabled: channelForm.value.enabled,
    config: {
      event_types: channelForm.value.event_types,
      min_severity: channelForm.value.min_severity,
    },
  }
  if (channelForm.value.endpoint.trim()) payload.endpoint = channelForm.value.endpoint
  try {
    if (editingId.value) await api.patch<NotificationChannel>(`/notification-channels/${editingId.value}`, payload)
    else await api.post<NotificationChannel>('/notification-channels', { ...payload, channel_type: 'webhook' })
    editorOpen.value = false
    app.toast(editingId.value ? 'Webhook 通道已更新' : 'Webhook 通道已创建', 'success')
    await load()
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    savingChannel.value = false
  }
}

async function toggleChannel(channel: NotificationChannel) {
  channelBusyId.value = channel.id
  try {
    await api.patch<NotificationChannel>(`/notification-channels/${channel.id}`, { enabled: !channel.enabled })
    app.toast(channel.enabled ? 'Webhook 通道已停用' : 'Webhook 通道已启用', 'success')
    await load()
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    channelBusyId.value = null
  }
}

async function testChannel(channel: NotificationChannel) {
  channelBusyId.value = channel.id
  try {
    await api.post<{ outbox_id: string; status: string }>(`/notification-channels/${channel.id}/test`)
    app.toast('测试通知已加入发送队列', 'success')
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    channelBusyId.value = null
  }
}

async function deleteChannel() {
  if (!deleteTarget.value) return
  savingChannel.value = true
  try {
    await api.delete<void>(`/notification-channels/${deleteTarget.value.id}`)
    deleteTarget.value = null
    app.toast('Webhook 通道已删除', 'success')
    await load()
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    savingChannel.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="stack">
    <AsyncState :loading="loading" :error="error" @retry="load">
      <section class="card">
        <div class="card-header"><div><h2>事件通知</h2><p class="subtle-text">控制需要进入收件箱和外部通道的业务事件</p></div></div>
        <form class="card-body stack" @submit.prevent="saveGlobal">
          <div class="switch-row"><div><div class="field-label">启用通知</div><p class="field-help">关闭后保留设置，但不再生成外部通知。</p></div><button type="button" class="switch" :class="{ on: form.enabled }" :aria-pressed="form.enabled" aria-label="启用通知" @click="form.enabled = !form.enabled" /></div>
          <hr />
          <div v-for="item in EVENT_OPTIONS" :key="item.value" class="switch-row"><span class="field-label">{{ item.label }}</span><button type="button" class="switch" :class="{ on: Boolean(form[item.value]) }" :aria-pressed="Boolean(form[item.value])" :aria-label="item.label" @click="form[item.value] = !form[item.value]" /></div>
          <hr />
          <div class="switch-row"><div><div class="field-label">免打扰时段</div><p class="field-help">事件仍会进入收件箱，只暂停本机弹窗。</p></div><button type="button" class="switch" :class="{ on: form.quiet_hours_enabled }" :aria-pressed="form.quiet_hours_enabled" aria-label="免打扰时段" @click="form.quiet_hours_enabled = !form.quiet_hours_enabled" /></div>
          <div v-if="form.quiet_hours_enabled" class="form-grid"><div class="field"><label for="quiet-start">开始</label><input id="quiet-start" v-model="form.quiet_hours_start" type="time" /></div><div class="field"><label for="quiet-end">结束</label><input id="quiet-end" v-model="form.quiet_hours_end" type="time" /></div></div>
          <div class="form-actions"><button class="button" data-testid="save-global" :disabled="savingGlobal">{{ savingGlobal ? '保存中…' : '保存事件设置' }}</button></div>
        </form>
      </section>

      <section class="card">
        <div class="card-header"><div><h2>Webhook 通道</h2><p class="subtle-text">端点使用当前 Windows 用户的 DPAPI 加密，页面和日志不会回显</p></div><button class="button small" data-testid="add-channel" @click="openCreate">＋ 新建通道</button></div>
        <div v-if="channels.length" class="channel-list">
          <article v-for="channel in channels" :key="channel.id" class="channel-row" :data-channel-id="channel.id">
            <span class="channel-icon">↗</span>
            <div class="channel-copy"><div class="channel-title"><strong>{{ channel.name }}</strong><span :class="['channel-state', { disabled: !channel.enabled }]">{{ channel.enabled ? '已启用' : '已停用' }}</span><span v-if="channel.endpoint_configured" class="secret-state">🔒 端点已配置</span></div><p>{{ channel.config.event_types.length ? channel.config.event_types.map((type) => EVENT_OPTIONS.find((item) => item.value === type)?.label || type).join('、') : '全部业务事件' }} · {{ SEVERITY_OPTIONS.find((item) => item.value === channel.config.min_severity)?.label || channel.config.min_severity }}</p><small>待发送 {{ channel.delivery.pending }} · 已发送 {{ channel.delivery.sent }} · 失败 {{ channel.delivery.failed }}</small></div>
            <div class="channel-actions"><button class="button secondary small" :disabled="!channel.enabled || channelBusyId === channel.id" :data-testid="`test-${channel.id}`" @click="testChannel(channel)">测试</button><button class="button secondary small" :data-testid="`edit-${channel.id}`" @click="openEdit(channel)">编辑</button><button class="button secondary small" :disabled="channelBusyId === channel.id" :data-testid="`toggle-${channel.id}`" @click="toggleChannel(channel)">{{ channel.enabled ? '停用' : '启用' }}</button><button class="text-button danger-link" :data-testid="`delete-${channel.id}`" @click="deleteTarget = channel">删除</button></div>
          </article>
        </div>
        <div v-else class="empty-channels"><strong>还没有 Webhook 通道</strong><p>创建后可将价格、库存和签到事件发送到你的自动化服务。</p><button class="button secondary small" @click="openCreate">创建第一个通道</button></div>
      </section>
    </AsyncState>
  </div>

  <ModalDialog v-if="editorOpen" :title="editingId ? '编辑 Webhook 通道' : '新建 Webhook 通道'" :busy="savingChannel" wide @close="editorOpen = false">
    <form id="channel-form" class="stack" @submit.prevent="saveChannel">
      <div class="form-grid"><div class="field"><label for="channel-name">名称 *</label><input id="channel-name" v-model.trim="channelForm.name" required maxlength="120" placeholder="例如：家庭自动化" /></div><div class="field"><label for="channel-endpoint">Webhook 端点 {{ editingId ? '' : '*' }}</label><input id="channel-endpoint" v-model.trim="channelForm.endpoint" :required="!editingId" type="url" autocomplete="new-password" :placeholder="editingId ? '已配置；留空表示不修改' : 'https://example.com/webhook'" /><p v-if="editingId" class="field-help">🔒 当前端点已配置。输入新地址会替换原端点，旧地址永不回显。</p></div></div>
      <fieldset class="channel-filter"><legend>接收事件</legend><p class="field-help">不选择表示接收全部业务事件。</p><div class="event-options"><label v-for="item in EVENT_OPTIONS" :key="item.value"><input type="checkbox" :checked="channelForm.event_types.includes(item.value)" @change="toggleEventType(item.value)" /> {{ item.label }}</label></div></fieldset>
      <div class="form-grid"><div class="field"><label for="channel-severity">最低级别</label><select id="channel-severity" v-model="channelForm.min_severity"><option v-for="item in SEVERITY_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option></select></div><div class="switch-row compact"><span class="field-label">{{ editingId ? '启用通道' : '创建后启用' }}</span><button type="button" class="switch" :class="{ on: channelForm.enabled }" :aria-pressed="channelForm.enabled" @click="channelForm.enabled = !channelForm.enabled" /></div></div>
    </form>
    <template #footer><button class="button secondary" :disabled="savingChannel" @click="editorOpen = false">取消</button><button form="channel-form" class="button" data-testid="save-channel" :disabled="savingChannel || !channelForm.name || (!editingId && !channelForm.endpoint)">{{ savingChannel ? '保存中…' : '保存通道' }}</button></template>
  </ModalDialog>

  <ModalDialog v-if="deleteTarget" title="删除 Webhook 通道" :busy="savingChannel" @close="deleteTarget = null">
    <p>确认删除“{{ deleteTarget.name }}”？尚未发送的通知会一并取消，已保存的端点密钥将被清理。此操作不能撤销。</p>
    <template #footer><button class="button secondary" :disabled="savingChannel" @click="deleteTarget = null">取消</button><button class="button danger" data-testid="confirm-delete" :disabled="savingChannel" @click="deleteChannel">确认删除</button></template>
  </ModalDialog>
</template>

<style scoped>
hr { width: 100%; margin: 0; border: 0; border-top: 1px solid var(--line); }
.form-actions { display: flex; justify-content: flex-end; }
.channel-list { display: flex; flex-direction: column; }
.channel-row { display: grid; min-height: 96px; grid-template-columns: 38px minmax(260px, 1fr) auto; align-items: center; gap: 13px; padding: 15px 18px; border-bottom: 1px solid var(--line); }
.channel-row:last-child { border-bottom: 0; }
.channel-icon { display: grid; width: 36px; height: 36px; place-items: center; border-radius: 9px; color: var(--accent); background: var(--accent-soft); font-size: 18px; }
.channel-copy { min-width: 0; }
.channel-title { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.channel-copy p { margin: 5px 0 3px; color: var(--muted); font-size: 12px; }
.channel-copy small { color: var(--subtle); }
.channel-state, .secret-state { padding: 2px 6px; border-radius: 5px; color: var(--positive); background: var(--positive-soft); font-size: 10px; }
.channel-state.disabled { color: var(--muted); background: var(--surface-soft); }
.secret-state { color: var(--accent); background: var(--accent-soft); }
.channel-actions { display: flex; align-items: center; gap: 7px; }
.danger-link { color: var(--negative); }
.empty-channels { padding: 34px; text-align: center; }
.empty-channels p { margin: 6px 0 15px; font-size: 12px; }
.channel-filter { margin: 0; padding: 15px; border: 1px solid var(--line); border-radius: 9px; }
.channel-filter legend { padding: 0 6px; color: var(--text); font-size: 13px; font-weight: 650; }
.event-options { display: flex; flex-wrap: wrap; gap: 10px 18px; margin-top: 12px; }
.event-options label { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.event-options input { width: 15px; min-height: 15px; }
.switch-row.compact { min-height: 38px; align-self: end; }
@media (max-width: 950px) { .channel-row { grid-template-columns: 38px 1fr; }.channel-actions { grid-column: 2; flex-wrap: wrap; } }
@media (max-width: 600px) { .channel-row { grid-template-columns: 1fr; }.channel-icon { display: none; }.channel-actions { grid-column: 1; }.channel-actions .button { flex: 1; } }
</style>
