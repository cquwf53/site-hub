<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { SiteType } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { siteTypeIcon } from '@/utils/siteType'

interface SiteTypeForm {
  name: string
  slug: string
  color: string
  icon: string
  sort_order: number
  enabled: boolean
}

const app = useAppStore()
const siteTypes = ref<SiteType[]>([])
const loading = ref(true)
const error = ref('')
const deleteError = ref('')
const saving = ref(false)
const formOpen = ref(false)
const editingId = ref<string | null>(null)
const form = ref<SiteTypeForm>(emptyForm())

function emptyForm(): SiteTypeForm {
  return { name: '', slug: '', color: '#6976d9', icon: '', sort_order: 0, enabled: true }
}

async function load() {
  loading.value = true
  error.value = ''
  deleteError.value = ''
  try { siteTypes.value = await api.get<SiteType[]>('/site-types') }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}

function createSiteType() {
  editingId.value = null
  const nextSort = siteTypes.value.reduce((highest, item) => Math.max(highest, item.sort_order), -10) + 10
  form.value = { ...emptyForm(), sort_order: nextSort }
  formOpen.value = true
}

function editSiteType(siteType: SiteType) {
  editingId.value = siteType.id
  form.value = {
    name: siteType.name,
    slug: siteType.slug,
    color: siteType.color || '#6976d9',
    icon: siteType.icon || '',
    sort_order: siteType.sort_order,
    enabled: siteType.enabled,
  }
  formOpen.value = true
}

