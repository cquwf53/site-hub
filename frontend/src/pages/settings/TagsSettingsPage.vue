<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Folder, Tag } from '@/api/types'
import { useAppStore } from '@/stores/app'

const app = useAppStore()
const tags = ref<Tag[]>([])
const folders = ref<Folder[]>([])
const loading = ref(true)
const error = ref('')
const saving = ref(false)
const tagOpen = ref(false)
const editingTag = ref<string | null>(null)
const tagForm = ref({ name: '', color: '#6976d9', icon: '' })
const folderOpen = ref(false)
const editingFolder = ref<string | null>(null)
const folderForm = ref({ name: '', parent_id: null as string | null, sort_order: 0 })

async function load() {
  loading.value = true; error.value = ''
  try { ;[folders.value, tags.value] = await Promise.all([api.get<Folder[]>('/folders'), api.get<Tag[]>('/tags')]) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}

function createTag() { editingTag.value = null; tagForm.value = { name: '', color: '#6976d9', icon: '' }; tagOpen.value = true }
function editTag(tag: Tag) { editingTag.value = tag.id; tagForm.value = { name: tag.name, color: tag.color || '#6976d9', icon: tag.icon || '' }; tagOpen.value = true }
async function saveTag() {
  if (!tagForm.value.name.trim()) return
  saving.value = true
  try {
    if (editingTag.value) await api.patch<Tag>(`/tags/${editingTag.value}`, tagForm.value)
    else await api.post<Tag>('/tags', tagForm.value)
    tagOpen.value = false; app.toast('标签已保存', 'success'); await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}
async function removeTag(tag: Tag) {
  if (!window.confirm(`删除标签“${tag.name}”？网站不会被删除。`)) return
  try { await api.delete<void>(`/tags/${tag.id}`); app.toast('标签已删除', 'success'); await load() }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}

function createFolder() { editingFolder.value = null; folderForm.value = { name: '', parent_id: null, sort_order: folders.value.length }; folderOpen.value = true }
function editFolder(folder: Folder) { editingFolder.value = folder.id; folderForm.value = { name: folder.name, parent_id: folder.parent_id, sort_order: folder.sort_order }; folderOpen.value = true }
async function saveFolder() {
  if (!folderForm.value.name.trim()) return
  saving.value = true
  try {
    if (editingFolder.value) await api.patch<Folder>(`/folders/${editingFolder.value}`, folderForm.value)
    else await api.post<Folder>('/folders', folderForm.value)
    folderOpen.value = false; app.toast('目录已保存', 'success'); await load()
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}
async function removeFolder(folder: Folder) {
  if (!window.confirm(`删除空目录“${folder.name}”？含子目录或网站时系统会拒绝删除。`)) return
  try { await api.delete<void>(`/folders/${folder.id}`); app.toast('目录已删除', 'success'); await load() }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
function parentName(id: string | null) { return id ? folders.value.find((folder) => folder.id === id)?.name || '未知目录' : '顶层' }
onMounted(load)
</script>

<template>
  <div class="stack">
    <section class="card">
      <div class="card-header"><div><h2>网站目录</h2><p class="subtle-text">目录形成树状层级；商品分类与这里完全独立</p></div><button class="button small" @click="createFolder">＋ 新建目录</button></div>
      <AsyncState :loading="loading" :error="error" :empty="!folders.length" empty-title="暂无目录" empty-text="创建第一个网站目录。" @retry="load">
        <div class="setting-rows"><div v-for="folder in folders" :key="folder.id" class="setting-row"><span class="folder-icon">□</span><div><strong>{{ folder.name }}</strong><small>上级：{{ parentName(folder.parent_id) }} · 排序 {{ folder.sort_order }}</small></div><button class="text-button" @click="editFolder(folder)">编辑</button><button class="text-button danger-link" @click="removeFolder(folder)">删除</button></div></div>
      </AsyncState>
    </section>
    <section class="card">
      <div class="card-header"><div><h2>标签</h2><p class="subtle-text">标签可跨目录组织网站</p></div><button class="button small" @click="createTag">＋ 新建标签</button></div>
      <AsyncState :loading="loading" :error="error" :empty="!tags.length" empty-title="暂无标签" empty-text="创建标签，为网站建立跨目录分类。" @retry="load">
        <div class="setting-rows"><div v-for="tag in tags" :key="tag.id" class="setting-row"><span class="color-dot" :style="{ background: tag.color || '#999' }" /><div><strong>{{ tag.name }}</strong><small>{{ tag.icon || '无图标' }}</small></div><button class="text-button" @click="editTag(tag)">编辑</button><button class="text-button danger-link" @click="removeTag(tag)">删除</button></div></div>
      </AsyncState>
    </section>
  </div>

  <ModalDialog v-if="tagOpen" :title="editingTag ? '编辑标签' : '新建标签'" :busy="saving" @close="tagOpen = false"><form id="tag-form" class="form-grid" @submit.prevent="saveTag"><div class="field span-2"><label for="tag-name">名称</label><input id="tag-name" v-model.trim="tagForm.name" required autofocus /></div><div class="field"><label for="tag-color">颜色</label><input id="tag-color" v-model="tagForm.color" type="color" /></div><div class="field"><label for="tag-icon">图标文本</label><input id="tag-icon" v-model="tagForm.icon" maxlength="4" placeholder="可选" /></div></form><template #footer><button class="button secondary" @click="tagOpen = false">取消</button><button form="tag-form" class="button" :disabled="saving">保存</button></template></ModalDialog>
  <ModalDialog v-if="folderOpen" :title="editingFolder ? '编辑目录' : '新建目录'" :busy="saving" @close="folderOpen = false"><form id="folder-form" class="form-grid" @submit.prevent="saveFolder"><div class="field span-2"><label for="folder-name">目录名称</label><input id="folder-name" v-model.trim="folderForm.name" required autofocus /></div><div class="field"><label for="folder-parent">上级目录</label><select id="folder-parent" v-model="folderForm.parent_id"><option :value="null">顶层</option><option v-for="folder in folders.filter((item) => item.id !== editingFolder)" :key="folder.id" :value="folder.id">{{ folder.name }}</option></select></div><div class="field"><label for="folder-sort">排序</label><input id="folder-sort" v-model.number="folderForm.sort_order" type="number" min="0" /></div></form><template #footer><button class="button secondary" @click="folderOpen = false">取消</button><button form="folder-form" class="button" :disabled="saving">保存</button></template></ModalDialog>
</template>

<style scoped>
.setting-rows { display: flex; flex-direction: column; }
.setting-row { display: grid; min-height: 58px; grid-template-columns: 16px 1fr auto auto; align-items: center; gap: 12px; padding: 9px 18px; border-bottom: 1px solid var(--line); }
.setting-row:last-child { border: 0; }
.color-dot { width: 10px; height: 10px; border-radius: 50%; }
.folder-icon { color: var(--accent); }
.setting-row div { display: flex; flex-direction: column; gap: 2px; }
.setting-row strong { font-size: 13px; }
.setting-row small { color: var(--subtle); }
.danger-link { color: var(--negative); }
</style>
