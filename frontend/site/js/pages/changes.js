import { api } from '../api.js';
import { ENUMS, badgeTone, can, changeTone, findingTone, reconcileTone } from '../config.js';
import { drawer, emptyState, errorBox, errorMessage, esc, field, filterBar, fmtTime, impactRows, kv, table, toast } from '../ui.js';

const state = { status: '' };

const EXPORTS = [
  ['/api/exports/metrics.csv', 'metrics.csv', '指标清单', '48 条口径 + 解析结果 + 下游报表'],
  ['/api/exports/conflicts.csv', 'conflicts.csv', '冲突与影响面', '同名冲突、差异字段、严重度'],
  ['/api/exports/change-requests.csv', 'change-requests.csv', '变更单', '影响面与审批结论'],
  ['/api/exports/run-findings.csv', 'run-findings.csv', '校验问题清单', '每次运行的问题身份键与新增/解除/延续对账'],
  ['/api/exports/runs.csv', 'runs.csv', '校验运行记录', '运行统计、对账基线与新增/解除/延续计数'],
];

function reconcileBadge(runState) {
  if (!runState) return '<span class="muted tiny">—</span>';
  return `<span class="badge ${reconcileTone(runState)}">${esc(ENUMS.reconcileState[runState] || runState)}</span>`;
}

function typeCounts(run) {
  const counts = run.metrics || {};
  return [
    ['口径冲突', counts.conflicts_total ?? 0],
    ['依赖环', counts.cycles_total ?? 0],
    ['解析失败', counts.parse_error_count ?? 0],
  ];
}

function reconcileSummary(run) {
  const rec = run.reconciliation || {};
  const counts = rec.counts || {};
  if (!rec.baseline_run_no) {
    return '<span class="muted tiny">首份有结论运行，无对账基线</span>';
  }
  return `<div class="chip-row">
      <span class="badge high">新增 ${esc(counts.new ?? 0)}</span>
      <span class="badge ok">解除 ${esc(counts.resolved ?? 0)}</span>
      <span class="badge pending">延续 ${esc(counts.continued ?? 0)}</span>
      <span class="muted tiny">基线 <span class="mono">${esc(rec.baseline_run_no)}</span></span>
    </div>`;
}

function findingRow(finding, stateValue) {
  const codes = (finding.metric_codes || []).join(' / ');
  const path = finding.path ? finding.path.join(' → ') : '';
  const stateCell = stateValue ? reconcileBadge(stateValue) : '<span class="muted tiny">—</span>';
  return `<tr data-finding="${esc(finding.finding_key)}">
    <td>${stateCell}</td>
    <td><span class="badge ${findingTone(finding.finding_type)}">${esc(ENUMS.findingType[finding.finding_type] || finding.finding_type)}</span></td>
    <td><b>${esc(finding.title)}</b><div class="mono muted tiny">${esc(finding.finding_key)}</div></td>
    <td class="mono tiny">${esc(path || codes || '—')}</td>
    <td>${finding.severity ? `<span class="badge ${badgeTone(finding.severity)}">${esc(finding.severity_label || finding.severity)}</span>` : '<span class="muted tiny">—</span>'}</td>
  </tr>`;
}

