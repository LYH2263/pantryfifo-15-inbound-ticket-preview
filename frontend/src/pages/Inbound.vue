<template>
  <div>
    <h1>分批入库</h1>
    <p class="muted">先预检取票，再按票面确认写入；预检不改库存</p>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" placeholder="数量" />
    <input v-model="expiry" placeholder="到期 YYYY-MM-DD" />
    <button @click="precheck">预检</button>
    <div v-if="ticket" class="ticket">
      <p>票面：{{ ticket.name }} ×{{ ticket.qty }} · 到期 {{ ticket.expiry }} · 将落{{ layerLabel[ticket.layer] || ticket.layer }}层</p>
      <p class="muted">全层在架 {{ ticket.total_lot_count }} 批 · 本层 {{ ticket.layer_lot_count }} 批（预检未写入）</p>
      <button @click="confirm">确认入库</button>
    </div>
    <p v-if="msg" class="muted">{{ msg }}</p>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const router = useRouter()
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const expiry = ref(new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10))
const ticket = ref(null)
const msg = ref('')
const layerLabel = { upper: '上', mid: '中', lower: '下' }
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })
// 票面与表单绑定：任何改动作废旧票，必须重新预检
watch([item_id, qty, expiry], () => { ticket.value = null })
async function precheck() {
  msg.value = ''
  try {
    ticket.value = await api('/lots/precheck', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value, expiry: expiry.value }) })
  } catch (e) { ticket.value = null; msg.value = e.message }
}
async function confirm() {
  msg.value = ''
  try {
    const r = await api('/lots/confirm', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value, expiry: expiry.value, ticket: ticket.value.ticket }) })
    router.push('/layer/' + r.layer) // 确认后落到该层页，可见同一新批
  } catch (e) { ticket.value = null; msg.value = e.message }
}
</script>