async function saveSiteType() {
  if (!form.value.name.trim() || !form.value.slug.trim()) return
  saving.value = true
  try {
    const current = siteTypes.value.find((item) => item.id === editingId.value)
    const payload: Partial<SiteTypeForm> = {
      name: form.value.name.trim(),
      slug: form.value.slug.trim().toLowerCase(),
      color: form.value.color,
      sort_order: Number(form.value.sort_order) || 0,
      enabled: form.value.enabled,
    }
    const icon = form.value.icon.trim()
    if (icon) payload.icon = icon
    if (current?.is_system) {
      delete payload.slug
      delete payload.enabled
    }
    if (editingId.value) await api.patch<SiteType>(`/site-types/${editingId.value}`, payload)
    else await api.post<SiteType>('/site-types', payload)
    formOpen.value = false
    app.toast('网站类型已保存', 'success')
    await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}

async function removeSiteType(siteType: SiteType) {
  if (siteType.is_system || !window.confirm(`删除网站类型“${siteType.name}”？`)) return
  deleteError.value = ''
  try {
    await api.delete<void>(`/site-types/${siteType.id}`)
    app.toast('网站类型已删除', 'success')
    await load()
  } catch (value) {
    deleteError.value = apiErrorMessage(value)
    app.toast(deleteError.value, 'error')
  }
}

onMounted(load)
</script>

<template>
  <section class="card">
    <div class="card-header">
      <div><h2>网站类型</h2><p class="subtle-text">每个网站选择一个主类型；标签仍可用于补充多个分类维度。</p></div>
      <button class="button small" data-testid="add-site-type" @click="createSiteType">＋ 新建类型</button>
    </div>
    <p v-if="deleteError" class="inline-error" role="alert">{{ deleteError }}</p>
    <AsyncState :loading="loading" :error="error" :empty="!siteTypes.length" empty-title="暂无网站类型" empty-text="新建类型后即可在网站列表中分类和筛选。" @retry="load">
      <div class="type-rows">
        <div v-for="siteType in siteTypes" :key="siteType.id" class="type-row" :class="{ disabled: !siteType.enabled }">
          <span class="type-mark" :style="{ color: siteType.color || '#6976d9', borderColor: siteType.color || '#6976d9', background: `${siteType.color || '#6976d9'}12` }">{{ siteTypeIcon(siteType.icon, siteType.name.slice(0, 1)) }}</span>
          <div class="type-info">
            <div class="type-title"><strong>{{ siteType.name }}</strong><span v-if="siteType.is_default" class="mini-badge">默认</span><span v-if="siteType.is_system" class="mini-badge neutral">系统</span><span v-if="!siteType.enabled" class="mini-badge muted-badge">已停用</span></div>
            <small>{{ siteType.slug }} · {{ siteType.site_count }} 个网站 · 排序 {{ siteType.sort_order }}</small>
          </div>
          <button class="text-button" :data-testid="`edit-site-type-${siteType.id}`" @click="editSiteType(siteType)">编辑</button>
          <button class="text-button danger-link" :data-testid="`delete-site-type-${siteType.id}`" :disabled="siteType.is_system" :title="siteType.is_system ? '系统内置类型不能删除' : siteType.site_count ? `仍有 ${siteType.site_count} 个网站使用，删除时需先处理引用` : '删除类型'" @click="removeSiteType(siteType)">删除</button>
        </div>
      </div>
    </AsyncState>
  </section>

  <ModalDialog v-if="formOpen" :title="editingId ? '编辑网站类型' : '新建网站类型'" :busy="saving" @close="formOpen = false">
    <form id="site-type-form" class="form-grid" @submit.prevent="saveSiteType">
      <div class="field"><label for="site-type-name">名称</label><input id="site-type-name" v-model.trim="form.name" required autofocus placeholder="例如：论坛" /></div>
      <div class="field"><label for="site-type-slug">稳定标识</label><input id="site-type-slug" v-model.trim="form.slug" required :disabled="siteTypes.find((item) => item.id === editingId)?.is_system" pattern="[a-z0-9]+(?:[-_][a-z0-9]+)*" placeholder="例如：forum" /><p class="field-help">仅使用小写字母、数字、横线或下划线。</p></div>
      <div class="field"><label for="site-type-color">颜色</label><input id="site-type-color" v-model="form.color" type="color" /></div>
      <div class="field"><label for="site-type-icon">图标文本</label><input id="site-type-icon" v-model="form.icon" maxlength="24" placeholder="例如：◫" /><p class="field-help">可输入短符号；留空时新类型使用默认图标，编辑时保留原图标。</p></div>
      <div class="field"><label for="site-type-sort">排序</label><input id="site-type-sort" v-model.number="form.sort_order" type="number" min="0" /></div>
      <div class="switch-row"><div><div class="field-label">启用</div><p class="field-help">停用后不能再分配给其他网站。</p></div><button type="button" class="switch" :class="{ on: form.enabled }" :aria-pressed="form.enabled" :disabled="siteTypes.find((item) => item.id === editingId)?.is_system" @click="form.enabled = !form.enabled" /></div>
    </form>
    <template #footer><button class="button secondary" :disabled="saving" @click="formOpen = false">取消</button><button form="site-type-form" class="button" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button></template>
  </ModalDialog>
</template>

<style scoped>
.type-rows { display: flex; flex-direction: column; }
.type-row { display: grid; min-height: 68px; grid-template-columns: 38px minmax(0, 1fr) auto auto; align-items: center; gap: 12px; padding: 10px 18px; border-bottom: 1px solid var(--line); }
.type-row:last-child { border: 0; }
.type-row.disabled { background: var(--surface-soft); }
.type-mark { display: grid; width: 34px; height: 34px; place-items: center; border: 1px solid; border-radius: 9px; font-size: 16px; font-weight: 700; }
.type-info { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.type-title { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.type-info strong { font-size: 13px; }
.type-info small { color: var(--subtle); }
.mini-badge { display: inline-flex; min-height: 19px; align-items: center; padding: 0 6px; border-radius: 5px; color: var(--accent); background: var(--accent-soft); font-size: 10px; font-weight: 650; }
.mini-badge.neutral { color: var(--muted); background: #f0f0f1; }
.mini-badge.muted-badge { color: var(--warning); background: var(--warning-soft); }
.danger-link { color: var(--negative); }
.danger-link:disabled { color: var(--subtle); cursor: not-allowed; }
.inline-error { margin: 0; padding: 10px 18px; border-bottom: 1px solid #efcfcc; color: var(--negative); background: var(--negative-soft); font-size: 12px; }
@media (max-width: 560px) { .type-row { grid-template-columns: 38px minmax(0, 1fr); }.type-row > button { grid-column: auto; } }
</style>
