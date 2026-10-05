<template>
  <div>
    <h1>分批入库 · 票面预检</h1>
    <p class="muted">先预检拿票面，再确认整单写入；确认后该层页即可见到同一新批。</p>
    <div v-for="(ln, i) in lines" :key="i" class="inbound-line">
      <select v-model.number="ln.item_id">
        <option v-for="it in items" :key="it.id" :value="it.id">{{ it.name }}（{{ layerLabel[it.layer] || it.layer }}）</option>
      </select>
      <input type="number" v-model.number="ln.qty" placeholder="数量" />
      <input v-model="ln.expiry" placeholder="到期 YYYY-MM-DD" />
      <button v-if="lines.length > 1" @click="lines.splice(i, 1)">删行</button>
    </div>
    <button @click="addLine">加一行</button>
    <button @click="doPrecheck">预检</button>
    <template v-if="preview">
      <table class="preview">
        <thead><tr><th>品项</th><th>数量</th><th>到期日</th><th>将落层</th><th>落架状态</th></tr></thead>
        <tbody>
          <tr v-for="(p, i) in preview.lines" :key="i">
            <td>{{ p.name }}</td><td>{{ p.qty }}</td><td>{{ p.expiry }}</td>
            <td>{{ layerLabel[p.layer] || p.layer }}</td>
            <td>{{ p.removable ? '即刻可下架' : '在架' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted">票面 {{ preview.ticket.slice(0, 20) }}… · 改动数量/到期日后票面失效，需重新预检</p>
      <button @click="doConfirm">确认入库</button>
    </template>
    <p v-if="msg" class="muted">{{ msg }}</p>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const router = useRouter()
const items = ref([])
const lines = ref([{ item_id: 1, qty: 1, expiry: '2026-12-01' }])
const preview = ref(null)
const msg = ref('')
const layerLabel = { upper: '上层', mid: '中层', lower: '下层' }
const REASONS = {
  ticket_mismatch: '票面与内容不符（数量/到期日已改），请重新预检',
  qty_non_positive: '数量必须为正',
  item_not_found: '品项不存在',
  empty_lines: '空单不能入库',
  bad_line: '行格式错误（检查日期 YYYY-MM-DD）',
}
onMounted(async () => {
  items.value = await api('/items')
  if (items.value[0]) lines.value[0].item_id = items.value[0].id
})
// 预检后任何改动都让旧票面作废，必须重新预检
watch(lines, () => { preview.value = null }, { deep: true })
function addLine() { lines.value.push({ item_id: lines.value[0].item_id, qty: 1, expiry: '2026-12-01' }) }
const payload = () => lines.value.map(l => ({ item_id: l.item_id, qty: l.qty, expiry: l.expiry }))
async function doPrecheck() {
  msg.value = ''
  try {
    preview.value = await api('/inbound/precheck', { method: 'POST', body: JSON.stringify({ lines: payload() }) })
  } catch (e) {
    preview.value = null
    msg.value = '预检失败：' + (REASONS[e.message] || e.message)
  }
}
async function doConfirm() {
  msg.value = ''
  try {
    const r = await api('/inbound/confirm', {
      method: 'POST',
      body: JSON.stringify({ ticket: preview.value.ticket, lines: payload() }),
    })
    const layers = [...new Set(r.lots.map(l => l.layer))]
    router.push(layers.length === 1 ? '/layer/' + layers[0] : '/')
  } catch (e) {
    preview.value = null
    msg.value = '确认失败：' + (REASONS[e.message] || e.message)
  }
}
</script>