function openRunDetail(view, ctx, run, allRuns) {
  const rec = run.reconciliation || {};
  const stateByKey = new Map();
  ['new', 'resolved', 'continued'].forEach((key) => {
    (rec[key] || []).forEach((item) => stateByKey.set(item.finding_key, key));
  });
  const currentRows = (run.findings || [])
    .map((finding) => findingRow(finding, stateByKey.get(finding.finding_key)))
    .join('');
  // 解除的问题不在本次清单里，从对账结论取完整内容
  const resolvedRows = (rec.resolved || [])
    .map((finding) => findingRow(finding, 'resolved'))
    .join('');
  const counts = rec.counts || {};
  const { root, close } = drawer({
    title: `校验运行 · ${run.run_no}`,
    subtitle: `${esc(ENUMS.runStatus[run.status] || run.status)} · ${fmtTime(run.started_at)} · ${run.operator_email}`,
    body: `
      ${kv([
        ['运行状态', esc(ENUMS.runStatus[run.status] || run.status)],
        ['输入口径', `${esc(run.input_count)} 条`],
        ['问题总数', `${esc(run.output_count)} 条（口径冲突 / 依赖环 / 解析失败分开记录）`],
        ['三类问题', typeCounts(run).map(([label, value]) => `${esc(label)} ${esc(value)}`).join(' · ')],
        ['对账基线', rec.baseline_run_no ? `<span class="mono">${esc(rec.baseline_run_no)}</span>` : '无（首份有结论运行）'],
        ['对账结论', reconcileSummary(run)],
        ['日志', `<span class="tiny">${esc(run.log_text || '—')}</span>`],
      ])}
      ${
        run.status === 'failed'
          ? `<div class="error-box"><span>该运行未完整完成，业务变更已回滚并保留上一份可用历史；本记录不携带问题清单，也不参与对账。</span></div>`
          : `
      <div class="dsl-box">
        <span class="label">本次问题清单（${esc(run.findings?.length || 0)} 条，带稳定身份键）</span>
        ${
          run.findings?.length
            ? `<div class="table-wrap"><table class="data"><thead><tr>
                <th>对账</th><th>类型</th><th>问题</th><th>环路径 / 涉及指标</th><th>严重度</th>
              </tr></thead><tbody>${currentRows}${resolvedRows}</tbody></table></div>`
            : emptyState({ title: '本次没有检出问题', hint: '所有口径解析通过、无同名冲突、依赖图无环。' })
        }
      </div>
      <div class="dsl-box">
        <span class="label">与 <span class="mono">${esc(rec.baseline_run_no || '—')}</span> 的对账明细</span>
        <div class="list-tight">
          <div class="impact-row"><span><span class="badge high">新增</span> 上一份没有、本次新检出</span><span class="mono">${esc(counts.new ?? 0)} 条</span></div>
          <div class="impact-row"><span><span class="badge ok">解除</span> 上一份有、本次消失</span><span class="mono">${esc(counts.resolved ?? 0)} 条</span></div>
          <div class="impact-row"><span><span class="badge pending">延续</span> 两份都在（内容可能变化）</span><span class="mono">${esc(counts.continued ?? 0)} 条</span></div>
        </div>
      </div>`
      }`,
  });
  root.querySelector('[data-close]')?.addEventListener('click', close);
}

