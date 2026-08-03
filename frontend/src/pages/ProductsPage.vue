<script setup lang="ts">
import PageHeader from '@/components/PageHeader.vue'
import ProductList from '@/components/ProductList.vue'
</script>
<template><PageHeader title="商品" description="跨网站查看价格、库存与最近变化。"><template #actions><RouterLink class="button secondary" to="/sites">管理监控网站</RouterLink></template></PageHeader><ProductList /></template>
