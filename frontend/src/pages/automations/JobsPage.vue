<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AsyncState from '@/components/AsyncState.vue'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Job, Page, TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { formatDate } from '@/utils/format'

const route = useRoute(); const router = useRouter(); const app = useAppStore(); const tasks = useTaskStore()
const data = ref<Page<Job>>({ items: [], page: 1, page_size: 25, total: 0 }); const loading = ref(true); const error = ref(''); const status = ref(String(route.query.status || 'active'))
async function load() { loading.value = true; error.value = ''; try { data.value = await api.get<Page<Job>>('/jobs', { page: route.query.page || 1, page_size: 25, status: status.value === 'active' ? ['queued', 'leased', 'running'] : status.value || undefined }) } catch (value) { error.value = apiErrorMessage(value) } finally { loading.value = false } }
function filter() { void router.replace({ query: { status: status.value || undefined } }); void load() }
function trackIfTask(value: TaskRef | Job, title: string) { if ('jobId' in value) { tasks.track(value as TaskRef, title); app.taskDrawerOpen = true } }
async function action(job: Job, name: 'cancel' | 'retry') { try { const result = await api.post<TaskRef | Job>(`/jobs/${job.id}/${name}`); trackIfTask(result, `${name === 'retry' ? '重试' : '取消'}任务`); app.toast(name === 'retry' ? '重试已安排' : '任务已取消', 'success'); await load() } catch (value) { app.toast(apiErrorMessage(value), 'error') } }
onMounted(load)
</script>
<template>
  <div class="toolbar"><select v-model="status" class="status-select" @change="filter"><option value="active">当前任务</option><option value="">全部状态</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="cancelled">已取消</option></select><span class="toolbar-note">调度由本地 Worker 执行，页面关闭后仍会继续。</span></div>
  <section class="card"><AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无任务" empty-text="添加签到账号或启用商品追踪后，系统会自动创建任务。" @retry="load">
<div class="table-wrap"><table class="data-table"><thead><tr><th>任务</th><th>计划时间</th><th>状态</th><th>尝试</th><th>说明</th><th /></tr></thead><tbody><tr v-for="job in data.items" :key="job.id"><td><div class="job-name"><strong>{{ job.site_name || job.kind || '自动化任务' }}</strong><small>{{ job.account_label || job.kind }}</small></div></td><td>{{ formatDate(job.planned_at || job.available_at) }}</td><td><StatusBadge :status="job.status" /></td><td>{{ job.attempt ?? 0 }} / {{ job.max_attempts ?? '—' }}</td><td class="message-cell">{{ job.message || '—' }}</td><td class="actions"><button v-if="['queued','leased','running'].includes(job.status)" class="text-button" @click="action(job, 'cancel')">取消</button><button v-if="['failed','cancelled','effect_unknown'].includes(job.status)" class="text-button" @click="action(job, 'retry')">重试</button></td></tr></tbody></table></div><PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="router.replace({ query: { ...route.query, page: String($event) } }); load()" />
  </AsyncState></section>
</template>
<style scoped>.status-select { width: 135px; }.toolbar-note { color: var(--subtle); font-size: 11px; }.job-name { display: flex; min-width: 160px; flex-direction: column; gap: 3px; }.job-name small { color: var(--muted); }.message-cell { max-width: 300px; overflow: hidden; color: var(--muted); text-overflow: ellipsis; white-space: nowrap; }</style>
