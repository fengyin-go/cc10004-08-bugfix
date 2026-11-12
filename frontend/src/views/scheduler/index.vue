<template>
  <section class="page" data-module="scheduler">
    <header class="page-head">
      <div>
        <h2>调度管理</h2>
        <p class="page-desc">启用调度前核对关联流水线节点当前轮的完成证据：上一轮归档不能解锁新触发，关联只认节点 id。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记调度配置</button>
        <button class="btn" type="button" @click="exportRows">导出调度管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>关联节点与前置裁决</th>
          <th>可执行动作</th>
          <th>裁决轨迹</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="status-tag" :class="statusClass(row.status)">{{ row.status }}</span>
          </td>
          <td>
            <template v-if="row['关联节点']">
              <p class="cell-line">节点 id：{{ row['关联节点'] }}</p>
              <p v-if="verdict(row)" class="cell-line" :class="verdict(row)?.ok ? 'event-ok' : 'event-no'">
                {{ verdict(row)?.ok ? `已核对第 ${verdict(row)?.round_no} 轮证据` : '前置未通过' }}
              </p>
              <p v-if="verdict(row) && !verdict(row)?.ok" class="event-reason">{{ verdict(row)?.reason }}</p>
              <p v-if="verdict(row)?.completion_proof" class="muted">证据 {{ verdict(row)?.completion_proof }}</p>
            </template>
            <span v-else class="muted">未绑定节点（启用将被阻断）</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in allowedActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openBind(row)">绑定节点</button>
          </td>
          <td>
            <button class="link" type="button" @click="openDetail(row)">查看裁决记录</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无调度管理数据，可先登记调度配置</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条调度管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="binding" class="drawer-mask" @click.self="closeBind">
      <aside class="drawer drawer-sm" role="dialog" aria-label="绑定流水线节点">
        <header class="drawer-head">
          <h3>为配置 {{ binding['配置编号'] }} 绑定流水线节点</h3>
        </header>
        <div class="drawer-body">
          <p class="page-desc">只接受节点数字 id（如 1、2），不按节点名称猜测归属。</p>
          <form class="bind-form" @submit.prevent="submitBind">
            <label class="filter-item">
              <span>流水线节点 id</span>
              <input v-model="nodeIdInput" placeholder="例如 1" />
            </label>
            <button class="btn primary" type="submit">确认绑定</button>
            <button class="btn ghost" type="button" @click="closeBind">取消</button>
          </form>
        </div>
      </aside>
    </div>

    <div v-if="detail" class="drawer-mask" @click.self="closeDetail">
      <aside class="drawer" role="dialog" aria-label="调度裁决详情">
        <header class="drawer-head">
          <div>
            <h3>{{ detail['配置编号'] }} · 裁决详情</h3>
            <p class="page-desc">
              当前状态 {{ detail.status }}；
              <template v-if="detail['关联节点']">关联节点 id {{ detail['关联节点'] }}</template>
              <template v-else>尚未绑定关联节点</template>
            </p>
          </div>
          <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
        </header>
        <div class="drawer-body">
          <div v-if="detail['前置裁决']" class="round-card">
            <header class="round-head">
              <strong>最近一次启用前置判定</strong>
              <span :class="detail['前置裁决'].ok ? 'event-ok' : 'event-no'">
                {{ detail['前置裁决'].ok ? '放行' : '阻断' }}
              </span>
            </header>
            <p v-if="detail['前置裁决'].completion_proof" class="proof-line">
              核对证据：<code>{{ detail['前置裁决'].completion_proof }}</code>
              （第 {{ detail['前置裁决'].round_no }} 轮）
            </p>
            <p v-if="!detail['前置裁决'].ok" class="event-reason">{{ detail['前置裁决'].reason }}</p>
            <p class="muted">裁决时间：{{ detail['前置裁决'].at }}</p>
          </div>
          <ul class="event-list">
            <li v-for="ev in detail.events ?? []" :key="ev.event_id" :class="{ 'event-blocked': !ev.ok }">
              <span class="event-time">{{ ev.at }}</span>
              <span class="event-action">{{ ev.action }}</span>
              <span :class="ev.ok ? 'event-ok' : 'event-no'">{{ ev.ok ? '放行' : '阻断' }}</span>
              <span v-if="ev.completion_proof" class="muted">证据 {{ ev.completion_proof }}</span>
              <span v-if="!ev.ok && ev.reason" class="event-reason">{{ ev.reason }}</span>
            </li>
          </ul>
        </div>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, postAction } from '@/api/client'

type TraceEvent = {
  event_id: string
  at: string
  action: string
  ok: boolean
  reason: string
  completion_proof?: string
}

type Verdict = {
  ok: boolean
  reason: string
  node_id?: number
  round_no?: number
  completion_proof?: string
  at: string
}

type Row = Record<string, string | number | boolean | null> & {
  id: number
  status: string
  关联节点?: number | null
  前置裁决?: Verdict | null
  events?: TraceEvent[]
}

const ENDPOINT = '/api/scheduler'
const columns = ["配置编号", "关联任务", "调度表达式", "时区设定", "前置任务", "最大并发数", "失败重试策略", "超时终止"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const stats = ref([
  { label: "调度配置总数", value: 0 },
  { label: "已启用配置", value: 0 },
  { label: "待校验配置", value: 0 },
  { label: "前置阻断", value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)
const detail = ref<Row | null>(null)
const binding = ref<Row | null>(null)
const nodeIdInput = ref('')

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '调度配置登记入口尚未接入审批流'
}

function verdict(row: Row): Verdict | null | undefined {
  return row['前置裁决']
}

/** 调度顺序协议的前端镜像：只暴露当前状态下合法的下一动作，停用即终结。 */
function allowedActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交校验']
    case '待校验':
      return ['启用调度']
    case '已启用':
      return ['停用调度']
    default:
      return []
  }
}

function statusClass(status: string): string {
  if (status === '已启用') return 'status-done'
  if (status === '已停用') return 'status-failed'
  return 'status-idle'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    await postAction(`${ENDPOINT}/${row.id}/actions`, action)
    await reload()
  } catch (error) {
    // 前置阻断等业务拒绝原因直接展示，不吞掉
    errorMessage.value = error instanceof Error ? error.message : '调度管理操作失败'
    await reload()
  }
}

function openBind(row: Row) {
  binding.value = row
  nodeIdInput.value = row['关联节点'] != null ? String(row['关联节点']) : ''
  errorMessage.value = ''
}

function closeBind() {
  binding.value = null
  nodeIdInput.value = ''
}

async function submitBind() {
  if (!binding.value) return
  try {
    await postAction(`${ENDPOINT}/${binding.value.id}/actions`, '绑定节点', {
      node_id: nodeIdInput.value,
    })
    closeBind()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '绑定节点失败'
  }
}

async function openDetail(row: Row) {
  try {
    detail.value = await fetchJson<Row>(`${ENDPOINT}/${row.id}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams({
    ...(filters.value as Record<string, string>),
    ...(statusFilter.value ? { status: statusFilter.value } : {}),
  }).toString()
  try {
    const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = payload.total
    stats.value[1].value = rows.value.filter((r) => r.status === '已启用').length
    stats.value[2].value = rows.value.filter((r) => r.status === '待校验').length
    stats.value[3].value = rows.value.filter((r) => r['前置裁决'] && !r['前置裁决']?.ok).length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度管理列表读取失败'
  }
}

onMounted(reload)
</script>
