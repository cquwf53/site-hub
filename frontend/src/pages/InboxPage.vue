<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import PaginationBar from '@/components/PaginationBar.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { InboxEvent, Page } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { formatDate } from '@/utils/format'

const route = useRoute(); const router = useRouter(); const app = useAppStore()
const data = ref<Page<InboxEvent>>({ items: [], page: 1, page_size: 30, total: 0 }); const loading = ref(true); const error = ref('')
const scope = ref(String(route.query.scope || 'unread')); const severity = ref(String(route.query.severity || '')); const selectedId = computed(() => route.query.event ? String(route.query.event) : data.value.items[0]?.id)
const selected = computed(() => data.value.items.find((event) => event.id === selectedId.value) || null)

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await api.get<Page<InboxEvent>>('/events', { page: route.query.page || 1, page_size: 30, inbox: true, unread: scope.value === 'unread' ? true : undefined, dismissed: scope.value === 'archived' ? true : false, severity: severity.value }) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
function applyFilters() { void router.replace({ query: { scope: scope.value, severity: severity.value || undefined } }) }
function choose(event: InboxEvent) { void router.replace({ query: { ...route.query, event: event.id } }); if (!event.read_at) void markRead(event) }
async function markRead(event: InboxEvent) { const previous = event.read_at; event.read_at = new Date().toISOString(); try { await api.patch<InboxEvent>(`/events/${event.id}`, { read: true }) } catch (value) { event.read_at = previous; app.toast(apiErrorMessage(value), 'error') } }
async function archive(event: InboxEvent) { try { await api.patch<InboxEvent>(`/events/${event.id}`, { dismissed: true }); app.toast('消息已归档', 'success'); await load() } catch (value) { app.toast(apiErrorMessage(value), 'error') } }
async function markAllRead() { try { await api.post<void>('/events/read-all', { filters: { inbox: true, severity: severity.value || undefined } }); app.toast('已全部标为已读', 'success'); await load() } catch (value) { app.toast(apiErrorMessage(value), 'error') } }
function entityLink(event: InboxEvent) { if (event.entity_type === 'site' && event.entity_id) return `/sites/${event.entity_id}/overview`; if (event.run_id) return `/automations/runs/${event.run_id}`; return undefined }
watch(() => route.query, () => { scope.value = String(route.query.scope || 'unread'); severity.value = String(route.query.severity || ''); void load() }, { deep: true })
onMounted(load)
</script>

<template>
  <PageHeader title="收件箱" description="只保留需要你知晓或处理的价格、库存和自动化事件。"><template #actions><button class="button secondary" @click="markAllRead">全部标为已读</button></template></PageHeader>
  <div class="toolbar"><div class="segmented"><button :class="{ active: scope === 'unread' }" @click="scope = 'unread'; applyFilters()">未读</button><button :class="{ active: scope === 'all' }" @click="scope = 'all'; applyFilters()">全部</button><button :class="{ active: scope === 'archived' }" @click="scope = 'archived'; applyFilters()">已归档</button></div><select v-model="severity" class="severity-select" @change="applyFilters"><option value="">全部级别</option><option value="error">异常</option><option value="warning">警告</option></select></div>
  <section class="card inbox-shell"><AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="收件箱已清空" empty-text="新的商品变化和运行异常会出现在这里。" @retry="load">
    <div class="inbox-grid"><div class="message-list"><button v-for="event in data.items" :key="event.id" class="message-row" :class="{ selected: selectedId === event.id, unread: !event.read_at }" @click="choose(event)"><span class="message-mark" :class="event.severity">{{ event.severity === 'error' ? '!' : event.severity === 'warning' ? '△' : '•' }}</span><span class="message-copy"><span><strong>{{ event.title || event.type }}</strong><time>{{ formatDate(event.occurred_at) }}</time></span><small>{{ event.message || '查看详情' }}</small></span></button><PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="router.replace({ query: { ...route.query, page: String($event) } })" /></div>
      <article v-if="selected" class="message-detail"><div class="row-between"><StatusBadge :status="selected.severity" /><time>{{ formatDate(selected.occurred_at) }}</time></div><h2>{{ selected.title || selected.type }}</h2><p class="message-body">{{ selected.message || '此事件没有附加说明。' }}</p><dl v-if="selected.payload_json && Object.keys(selected.payload_json).length" class="event-data"><div v-for="(value, key) in selected.payload_json" :key="key"><dt>{{ key }}</dt><dd>{{ typeof value === 'object' ? JSON.stringify(value) : value }}</dd></div></dl><div class="detail-actions"><RouterLink v-if="entityLink(selected)" class="button" :to="entityLink(selected) || '/'">查看相关内容</RouterLink><button v-if="!selected.read_at" class="button secondary" @click="markRead(selected)">标为已读</button><button class="button secondary" @click="archive(selected)">归档</button></div></article>
    </div>
  </AsyncState></section>