function openChange(view, ctx, change) {
  const approver = can(ctx.user.role, 'change:approve');
  const impact = change.impact || {};
  const { root, close } = drawer({
    title: `${change.cr_no} · ${change.change_kind_label}`,
    subtitle: `${change.target_ref} · 状态 ${change.status_label}`,
    body: `
      ${kv([
        ['目标', `<span class="mono">${esc(change.target_ref)}</span>`],
        ['类型', esc(change.change_kind_label)],
        ['严重度', `<span class="badge ${badgeTone(change.severity)}">${esc(ENUMS.severity[change.severity])}</span>`],
        ['影响分', `<span class="mono">${esc(change.impact_score)}</span>（核心报表 ×3）`],
        ['影响指标', `${esc(impact.metric_count ?? (impact.metric_codes || []).length)} 个`],
        ['影响报表', `${esc(impact.report_count || 0)} 张`],
        ['申请人', esc(change.requester_email)],
        ['申请时间', fmtTime(change.created_at)],
        ['审批人', esc(change.approver_email || '—')],
        ['审批结论', esc(change.decision_note || '—')],
        ['决定时间', fmtTime(change.decided_at)],
      ])}
      <div class="dsl-box">
        <span class="label">变更原因</span>
        <p class="section-note">${esc(change.reason || '—')}</p>
      </div>
      ${
        change.proposed_dsl
          ? `<div class="dsl-box"><span class="label">拟定口径 DSL（算法 A 已校验）</span><pre class="code">${esc(change.proposed_dsl)}</pre></div>`
          : ''
      }
      <div class="dsl-box">
        <span class="label">影响面 · 算法 B 计算</span>
        ${impactRows(impact)}
      </div>
      ${
        approver && change.status === 'pending'
          ? `<div class="dsl-box">
              <span class="label">管理员审批</span>
              ${field({ label: '审批备注', name: 'note', value: '', placeholder: '批准理由 / 驳回原因' })}
              <div style="display:flex;gap:8px">
                <button class="btn sm accent" id="approve" type="button">批准</button>
                <button class="btn sm ghost" id="reject" type="button">驳回</button>
              </div>
            </div>`
          : change.status === 'pending'
            ? '<p class="muted tiny">待管理员审批。专员可以提交变更单与查看影响面，不能批准。</p>'
            : '<p class="muted tiny">该变更单已完成审批。</p>'
      }`,
  });

  const decide = async (status) => {
    try {
      await api.patch(`/api/change-requests/${change.id}`, {
        status,
        note: root.querySelector('[name=note]')?.value?.trim() || null,
      });
      toast(status === 'approved' ? '已批准变更单' : '已驳回变更单');
      close();
      await changesPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  };
  root.querySelector('#approve')?.addEventListener('click', () => decide('approved'));
  root.querySelector('#reject')?.addEventListener('click', () => decide('rejected'));
}

async function renderAudit(view, ctx, user) {
  const panel = view.querySelector('#audit-panel');
  if (!panel) return;
  try {
    const data = await api.get('/api/audit', { limit: 10 });
    panel.innerHTML = `<div class="panel-head"><h2>审计尾迹</h2><span class="meta">${data.total} 条 · 最近 10</span></div>
      <div class="panel-body flush">${table({
        rows: data.items,
        columns: [
          { key: 'created_at', label: '时间', render: (row) => `<span class="tiny">${fmtTime(row.created_at)}</span>` },
          { key: 'actor_email', label: '操作人', className: 'mono' },
          { key: 'actor_role', label: '角色' },
          { key: 'action_label', label: '动作' },
          { key: 'entity', label: '实体', className: 'mono' },
          { key: 'layer', label: '层' },
        ],
        empty: { title: '没有审计记录', hint: '登录或提交变更单后会写入。' },
      })}</div>`;
    panel.querySelectorAll('table.data tbody tr').forEach((row) => row.replaceWith(row));
  } catch (err) {
    panel.innerHTML = `<div class="panel-body"><div class="error-box"><span>${esc(errorMessage(err))}</span></div></div>`;
  }
}

export const changesPage = {
  async render(view, ctx) {
    let data;
    let runs;
    try {
      data = await api.get('/api/change-requests', { status: state.status, limit: 50 });
      runs = await api.get('/api/runs', { limit: 10 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => changesPage.render(view, ctx));
      return;
    }
    const latest = runs.items[0];
    const latestRec = latest?.reconciliation?.counts || {};
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>校验运行</h2>
          <div style="display:flex;gap:10px;align-items:center">
            <span class="meta">${runs.total} 次</span>
            ${can(ctx.user.role, 'run:create') ? '<button class="btn sm accent" id="run-now">跑一次校验</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${
            latest
              ? `<dl class="kv">
                  <dt>运行号</dt><dd class="mono">${esc(latest.run_no)}</dd>
                  <dt>状态</dt><dd>${esc(ENUMS.runStatus[latest.status] || latest.status)}</dd>
                  <dt>输入 / 问题</dt><dd>${esc(latest.input_count)} 条口径 → ${esc(latest.output_count)} 条问题</dd>
                  <dt>三类问题</dt><dd>${typeCounts(latest).map(([label, value]) => `${esc(label)} <b>${esc(value)}</b>`).join(' · ')}</dd>
                  <dt>对账结论</dt><dd>${reconcileSummary(latest)}</dd>
                  <dt>依赖边</dt><dd class="mono">指标 ${esc(latest.metrics?.dep_edges?.metric ?? 0)} · 字段 ${esc(latest.metrics?.dep_edges?.field ?? 0)} · 报表 ${esc(latest.metrics?.dep_edges?.report ?? 0)}</dd>
                  <dt>日志</dt><dd class="tiny">${esc(latest.log_text || '—')}</dd>
                </dl>
                ${
                  latest.status === 'failed'
                    ? '<div class="error-box"><span>最近一次校验未完整完成，业务变更已回滚；上一份可用历史仍然有效，重跑后仍以最近一份有结论运行为对账基线。</span></div>'
                    : ''
                }`
              : emptyState({ title: '还没有校验运行', hint: '点右上角跑一次。' })
          }
        </div>
      </section>
      <section class="panel" id="run-history-panel">
        <div class="panel-head"><h2>校验历史与问题对账</h2><span class="meta">每行可展开问题清单</span></div>
        ${table({
          rows: runs.items,
          columns: [
            { key: 'run_no', label: '运行号', className: 'mono' },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ${row.status === 'failed' ? 'danger' : row.status === 'success' ? 'ok' : 'pending'}">${esc(ENUMS.runStatus[row.status] || row.status)}</span>` },
            { key: 'output_count', label: '问题数', className: 'num' },
            {
              key: 'reconciliation',
              label: '新增 / 解除 / 延续',
              render: (row) => {
                const rec = row.reconciliation?.counts;
                if (!row.reconciliation?.baseline_run_no) return '<span class="muted tiny">无基线</span>';
                return `<span class="mono tiny">${esc(rec?.new ?? 0)} / ${esc(rec?.resolved ?? 0)} / ${esc(rec?.continued ?? 0)}</span>`;
              },
            },
            {
              key: 'types',
              label: '冲突 / 环 / 解析失败',
              className: 'mono tiny',
              render: (row) => `${esc(row.metrics?.conflicts_total ?? 0)} / ${esc(row.metrics?.cycles_total ?? 0)} / ${esc(row.metrics?.parse_error_count ?? 0)}`,
            },
            { key: 'started_at', label: '开始时间', render: (row) => `<span class="tiny">${fmtTime(row.started_at)}</span>` },
          ],
          empty: { title: '没有校验历史', hint: '跑一次校验后，这里会留下可辨认的问题清单。' },
        })}
      </section>
      <section class="panel" id="change-list-panel">
        <div class="panel-head"><h2>变更单</h2><span class="meta">${data.total} 张 · 待审批 ${data.items.filter((item) => item.status === 'pending').length}</span></div>
        ${filterBar('change-filters', [
          { label: '状态', name: 'status', value: state.status, options: [{ value: '', label: '全部状态' }, ...Object.entries(ENUMS.changeStatus).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: data.items,
          columns: [
            { key: 'cr_no', label: '变更单号', className: 'mono' },
            { key: 'target_ref', label: '目标', className: 'mono' },
            { key: 'change_kind_label', label: '类型' },
            {
              key: 'severity',
              label: '严重度',
              render: (row) => `<span class="badge ${badgeTone(row.severity)}">${esc(ENUMS.severity[row.severity])}</span>`,
            },
            { key: 'impact_score', label: '影响分', className: 'num' },
            {
              key: 'impact',
              label: '影响面',
              render: (row) => `<span class="tiny">指标 ${esc((row.impact.metric_codes || []).length)} / 报表 ${esc(row.impact.report_count || 0)}</span>`,
            },
            {
              key: 'status',
              label: '状态',
              render: (row) => `<span class="badge ${changeTone(row.status)}">${esc(row.status_label)}</span>`,
            },
            { key: 'requester_email', label: '申请人', className: 'mono' },
            { key: 'created_at', label: '申请时间', render: (row) => `<span class="tiny">${fmtTime(row.created_at)}</span>` },
          ],
          empty: { title: '没有变更单', hint: '在「冲突与影响面」里把一处冲突变成变更单。' },
        })}
      </section>
      ${
        can(ctx.user.role, 'export:read')
          ? `<section class="panel">
              <div class="panel-head"><h2>导出</h2><span class="meta">UTF-8 BOM · CSV</span></div>
              <div class="panel-body">
                <div class="export-grid">
                  ${EXPORTS.map(
                    ([path, filename, title, hint]) => `<div class="export-card">
                      <b>${esc(title)}</b>
                      <span class="hint">${esc(hint)}</span>
                      <button class="btn sm ghost" data-export="${esc(path)}" data-filename="${esc(filename)}">下载 ${esc(filename)}</button>
                    </div>`,
                  ).join('')}
                </div>
              </div>
            </section>`
          : `<section class="panel"><div class="panel-head"><h2>导出</h2><span class="meta">403</span></div>
              <div class="panel-body"><p class="section-note">只读角色不提供导出（后端会返回真实 403）。如需导出请使用专员或管理员账号。</p></div></section>`
      }
      ${can(ctx.user.role, 'audit:read') ? '<section class="panel" id="audit-panel"></section>' : ''}`;

    view.querySelector('#run-now')?.addEventListener('click', async () => {
      try {
        const run = await api.post('/api/runs', { trigger: 'manual' });
        const rec = run.reconciliation?.counts || {};
        toast(`校验完成：${run.output_count} 条问题（新增 ${rec.new ?? 0} / 解除 ${rec.resolved ?? 0} / 延续 ${rec.continued ?? 0}）`);
        await changesPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelector('#change-filters')?.addEventListener('change', (event) => {
      state.status = event.target.value;
      changesPage.render(view, ctx);
    });
    view.querySelectorAll('[data-export]').forEach((button) => {
      button.addEventListener('click', async () => {
        try {
          await api.download(button.dataset.export, button.dataset.filename);
          toast(`${button.dataset.filename} 已下载`);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
    // 校验历史表点击展开运行详情（问题清单 + 对账）
    const historyPanel = view.querySelector('#run-history-panel');
    historyPanel?.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const runNo = row.querySelector('.mono')?.textContent;
        const run = runs.items.find((entry) => entry.run_no === runNo);
        if (run) openRunDetail(view, ctx, run, runs.items);
      });
    });
    view.querySelector('#change-list-panel')?.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const crNo = row.querySelector('.mono')?.textContent;
        const item = data.items.find((entry) => entry.cr_no === crNo);
        if (item) openChange(view, ctx, item);
      });
    });
    if (can(ctx.user.role, 'audit:read')) await renderAudit(view, ctx, ctx.user);
  },
};

export default changesPage;
