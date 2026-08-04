<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import DynamicSchemaForm from '@/components/DynamicSchemaForm.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { AdapterManifest, Folder, Site, SiteType, Tag } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { siteTypeIcon } from '@/utils/siteType'
import { useSiteDetail } from './siteContext'

interface ProductSettings { enabled: boolean; interval_override_minutes: number | null; effective_interval_minutes?: number; scraper_type?: string; config?: Record<string, unknown>; configured_secret_fields?: string[]; [key: string]: unknown }
const { site, refresh } = useSiteDetail(); const app = useAppStore(); const router = useRouter()
const folders = ref<Folder[]>([]); const tags = ref<Tag[]>([]); const siteTypes = ref<SiteType[]>([]); const manifests = ref<AdapterManifest[]>([]); const product = ref<ProductSettings>({ enabled: false, interval_override_minutes: null })
const basic = ref({ name: '', url: '', folder_id: null as string | null, site_type_id: null as string | null, description: '', note: '', pinned: false, tag_ids: [] as string[] })
const adapter = ref({ id: '', config: {} as Record<string, unknown>, clear_secrets: [] as string[] }); const configuredAdapterSecrets = ref<string[]>([]); const saving = ref(''); const deleteOpen = ref(false)
const productManifests = computed(() => manifests.value.filter((item) => item.capabilities.includes('product_monitor')))
const manifest = computed(() => productManifests.value.find((item) => item.id === adapter.value.id))
const availableSiteTypes = computed(() => siteTypes.value.filter((item) => item.enabled || item.id === basic.value.site_type_id))

async function load() {
  if (!site.value) return
  basic.value = { name: site.value.name, url: site.value.url, folder_id: site.value.folder_id, site_type_id: site.value.site_type_id || null, description: site.value.description || '', note: site.value.note || '', pinned: site.value.pinned, tag_ids: site.value.tags?.map((tag) => tag.id) || [] }
  adapter.value = { id: site.value.adapter || '', config: structuredClone(site.value.config || {}), clear_secrets: [] }
  configuredAdapterSecrets.value = site.value.configured_secret_fields || []
  try {
    ;[folders.value, tags.value, siteTypes.value, manifests.value] = await Promise.all([api.get<Folder[]>('/folders'), api.get<Tag[]>('/tags'), api.get<SiteType[]>('/site-types'), api.get<AdapterManifest[]>('/adapters')])
    product.value = await api.get<ProductSettings>(`/sites/${site.value.id}/product-settings`)
    adapter.value = {
      id: product.value.scraper_type === 'none' ? '' : product.value.scraper_type || adapter.value.id,
      config: structuredClone(product.value.config || adapter.value.config),
      clear_secrets: [],
    }
    configuredAdapterSecrets.value = product.value.configured_secret_fields || configuredAdapterSecrets.value
  } catch { /* capabilities may be unavailable for bookmark-only sites */ }
}
async function saveBasic() { if (!site.value) return; saving.value = 'basic'; try { await api.patch<Site>(`/sites/${site.value.id}`, basic.value); app.toast('网站信息已保存', 'success'); await refresh() } catch (value) { app.toast(apiErrorMessage(value), 'error') } finally { saving.value = '' } }
async function saveProduct() { if (!site.value) return; saving.value = 'product'; try { await api.put<ProductSettings>(`/sites/${site.value.id}/product-settings`, product.value); app.toast('商品追踪设置已保存', 'success') } catch (value) { app.toast(apiErrorMessage(value), 'error') } finally { saving.value = '' } }
async function saveAdapter() { if (!site.value) return; saving.value = 'adapter'; try { await api.patch<Site>(`/sites/${site.value.id}`, { adapter: adapter.value.id, config: adapter.value.config, clear_secrets: adapter.value.clear_secrets }); app.toast('适配器配置已保存', 'success'); await refresh(); await load() } catch (value) { app.toast(apiErrorMessage(value), 'error') } finally { saving.value = '' } }
async function remove() { if (!site.value) return; saving.value = 'delete'; try { await api.delete<void>(`/sites/${site.value.id}`); app.toast('网站已删除', 'success'); void router.push('/sites') } catch (value) { app.toast(apiErrorMessage(value), 'error') } finally { saving.value = '' } }
onMounted(load)
</script>

