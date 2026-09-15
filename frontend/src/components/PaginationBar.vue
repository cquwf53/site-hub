<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ page: number; pageSize: number; total: number }>()
defineEmits<{ change: [page: number] }>()
const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
</script>
<template>
  <div v-if="total > pageSize" class="pagination">
    <span>共 {{ total }} 条</span>
    <div><button class="icon-button" aria-label="上一页" :disabled="page <= 1" @click="$emit('change', page - 1)">‹</button><span>{{ page }} / {{ pages }}</span><button class="icon-button" aria-label="下一页" :disabled="page >= pages" @click="$emit('change', page + 1)">›</button></div>
  </div>
</template>
