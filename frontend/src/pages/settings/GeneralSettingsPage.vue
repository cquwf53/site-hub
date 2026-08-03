<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import { useAppStore } from '@/stores/app'

interface GeneralSettings { language: string; timezone: string; retain_history_days: number }
const app = useAppStore(); const form = ref<GeneralSettings>({ language: 'zh-CN', timezone: 'Asia/Shanghai', retain_history_days: 90 }); const worker = ref<Record<string, unknown>>({}); const loading = ref(true); const error = ref(''); const saving = ref(false)
async function load() { loading.value = true; error.value = ''; try { const [settings, workerState] = await Promise.all([api.get<GeneralSettings>('/settings/general'), api.get<Record<string, unknown>>('/worker/status')]); form.value = { language: settings.language, timezone: settings.timezone, retain_history_days: settings.retain_history_days }; worker.value = workerState } catch (value) { error.value = apiErrorMessage(value) } finally { loading.value = false } }
async function save() { saving.value = true; try { form.value = await api.put<GeneralSettings>('/settings/general', form.value); app.toast('通用设置已保存', 'success') } catch (value) { app.toast(apiErrorMessage(value), 'error') } finally { saving.value = false } }
onMounted(load)
</script>
<template><div class="stack"><section class="card"><div class="card-header"><div><h2>通用</h2><p class="subtle-text">仅影响这台电脑上的 SiteHub</p></div><StatusBadge :status="String(worker.status || 'offline')" :label="worker.status === 'online' ? 'Worker 正常' : 'Worker 未连接'" /></div><AsyncState :loading="loading" :error="error" @retry="load"><form class="card-body form-grid" @submit.prevent="save"><div class="field"><label for="language">界面语言</label><select id="language" v-model="form.language"><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></div><div class="field"><label for="timezone">时区</label><input id="timezone" v-model="form.timezone" placeholder="Asia/Shanghai" /></div><div class="field"><label for="history-days">历史保留天数</label><input id="history-days" v-model.number="form.retain_history_days" type="number" min="7" max="3650" /></div><div /><div class="form-actions span-2"><button class="button" :disabled="saving">{{ saving ? '保存中…' : '保存设置' }}</button></div></form></AsyncState></section><section class="card startup-note"><h2>Windows 开机启动</h2><p>开机任务属于切换/运维操作，由 <code>tools\install-startup.ps1</code> 和 <code>tools\uninstall-startup.ps1</code> 明确注册或移除。后台任务不会主动打开浏览器，启动日志保存在 <code>data\logs\startup.log</code>。</p></section></div></template>
<style scoped>.form-actions { display: flex; justify-content: flex-end; padding-top: 7px; }.startup-note { padding: 20px; }.startup-note h2 { margin-bottom: 7px; }.startup-note p { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.7; }.startup-note code { color: var(--text); }</style>