<template>
  <div class="settings-stack">
    <section class="card"><div class="card-header"><div><h2>基本信息</h2><p class="subtle-text">名称、网址与整理方式</p></div></div><form class="card-body form-grid" @submit.prevent="saveBasic">
      <div class="field"><label for="settings-name">名称</label><input id="settings-name" v-model.trim="basic.name" required /></div><div class="field"><label for="settings-url">网站地址</label><input id="settings-url" v-model.trim="basic.url" required type="url" /></div>
      <div class="field"><label for="settings-folder">目录</label><select id="settings-folder" v-model="basic.folder_id"><option :value="null">未归档</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{ folder.name }}</option></select></div><div class="field"><label for="settings-site-type">主类型</label><select id="settings-site-type" v-model="basic.site_type_id" required><option :value="null" disabled>请选择网站类型</option><option v-for="siteType in availableSiteTypes" :key="siteType.id" :value="siteType.id">{{ siteTypeIcon(siteType.icon) }} {{ siteType.name }}{{ siteType.enabled ? '' : '（已停用）' }}</option></select></div>
      <div class="field span-2"><label for="settings-tags">标签</label><select id="settings-tags" v-model="basic.tag_ids" multiple><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{ tag.name }}</option></select><p class="field-help">主类型用于统一分类，标签可跨类型补充多个维度。</p></div>
      <div class="field span-2"><label for="settings-description">简介</label><textarea id="settings-description" v-model="basic.description" /></div><div class="field span-2"><label for="settings-note">私人备注</label><textarea id="settings-note" v-model="basic.note" /></div>
      <div class="switch-row span-2"><div><div class="field-label">置顶网站</div><p class="field-help">在网站列表中优先显示。</p></div><button type="button" class="switch" :class="{ on: basic.pinned }" :aria-pressed="basic.pinned" @click="basic.pinned = !basic.pinned" /></div>
      <div class="form-actions span-2"><button class="button" :disabled="saving === 'basic'">{{ saving === 'basic' ? '保存中…' : '保存基本信息' }}</button></div>
    </form></section>
    <section v-if="productManifests.length" class="card"><div class="card-header"><div><h2>网站适配器</h2><p class="subtle-text">普通配置可编辑；密钥只显示是否已配置，账号凭据仍在“签到账号”中单独管理</p></div></div><form class="card-body stack" @submit.prevent="saveAdapter"><div class="field"><label for="site-adapter">适配器</label><select id="site-adapter" v-model="adapter.id"><option value="">无专用适配器</option><option v-for="item in productManifests" :key="item.id" :value="item.id">{{ item.name || item.id }} · v{{ item.version }}</option></select></div><DynamicSchemaForm v-if="manifest" v-model="adapter.config" v-model:clear-secrets="adapter.clear_secrets" :schema="manifest.site_config_schema" :ui-schema="manifest.ui_schema" :secret-fields="manifest.secret_fields" :configured-secret-fields="configuredAdapterSecrets" :disabled="saving === 'adapter'" /><div class="form-actions"><button class="button" :disabled="saving === 'adapter'">保存适配器配置</button></div></form></section>
    <section class="card"><div class="card-header"><div><h2>商品追踪</h2><p class="subtle-text">控制商品采集与站点级执行周期</p></div></div><form class="card-body form-grid" @submit.prevent="saveProduct"><div class="switch-row span-2"><div><div class="field-label">启用商品追踪</div><p class="field-help">启用后由 Python 抓取器进入统一持久化队列。</p></div><button type="button" class="switch" :class="{ on: product.enabled }" :aria-pressed="product.enabled" @click="product.enabled = !product.enabled" /></div><div class="field"><label for="sync-interval">站点覆盖周期（分钟）</label><input id="sync-interval" v-model.number="product.interval_override_minutes" type="number" min="1" max="10080" placeholder="留空使用全局 60 分钟" /><p class="field-help">留空时使用全局周期；当前有效值 {{ product.effective_interval_minutes || 60 }} 分钟。</p></div><div /><div class="form-actions span-2"><button class="button" :disabled="saving === 'product'">保存商品设置</button></div></form></section>
    <section class="card danger-zone"><div><h2>删除网站</h2><p>删除网站、账号与当前配置；历史事件仍可在数据归档中保留。</p></div><button class="button secondary" @click="deleteOpen = true">删除网站</button></section>
  </div>
  <ModalDialog v-if="deleteOpen" title="确认删除网站" :busy="saving === 'delete'" @close="deleteOpen = false"><p>此操作无法撤销。确认删除“{{ site?.name }}”吗？</p><template #footer><button class="button secondary" @click="deleteOpen = false">取消</button><button class="button danger" :disabled="saving === 'delete'" @click="remove">确认删除</button></template></ModalDialog>
</template>
<style scoped>.settings-stack { display: flex; max-width: 820px; flex-direction: column; gap: 16px; }.form-actions { display: flex; justify-content: flex-end; padding-top: 4px; }.danger-zone { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 20px; border-color: #efcfcc; }.danger-zone h2 { color: var(--negative); }.danger-zone p { margin: 0; font-size: 12px; }select[multiple] { min-height: 78px; }</style>
