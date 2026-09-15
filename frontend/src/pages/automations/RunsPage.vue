<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AsyncState from '@/components/AsyncState.vue'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Page, Run } from '@/api/types'
import { formatDate, formatDuration } from '@/utils/format'
const route = useRoute(); const router = useRouter(); const data = ref<Page<Run>>({ items: [], page: 1, page_size: 25, total: 0 }); const loading = ref(true); const error = ref(''); const status = ref(String(route.query.status || ''))
let loadVersion = 0
async function load() { const version = ++loadVersion; loading.value = true; error.value = ''; try { const result = await api.get<Page<Run>>('/runs', { page: route.query.page || 1, page_size: 25, status: status.value || undefined }); if (version === loadVersion) data.value = result } catch (value) { if (version === loadVersion) error.value = apiErrorMessage(value) } finally { if (version === loadVersion) loading.value = false } }
function filter() { void router.replace({ query: { status: status.value || 'all' } }) }
watch(() => route.query, () => { status.value = route.query.status === 'all' ? '' : String(route.query.status || ''); void load() }, { deep: true, immediate: true })
</script>
<template><div class="toolbar"><select v-model="status" class="status-select" @change="filter"><option value="">全部结果</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="cancelled">已取消</option></select></div><section class="card"><AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无运行记录" empty-text="任务执行后会在这里保留结果。" @retry="load"><div class="table-wrap"><table class="data-table"><thead><tr><th>网站 / 账号</th><th>类型</th><th>开始时间</th><th>耗时</th><th>结果</th><th /></tr></thead><tbody><tr v-for="run in data.items" :key="run.id"><td><div class="run-name"><strong>{{ run.site_name || '后台任务' }}</strong><small>{{ run.account_label || run.reason_code }}</small></div></td><td>{{ run.kind || '—' }}</td><td>{{ formatDate(run.started_at) }}</td><td>{{ formatDuration(run.duration_ms) }}</td><td><StatusBadge :status="run.status" /></td><td class="actions"><RouterLink class="text-button" :to="`/automations/runs/${run.id}`">详情</RouterLink></td></tr></tbody></table></div><PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="router.replace({ query: { ...route.query, page: String($event) } })" /></AsyncState></section></template>
<style scoped>.status-select { width: 135px; }.run-name { display: flex; min-width: 170px; flex-direction: column; gap: 3px; }.run-name small { color: var(--muted); }</style>