</template>

<style scoped>
.segmented { display: flex; overflow: hidden; border: 1px solid var(--line); border-radius: 8px; }.segmented button { min-height: 34px; padding: 0 13px; border: 0; border-right: 1px solid var(--line); background: #fff; cursor: pointer; font-size: 12px; }.segmented button:last-child { border: 0; }.segmented button.active { color: var(--accent); background: var(--accent-soft); font-weight: 600; }.severity-select { width: 130px; }.inbox-shell { min-height: 540px; overflow: hidden; }.inbox-grid { display: grid; min-height: 540px; grid-template-columns: minmax(300px, 40%) 1fr; }.message-list { border-right: 1px solid var(--line); }.message-row { display: grid; width: 100%; min-height: 72px; grid-template-columns: 29px 1fr; align-items: center; gap: 10px; padding: 10px 14px; border: 0; border-bottom: 1px solid var(--line); background: #fff; cursor: pointer; text-align: left; }.message-row:hover { background: var(--surface-soft); }.message-row.selected { background: var(--accent-soft); }.message-row.unread strong::before { display: inline-block; width: 6px; height: 6px; margin: 0 6px 1px 0; border-radius: 50%; background: var(--accent); content: ''; }.message-mark { display: grid; width: 27px; height: 27px; place-items: center; border-radius: 8px; color: var(--accent); background: var(--accent-soft); }.message-mark.error { color: var(--negative); background: var(--negative-soft); }.message-mark.warning { color: var(--warning); background: var(--warning-soft); }.message-copy { min-width: 0; }.message-copy > span { display: flex; align-items: center; justify-content: space-between; gap: 7px; }.message-copy strong, .message-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.message-copy strong { font-size: 12px; }.message-copy small { display: block; margin-top: 4px; color: var(--muted); font-size: 11px; }.message-copy time { color: var(--subtle); font-size: 10px; white-space: nowrap; }.message-detail { padding: 28px; }.message-detail time { color: var(--subtle); font-size: 11px; }.message-detail h2 { margin-top: 22px; font-size: 22px; }.message-body { color: #4d4f56; font-size: 14px; white-space: pre-wrap; }.event-data { margin: 22px 0; padding: 14px; border-radius: 9px; background: var(--surface-soft); }.event-data div { display: grid; grid-template-columns: 130px 1fr; gap: 15px; padding: 7px; border-bottom: 1px solid var(--line); }.event-data div:last-child { border: 0; }.event-data dt { color: var(--muted); font-size: 11px; }.event-data dd { overflow-wrap: anywhere; margin: 0; font-size: 12px; }.detail-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 25px; }
@media (max-width: 820px) { .inbox-grid { grid-template-columns: 1fr; }.message-list { border-right: 0; }.message-detail { border-top: 1px solid var(--line); } } @media (max-width: 480px) { .message-detail { padding: 20px; }.event-data div { grid-template-columns: 1fr; gap: 3px; } }
</style>
