<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { InboxEvent, Page } from '@/api/types'
import { formatDate } from '@/utils/format'
import { useSiteDetail } from './siteContext'

const { site } = useSiteDetail()
const data = ref<Page<InboxEvent>>({ items: [], page: 1, page_size: 20, total: 0 })
const loading = ref(true); const error = ref(''); const pageNo = ref(1)
async function load() { if (!site.value) return; loading.value = true; error.value = ''; try { data.value = await api.get<Page<InboxEvent>>('/events', { site_id: site.value.id, page: pageNo.value, page_size: 20, include_dismissed: true }) } catch (value) { error.value = apiErrorMessage(value) } finally { loading.value = false } }
function changePage(value: number) { pageNo.value = value; void load() }
onMounted(load)
</script>
<template>
  <div class="row-between section-title"><div><h2>活动记录</h2><p>商品变化、签到与配置操作的完整时间线。</p></div></div>
  <section class="card"><AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无活动" empty-text="网站发生变化后会记录在这里。" @retry="load">
    <div class="timeline"><article v-for="event in data.items" :key="event.id" class="timeline-row"><span class="timeline-dot" /><div><div class="row"><strong>{{ event.title || event.type }}</strong><StatusBadge :status="event.severity" /></div><p>{{ event.message || '系统事件' }}</p><time>{{ formatDate(event.occurred_at) }}</time></div></article></div>
    <PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="changePage" />
  </AsyncState></section>
</template>
<style scoped>.section-title { margin-bottom: 14px; }.section-title h2 { margin-bottom: 3px; }.section-title p { margin: 0; font-size: 12px; }.timeline { padding: 8px 20px; }.timeline-row { position: relative; display: grid; grid-template-columns: 20px 1fr; gap: 11px; padding: 15px 0; border-bottom: 1px solid var(--line); }.timeline-row:last-child { border: 0; }.timeline-row::before { position: absolute; top: 29px; bottom: -15px; left: 6px; width: 1px; background: var(--line); content: ''; }.timeline-row:last-child::before { display: none; }.timeline-dot { width: 13px; height: 13px; margin-top: 3px; border: 3px solid var(--accent-soft); border-radius: 50%; background: var(--accent); }.timeline-row p { margin: 5px 0; font-size: 12px; }.timeline-row time { color: var(--subtle); font-size: 11px; }</style>
