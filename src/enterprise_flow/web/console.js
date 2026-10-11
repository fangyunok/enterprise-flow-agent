/* EnterpriseFlow console — one bundle served by webapp.py and by the deployed site. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (cents) => '¥' + (Number(cents || 0) / 100).toFixed(2);
  const pct = (value) => (Number(value || 0) * 100).toFixed(1) + '%';
  const LABEL = {
    draft: '待确认', confirmed: '已确认', submitted: '已提交', awaiting_confirmation: '等待确认',
    waiting_fields: '待补信息', needs_input: '待补信息', awaiting_input: '待补信息', completed: '已完成',
    failed: '处理失败', running: '执行中', cancelled: '已取消', interrupted: '已中断', created: '已创建'
  };
  const label = (value) => LABEL[value] || value || '等待处理';
  const STATUS_CLASS = { submitted: 'good', confirmed: 'good', waiting_fields: 'warn', awaiting_confirmation: 'warn', failed: 'bad' };

  let me = null;
  let busy = 0;
  let orders = [];
  let currentDraft = null;
  let currentWorkflow = null;
  let streamSource = null;

  function feedback(message, error) {
    const node = $('feedback');
    node.textContent = message;
    node.className = 'feedback ' + (error ? 'error' : 'ok');
    node.style.display = 'block';
    if (!error) setTimeout(() => { if (node.textContent === message) node.style.display = 'none'; }, 4200);
  }

  async function api(path, options) {
    const init = Object.assign({ credentials: 'same-origin' }, options || {});
    if (init.body && typeof init.body !== 'string') {
      init.headers = Object.assign({ 'content-type': 'application/json' }, init.headers);
      init.body = JSON.stringify(init.body);
    }
    const response = await fetch(path, init);
    const type = response.headers.get('content-type') || '';
    const payload = type.indexOf('application/json') >= 0 ? await response.json() : await response.text();
    if (!response.ok) throw new Error((payload && payload.message) || (payload && payload.error) || '请求失败。');
    return payload;
  }

  const entries = (value) => Array.isArray(value) ? value : ((value && (value.items || value.data)) || []);

  function guarded(id, action, lockWhen) {
    const node = $(id);
    if (!node) return;
    node.addEventListener('click', async () => {
      if (busy) return;
      busy++;
      node.disabled = true;
      try { await action(); } catch (error) { feedback(error.message, true); }
      finally { busy--; node.disabled = !!(lockWhen && lockWhen()); }
    });
  }

  const requireLogin = () => { if (!me) throw new Error('请先选择员工身份并切换登录。'); };

  /* ---------------- navigation ---------------- */
  const TITLES = {
    overview: ['运行总览', '从制度依据到业务申请，每一步都可核对。'],
    workbench: ['业务工作台', '订单、制度、草稿与提交的业务闭环。'],
    orchestration: ['流程编排', 'LangGraph 状态图的暂停、恢复与工具调用。'],
    agents: ['多角色分析', '最小权限角色协作与交叉检查。'],
    evaluation: ['检索与评测', '召回、抽取与越权拦截的实测记录。'],
    observability: ['可观测性', '运行 trace、阶段耗时与 token 成本。'],
    audit: ['审计与接口', '审计事件流与服务契约。']
  };

  function show(view) {
    document.querySelectorAll('.view').forEach((node) => node.classList.toggle('active', node.dataset.view === view));
    document.querySelectorAll('#nav button').forEach((node) => node.classList.toggle('active', node.dataset.view === view));
    const title = TITLES[view] || TITLES.overview;
    $('view-title').textContent = title[0];
    $('view-subtitle').textContent = title[1];
    if (view === 'evaluation') loadEvaluation();
    if (view === 'observability') loadObservability();
    if (view === 'audit') loadAudit();
    if (view === 'orchestration') loadOrchestration();
  }

  document.querySelectorAll('#nav button').forEach((node) => {
    node.addEventListener('click', () => show(node.dataset.view));
  });

  /* ---------------- overview ---------------- */
  function renderOverview(data) {
    const kpi = data.kpi || {};
    $('kpi').innerHTML = [
      ['业务任务', kpi.runs, '持久化运行'],
      ['草稿', kpi.drafts, '版本化快照'],
      ['申请编号', kpi.submissions, '幂等提交'],
      ['越权拦截', kpi.blocked, '制度缺口与预检拒绝']
    ].map(([name, value, note]) =>
      `<div class="metric"><strong>${esc(value == null ? '—' : value)}</strong><span>${esc(name)} · ${esc(note)}</span></div>`).join('');

    $('pipeline').innerHTML = (data.pipeline || []).map((node) =>
      `<div class="pnode ${esc(node.state || '')}"><div class="nm">${esc(node.name)}</div><div class="ms">${esc(node.note || '')}</div></div>`).join('');

    $('boundary').innerHTML = (data.boundary || []).map((item) =>
      `<div class="item"><div class="row"><strong>${esc(item.title)}</strong><span class="pill good">${esc(item.owner)}</span></div>
       <div class="sub">${esc(item.detail)}</div></div>`).join('');

    const capability = data.capability || [];
    $('capability-count').textContent = capability.length + ' 项';
    $('capability').innerHTML = capability.map((item) =>
      `<tr><td><strong>${esc(item.name)}</strong></td><td>${esc(item.detail)}</td></tr>`).join('');
    $('engine-pill').textContent = data.mode_label ? data.mode_label : '引擎待命';
  }

  /* ---------------- workbench ---------------- */
  function renderOrders() {
    const target = $('orders');
    target.className = 'scroll';
    target.innerHTML = orders.length ? orders.map((order) =>
      `<div class="item"><label class="check"><input type="checkbox" name="order" value="${esc(order.order_id)}">
       <span>${esc(KIND[order.kind] || order.kind || '订单')} · ${money(order.amount_cents)}
       <div class="sub">${esc(order.order_id)} · ${esc(order.city || '')} · ${esc(order.start_date || '')}${order.end_date ? ' → ' + esc(order.end_date) : ''} · ${esc(order.status || '')}</div></span></label></div>`).join('')
      : '<div class="empty">该员工没有可展示的订单。</div>';
  }

  const KIND = { hotel: '住宿', train: '铁路票据' };

  function sourcesHtml(sources) {
    return (sources || []).map((source) =>
      `<div class="source"><strong>${esc(source.title || source.policy_id || '适用制度')} · ${esc(source.version || source.policy_version || '')} · ${esc(source.clause_id || source.section || '')}</strong>
       <p>${esc(source.content || source.text || '')}</p></div>`).join('');
  }

  const STEPS = ['制度与订单', '核算草稿', '版本确认', '提交结果'];

  function renderSteps(draft) {
    const reached = draft.status === 'submitted' ? 3 : draft.status === 'confirmed' ? 2 : 1;
    $('steps').innerHTML = STEPS.map((name, index) =>
      `<div class="pnode ${index <= reached ? 'done' : ''}"><div class="nm">${esc(name)}</div><div class="ms">${index <= reached ? '已达成' : '待完成'}</div></div>`).join('');
  }

  function renderDraft(draft) {
    currentDraft = draft;
    $('checked').checked = false;
    $('draft-status').textContent = label(draft.status);
    $('draft-status').className = 'pill ' + (STATUS_CLASS[draft.status] || 'flat');
    $('draft').className = '';
    let html = `<div class="muted">${esc(draft.draft_id)} · 版本 ${esc(draft.version)}</div>
      <div class="metrics" style="margin:12px 0">
        <div class="metric"><strong>${money(draft.total_cents)}</strong><span>票据合计</span></div>
        <div class="metric eligible"><strong>${money(draft.eligible_cents)}</strong><span>可申请金额</span></div>
        <div class="metric excess"><strong>${money(draft.excess_cents)}</strong><span>超出制度部分</span></div>
      </div>`;
    html += (draft.items || []).map((item) =>
      `<div class="item" style="margin-bottom:8px"><div class="row"><strong>${esc(KIND[item.kind] || item.order_id)}</strong><span>${money(item.eligible_cents)}</span></div>
       <div class="sub">${esc(item.order_id || '')} · ${esc((item.breakdown || []).length)} 个核算日</div></div>`).join('');
    html += `<div class="caption">成本中心：${esc((draft.input || {}).cost_center || '未填写')} · 备注：${esc((draft.input || {}).notes || '无')}</div>`;
    html += '<div class="divider"><h3>核算依据</h3>' + sourcesHtml(draft.sources) + '</div>';
    if (draft.submission) {
      html += `<div class="item" style="border-color:#c8e9da;background:var(--good-soft)">申请已提交
        <strong>${esc(draft.submission.submission_id)}</strong><div class="sub">再次提交将返回同一申请编号。</div></div>`;
    }
    html += '<div class="divider"><details><summary>查看草稿版本与审计字段</summary><pre class="json">' +
      esc(JSON.stringify(draft, null, 2)) + '</pre></details></div>';
    $('draft').innerHTML = html;
    $('draft-actions').style.display = 'block';
    $('confirm').disabled = draft.status === 'submitted';
    $('edit').disabled = draft.status === 'submitted';
    $('submit').textContent = draft.status === 'submitted' ? '再次获取提交结果' : '提交已确认草稿';
    renderSteps(draft);
    if ((draft.input || {}).cost_center) $('cost-center').value = draft.input.cost_center;
  }

  async function refreshDrafts() {
    const drafts = entries(await api('/api/drafts'));
    $('draft-count').textContent = drafts.length + ' 份';
    $('drafts').className = 'scroll';
    $('drafts').innerHTML = drafts.length ? drafts.map((draft) =>
      `<button class="listbutton" data-draft="${esc(draft.draft_id)}">${esc((draft.input || {}).destination || '业务申请')} · ${money(draft.eligible_cents)}
       <span>${esc(draft.draft_id)} · v${esc(draft.version)} · ${esc(label(draft.status))}</span></button>`).join('')
      : '<div class="empty">暂无草稿。</div>';
    $('drafts').querySelectorAll('[data-draft]').forEach((button) => {
      button.addEventListener('click', async () => {
        try { renderDraft(await api('/api/drafts/' + encodeURIComponent(button.dataset.draft))); }
        catch (error) { feedback(error.message, true); }
      });
    });
    if (currentDraft) {
      try { renderDraft(await api('/api/drafts/' + encodeURIComponent(currentDraft.draft_id))); } catch (_) { /* draft pruned */ }
    }
  }

  async function refreshPreferences() {
    const prefs = entries(await api('/api/preferences'));
    $('preferences').textContent = prefs.length
      ? prefs.map((p) => (p.key === 'cost_center' ? '常用成本中心' : p.key) + '：' + p.value + ' · v' + p.version).join('；')
      : '暂无已确认偏好。';
  }

  async function loadIdentity() {
    me = await api('/api/me');
    $('identity-status').textContent = me.display_name + ' · ' + me.tenant_id;
    $('identity-status').className = 'pill good';
    $('users').value = me.user_id;
    orders = entries(await api('/api/orders'));
    renderOrders();
    const centers = entries(await api('/api/cost-centers'));
    $('cost-center').innerHTML = '<option value="">使用已确认偏好或稍后补充</option>' +
      centers.map((c) => {
        const code = typeof c === 'string' ? c : (c.code || c.cost_center || c.id);
        const name = typeof c === 'string' ? c : (c.label || c.name || code);
        return `<option value="${esc(code)}">${esc(name)}</option>`;
      }).join('');
    await Promise.all([refreshDrafts(), refreshPreferences()]);
    const saved = localStorage.getItem('enterprise-flow-run-' + me.user_id);
    if (saved && !currentWorkflow) {
      try { await showWorkflow(await api('/api/workflows/' + encodeURIComponent(saved))); }
      catch (_) { localStorage.removeItem('enterprise-flow-run-' + me.user_id); }
    }
  }

  async function showWorkflow(record) {
    currentWorkflow = record;
    if (me && record.run_id) localStorage.setItem('enterprise-flow-run-' + me.user_id, record.run_id);
    $('workflow').style.display = 'block';
    $('workflow-status').textContent = label(record.status || record.stage);
    $('workflow-status').className = 'pill ' + (STATUS_CLASS[record.status] || 'flat');
    $('workflow-json').textContent = JSON.stringify(record, null, 2);
    const prompts = (record.pending && record.pending.questions) || record.questions || [];
    $('workflow-detail').textContent = prompts.length ? '待补充：' + prompts.join('、') : '任务状态已保存。';
    const clarifying = record.pending && record.pending.kind === 'clarification';
    $('clarification').style.display = clarifying ? 'block' : 'none';
    if (clarifying) {
      const fields = record.pending.fields || {};
      $('resume-start').value = fields.start_date || '';
      $('resume-end').value = fields.end_date || '';
      $('resume-city').value = fields.destination || '';
      if (fields.cost_center) $('cost-center').value = fields.cost_center;
      document.querySelectorAll('input[name="order"]').forEach((input) => {
        input.checked = (fields.order_ids || []).indexOf(input.value) >= 0;
      });
    }
    const draftId = record.draft_id || (record.result && record.result.draft_id);
    if (record.draft && record.draft.draft_id) renderDraft(record.draft);
    else if (draftId) renderDraft(await api('/api/drafts/' + encodeURIComponent(draftId)));
    await refreshDrafts();
    openStream(record.run_id);
  }

  /* ---------------- orchestration + SSE ---------------- */
  const NODES = [
    ['extract', '字段提取', '模型只输出受限字段'],
    ['collect', '字段补全', '偏好复用与澄清'],
    ['evaluate', '制度检索', '权限过滤后排序'],
    ['draft', '确定核算', '整数分与按晚拆分'],
    ['approval', '人工确认', 'interrupt 持久化暂停'],
    ['confirm', '版本绑定', '版本号与内容摘要'],
    ['submit', '事务提交', '唯一约束与幂等键']
  ];
  const STAGE_INDEX = { created: -1, extract: 0, collect: 1, clarify: 1, evaluate: 2, draft: 3, approval: 4, confirm: 5, submit: 6, submitted: 6, cancelled: 4 };

  function renderGraph(record) {
    const reached = STAGE_INDEX[record ? record.stage : 'created'];
    const waiting = record && record.pending ? (record.pending.kind === 'clarification' ? 1 : 4) : -1;
    $('graph-timeline').innerHTML = NODES.map(([key, name, note], index) => {
      const state = reached == null ? '' : index < reached ? 'done' : index === reached && waiting === index ? 'wait' : index === reached ? 'now' : '';
      return `<div class="tstep ${state}"><span class="dot"></span><div><strong>${esc(name)}</strong>
        <div class="sub muted">${esc(note)}</div></div><span class="ms">${esc(key)}</span></div>`;
    }).join('');
  }

  function openStream(runId) {
    if (streamSource) { streamSource.close(); streamSource = null; }
    const list = $('stream');
    list.innerHTML = '';
    let seen = -1;
    const push = (text, state) => {
      const row = document.createElement('div');
      row.className = 'tstep ' + (state || '');
      row.innerHTML = `<span class="dot"></span><div>${esc(text)}</div><span class="ms">${new Date().toLocaleTimeString('zh-CN', { hour12: false })}</span>`;
      list.appendChild(row);
    };
    try {
      streamSource = new EventSource('/api/runs/' + encodeURIComponent(runId) + '/events');
      $('stream-state').textContent = '已连接';
      streamSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.stage_index > seen) {
          seen = data.stage_index;
          NODES.slice(Math.max(seen - (data.stage_index - seen), 0), data.stage_index + 1).forEach(() => {});
        }
        push(data.message || data.stage, data.terminal ? 'done' : '');
        if (data.terminal) { $('stream-state').textContent = '已完成'; streamSource.close(); streamSource = null; }
      };
      streamSource.onerror = () => { $('stream-state').textContent = '已断开'; if (streamSource) streamSource.close(); streamSource = null; };
    } catch (_) {
      $('stream-state').textContent = '不支持 SSE';
    }
    push('已订阅运行 ' + runId + ' 的阶段事件。');
  }

  async function loadOrchestration() {
    if (!currentWorkflow) {
      if (me) {
        const runs = entries(await api('/api/runs'));
        if (runs.length) { await showWorkflow(runs[0]); return; }
      }
      renderGraph(null);
      return;
    }
    renderGraph(currentWorkflow);
    const events = (currentWorkflow.result && currentWorkflow.result.tool_events) || currentWorkflow.tool_events || [];
    $('tool-count').textContent = events.length + ' 次';
    $('tool-events').innerHTML = events.length ? events.map((event, index) =>
      `<tr><td class="num">${index + 1}</td><td><code>${esc(event.tool)}</code></td><td class="num">${esc(event.duration_ms)} ms</td>
       <td>${esc(TOOL_NOTE[event.tool] || '')}</td></tr>`).join('') : '<tr><td colspan="4" class="muted">还没有工具调用。</td></tr>';
  }

  const TOOL_NOTE = {
    get_my_orders: '只返回绑定身份的订单',
    get_preferences: '读取本人明确保存的偏好',
    search_policy: '按租户、部门与生效日过滤后排序',
    calculate_expense: '整数分核算，逐日匹配制度',
    create_expense_draft: '幂等创建版本化草稿',
    confirm_expense_draft: '把人工决定绑定到版本与摘要',
    submit_expense: '事务提交，幂等键去重'
  };

  /* ---------------- agents ---------------- */
  function renderProposal(p) {
    const conflicts = (p.conflicts || []).map((c) =>
      `<div class="item" style="border-color:${c.severity === 'blocking' ? '#f2dfbb' : 'var(--line-soft)'};background:${c.severity === 'blocking' ? 'var(--warn-soft)' : '#fdfdff'};margin-bottom:8px">
       <div class="row"><strong>${esc(c.code)}</strong><span class="pill ${c.severity === 'blocking' ? 'warn' : 'flat'}">${esc(c.severity)}</span></div>
       <div class="sub">${esc(c.detail)}</div></div>`).join('');
    const findings = (p.findings || []).map((f) =>
      `<div class="item" style="margin-bottom:8px"><div class="row"><strong>${esc(f.agent)}</strong>
       <span class="pill ${f.ok ? 'good' : 'warn'}">${esc(f.tool_calls)} 次工具调用</span></div>
       <div>${esc(f.conclusion)}</div></div>`).join('');
    const c = p.calculation;
    const summary = c ? `<div class="metrics" style="margin-bottom:14px">
      <div class="metric"><strong>${money(c.total_cents)}</strong><span>票据合计</span></div>
      <div class="metric eligible"><strong>${money(c.eligible_cents)}</strong><span>可申请金额</span></div>
      <div class="metric excess"><strong>${money(c.excess_cents)}</strong><span>超出制度部分</span></div></div>` : '';
    $('agent-result').innerHTML = `<div class="card"><header><h2>角色结论与交叉检查</h2>
      <span class="pill ${p.blocked ? 'warn' : 'good'}">${p.blocked ? '存在阻塞项' : '未阻塞'}</span></header>
      ${summary}<h3>角色结论</h3><div style="margin-top:8px">${findings || '<div class="empty">没有角色产出结论。</div>'}</div>
      <div class="divider"><h3>交叉检查</h3><div style="margin-top:8px">${conflicts || '<div class="caption">未发现冲突。</div>'}</div></div>
      <p class="caption">${esc(p.next_action || '')} · 工具调用 ${esc(p.tool_calls)} 次 · 只读 ${esc(p.read_only)} · 业务写入 ${esc(p.business_effects)}</p></div>`;
  }

  function renderPlan(result) {
    const steps = (result.steps || []).map((step) =>
      `<div class="item" style="margin-bottom:8px"><div class="row"><strong>步骤 ${esc(step.observation.step)} · ${esc(step.observation.tool)}</strong>
       <span class="pill ${step.observation.ok ? 'good' : 'bad'}">${esc(step.observation.duration_ms)} ms</span></div>
       <div>${esc(step.thought)}</div>
       <div class="sub">观察摘要：${esc(JSON.stringify(step.observation.summary || {}))}</div></div>`).join('');
    $('agent-result').innerHTML = `<div class="card"><header><h2>只读规划轨迹</h2>
      <span class="pill good">${esc(result.stop_reason)}</span></header>
      ${steps || '<div class="empty">规划未执行任何工具。</div>'}
      <p class="caption">工具调用 ${esc(result.tool_calls)} 次 · 回注上下文 ${esc(result.context_chars)} 字符 · 被拦截工具 ${esc((result.blocked_tools || []).join(',') || '无')} · 只读 ${esc(result.read_only)} · 业务写入 ${esc(result.business_effects)}</p></div>`;
  }

  /* ---------------- evaluation ---------------- */
  async function loadEvaluation() {
    try {
      const data = await api('/api/evaluation');
      $('retrieval-table').innerHTML = (data.retrieval || []).map((row) =>
        `<tr><td><strong>${esc(row.label)}</strong></td>
         <td><div class="bar good"><i style="width:${(row.recall_at3 * 100).toFixed(1)}%"></i></div><span class="muted">${pct(row.recall_at3)}</span></td>
         <td><div class="bar good"><i style="width:${(row.recall_at5 * 100).toFixed(1)}%"></i></div><span class="muted">${pct(row.recall_at5)}</span></td>
         <td class="num">${esc(row.leakage)}</td></tr>`).join('');
      $('model-eval').innerHTML = (data.model || []).map((row) =>
        `<div class="item"><div class="row"><strong>${esc(row.label)}</strong><span class="pill flat">${esc(row.value)}</span></div>
         <div class="sub">${esc(row.note || '')}</div></div>`).join('');
      $('planner-eval').innerHTML = (data.planner || []).map((row) =>
        `<div class="item"><div class="row"><strong>${esc(row.label)}</strong><span class="pill ${row.good ? 'good' : 'flat'}">${esc(row.value)}</span></div>
         <div class="sub">${esc(row.note || '')}</div></div>`).join('');
      $('eval-json').textContent = JSON.stringify(data.raw || data, null, 2);
    } catch (error) { feedback(error.message, true); }
  }

  /* ---------------- observability ---------------- */
  async function loadObservability() {
    try {
      const data = await api('/api/trace');
      const summary = data.summary || {};
      $('obs-kpi').innerHTML = [
        ['Span 数', summary.span_count, '本次运行决策点'],
        ['失败 Span', summary.failed_spans, '异常决策点'],
        ['模型调用', (summary.model_usage || {}).model_calls, '含未返回用量调用'],
        ['估算成本', '$' + ((summary.model_usage || {}).cost_usd || 0).toFixed(6), '费率由配置注入']
      ].map(([name, value, note]) =>
        `<div class="metric"><strong>${esc(value == null ? '—' : value)}</strong><span>${esc(name)} · ${esc(note)}</span></div>`).join('');
      const spans = summary.spans || [];
      $('trace-spans').textContent = spans.length + ' span';
      const max = Math.max(1, ...spans.map((s) => s.duration_ms || 0));
      $('trace-gantt').innerHTML = spans.length ? spans.map((span) =>
        `<div class="gantt-row"><div>${esc(span.name)}</div>
         <div class="track"><div class="fill ${esc(span.category || '')}" style="width:${Math.max(2, ((span.duration_ms || 0) / max) * 100).toFixed(1)}%"></div></div>
         <span class="ms">${esc(span.duration_ms)}ms</span></div>`).join('') : '<div class="empty">还没有 span；发起一次任务后自动生成。</div>';
      const stageMs = summary.stage_ms || {};
      const stageMax = Math.max(1, ...Object.values(stageMs));
      $('stage-ms').innerHTML = Object.keys(stageMs).length ? Object.keys(stageMs).map((key) =>
        `<div><div class="row"><span>${esc(key)}</span><span class="muted">${esc(stageMs[key])} ms</span></div>
         <div class="bar"><i style="width:${((stageMs[key] / stageMax) * 100).toFixed(1)}%"></i></div></div>`).join('')
        : '<div class="empty">还没有阶段耗时记录。</div>';
      const usage = summary.model_usage || {};
      $('cost-pill').textContent = '$' + (usage.cost_usd || 0).toFixed(6);
      $('usage').innerHTML = [
        ['模型调用次数', usage.model_calls],
        ['输入 token', usage.input_tokens],
        ['输出 token', usage.output_tokens],
        ['未返回用量调用', usage.uncounted_calls || 0]
      ].map(([name, value]) => `<div class="row"><span class="muted">${esc(name)}</span><strong>${esc(value == null ? '—' : value)}</strong></div>`).join('');
    } catch (error) { feedback(error.message, true); }
    try { $('metrics-view').textContent = await api('/api/metrics'); }
    catch (error) { $('metrics-view').textContent = '指标不可用：' + error.message; }
  }

  /* ---------------- audit ---------------- */
  async function loadAudit() {
    try {
      const events = entries(await api('/api/audit'));
      $('audit-count').textContent = events.length + ' 条';
      $('audit-list').innerHTML = events.length ? events.map((event) =>
        `<div class="item"><div class="row"><strong>${esc(event.action)}</strong><span class="pill flat">${esc(event.at_utc || '')}</span></div>
         <div class="sub">${esc(event.resource_id || '')} · ${esc(JSON.stringify(event.details || {}))}</div></div>`).join('')
        : '<div class="empty">还没有审计事件。</div>';
    } catch (error) { feedback(error.message, true); }
    try {
      const contract = await api('/api/contract');
      $('contract').innerHTML = (contract.endpoints || []).map((item) =>
        `<div class="item"><div class="row"><code>${esc(item.method)} ${esc(item.path)}</code><span class="pill flat">${esc(item.tag || '')}</span></div>
         <div class="sub">${esc(item.summary || '')}</div></div>`).join('');
      $('health-view').textContent = JSON.stringify(contract.health || {}, null, 2);
    } catch (error) { feedback('服务契约不可用：' + error.message, true); }
  }

  /* ---------------- actions ---------------- */
  guarded('login', async () => {
    await api('/api/login', { method: 'POST', body: { user_id: $('users').value } });
    currentDraft = null; currentWorkflow = null;
    $('workflow').style.display = 'none';
    $('draft-actions').style.display = 'none';
    $('draft').className = 'empty';
    $('draft').textContent = '选择草稿后查看金额、来源和版本。';
    await loadIdentity();
    feedback('已切换身份。所有订单、草稿和任务都按当前员工过滤。');
  });

  guarded('logout', async () => { await api('/api/logout', { method: 'POST', body: {} }); location.reload(); });

  guarded('refresh', async () => {
    requireLogin();
    await loadIdentity();
    if (currentWorkflow) await showWorkflow(await api('/api/workflows/' + encodeURIComponent(currentWorkflow.run_id)));
    feedback('已刷新持久业务状态。');
  });

  guarded('start', async () => {
    requireLogin();
    await showWorkflow(await api('/api/workflows', { method: 'POST', body: { message: $('message').value, request_id: crypto.randomUUID() } }));
    feedback('任务已执行，核算与提交结果以业务服务记录为准。');
  });

  guarded('resume', async () => {
    requireLogin();
    if (!currentWorkflow) throw new Error('没有可继续的任务。');
    const fields = Object.assign({}, (currentWorkflow.pending && currentWorkflow.pending.fields) || {});
    const selected = Array.from(document.querySelectorAll('input[name="order"]:checked')).map((i) => i.value);
    if (selected.length) fields.order_ids = selected;
    if ($('cost-center').value) fields.cost_center = $('cost-center').value;
    if ($('resume-start').value) fields.start_date = $('resume-start').value;
    if ($('resume-end').value) fields.end_date = $('resume-end').value;
    if ($('resume-city').value.trim()) fields.destination = $('resume-city').value.trim();
    await showWorkflow(await api('/api/workflows/' + encodeURIComponent(currentWorkflow.run_id) + '/resume',
      { method: 'POST', body: { decision: { action: 'provide_fields', fields } } }));
  });

  guarded('create-draft', async () => {
    requireLogin();
    const ids = Array.from(document.querySelectorAll('input[name="order"]:checked')).map((i) => i.value);
    if (!ids.length) throw new Error('请先勾选本人订单。');
    renderDraft(await api('/api/drafts', { method: 'POST', body: { order_ids: ids, cost_center: $('cost-center').value || null, notes: $('notes').value } }));
    await refreshDrafts();
    feedback('已生成版本化草稿，请核对费用与制度。');
  });

  const workflowOwns = (draft) => currentWorkflow && currentWorkflow.pending &&
    currentWorkflow.pending.kind === 'approval' &&
    ((currentWorkflow.draft_id || (currentWorkflow.draft && currentWorkflow.draft.draft_id) || currentWorkflow.pending.draft_id) === draft.draft_id);

  guarded('confirm', async () => {
    requireLogin();
    if (!currentDraft || !$('checked').checked) throw new Error('请核对并勾选当前版本确认声明。');
    if (workflowOwns(currentDraft)) {
      await showWorkflow(await api('/api/workflows/' + encodeURIComponent(currentWorkflow.run_id) + '/resume',
        { method: 'POST', body: { decision: { action: 'approve', expected_version: currentDraft.version, expected_hash: currentDraft.content_hash } } }));
      feedback('当前版本已确认，申请记录已生成。');
    } else {
      renderDraft(await api('/api/drafts/' + encodeURIComponent(currentDraft.draft_id) + '/confirm',
        { method: 'POST', body: { expected_version: currentDraft.version, expected_hash: currentDraft.content_hash } }));
      await refreshDrafts();
      feedback('当前版本已确认，可以提交。');
    }
  }, () => currentDraft && currentDraft.status === 'submitted');

  guarded('submit', async () => {
    requireLogin();
    if (!currentDraft) throw new Error('请先选择草稿。');
    const result = await api('/api/drafts/' + encodeURIComponent(currentDraft.draft_id) + '/submit',
      { method: 'POST', body: { expected_version: currentDraft.version, idempotency_key: 'console-' + currentDraft.draft_id + '-v' + currentDraft.version } });
    renderDraft(await api('/api/drafts/' + encodeURIComponent(currentDraft.draft_id)));
    await refreshDrafts();
    feedback('申请记录已入库：' + (result.submission_id || (currentDraft.submission && currentDraft.submission.submission_id) || '已返回业务编号'));
  });

  guarded('edit', async () => {
    requireLogin();
    if (!currentDraft) throw new Error('请先选择草稿。');
    const wasWorkflow = workflowOwns(currentDraft);
    renderDraft(await api('/api/drafts/' + encodeURIComponent(currentDraft.draft_id), {
      method: 'PATCH',
      body: { expected_version: currentDraft.version, order_ids: currentDraft.input.order_ids, cost_center: $('cost-center').value || null, notes: $('notes').value }
    }));
    if (wasWorkflow) await showWorkflow(await api('/api/workflows/' + encodeURIComponent(currentWorkflow.run_id) + '/resume', { method: 'POST', body: { decision: { action: 'refresh' } } }));
    await refreshDrafts();
    feedback('已更新草稿；原确认失效，请核对新版本。');
  }, () => currentDraft && currentDraft.status === 'submitted');

  guarded('search', async () => {
    requireLogin();
    const policies = entries(await api('/api/policies?query=' + encodeURIComponent($('policy-query').value) + '&trip_date=' + encodeURIComponent($('policy-date').value)));
    $('policies').innerHTML = policies.length ? sourcesHtml(policies) : '<div class="empty">当前权限和日期下没有匹配制度。</div>';
  });

  guarded('policy-answer', async () => {
    requireLogin();
    const result = await api('/api/policy-answers', { method: 'POST', body: { question: $('policy-question').value, trip_date: $('policy-date').value } });
    $('policy-answer-result').innerHTML = '<p>' + esc(result.answer || result.message || '资料不足，无法回答。') + '</p>' +
      sourcesHtml(result.sources || []) +
      '<p class="caption">' + (result.model_used ? '模型生成，语义支持待人工核对。' : '直接展示条款，未调用大模型。') + '</p>';
  });

  guarded('save-pref', async () => {
    requireLogin();
    if (!$('cost-center').value) throw new Error('请先选择要保存的成本中心。');
    await api('/api/preferences', { method: 'PATCH', body: { key: 'cost_center', value: $('cost-center').value, confirmed: true } });
    await refreshPreferences();
    feedback('已保存用户明确确认的成本中心偏好。');
  });

  guarded('delete-pref', async () => {
    requireLogin();
    await api('/api/preferences', { method: 'DELETE', body: { key: 'cost_center' } });
    await refreshPreferences();
    feedback('已删除成本中心偏好。');
  });

  guarded('agent-run', async () => {
    requireLogin();
    const result = await api('/api/agent-proposals', { method: 'POST', body: { message: $('agent-message').value } });
    renderProposal(result);
    feedback(result.blocked ? '分析发现问题，请按结论补充信息或人工核查。' : '分析完成：结论仅供核对，创建与提交仍需人工确认。');
  });

  guarded('plan-run', async () => {
    requireLogin();
    renderPlan(await api('/api/plan', { method: 'POST', body: { message: $('agent-message').value } }));
    feedback('规划轨迹已生成，未产生任何业务写入。');
  });

  /* ---------------- boot ---------------- */
  (async () => {
    try {
      const overview = await api('/api/overview');
      renderOverview(overview);
      const users = entries(await api('/api/demo-users'));
      $('users').innerHTML = users.map((u) =>
        `<option value="${esc(u.user_id)}">${esc(u.display_name)} · ${esc(u.tenant_id)} / ${esc(u.department_id)}</option>`).join('');
      try { await loadIdentity(); } catch (_) { me = null; }
    } catch (error) { feedback(error.message, true); }
  })();
})();
