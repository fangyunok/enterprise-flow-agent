"""Local EnterpriseFlow workbench with signed demo identity and guarded JSON APIs.

The identity picker switches between the built-in principals used for permission-isolation checks.
All resource access still passes a Principal reconstructed by the server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .service import DomainError, EnterpriseService

_COOKIE = "enterprise_flow_demo"
_SESSION_SECONDS = 8 * 60 * 60
_MAX_BODY = 32768
_DRAFT_FIELDS = {"order_ids", "cost_center", "notes", "start_date", "end_date", "destination"}


class _HTTPProblem(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code, self.message, self.status_code = code, message, status_code


class SameOriginMiddleware:
    """Reject cross-origin browser mutations; allow local CLI requests."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] in {"POST", "PATCH", "PUT", "DELETE"}:
            request = Request(scope)
            allowed = request.headers.get("sec-fetch-site", "").lower() not in {"cross-site", "same-site"}
            origin = request.headers.get("origin")
            if origin is not None:
                try:
                    source, target = urlsplit(origin), urlsplit(str(request.url))
                    def port(value):
                        return value.port or (443 if value.scheme == "https" else 80)
                    allowed = allowed and source.scheme in {"http", "https"}
                    allowed = allowed and not source.username and not source.password
                    allowed = allowed and not source.path and not source.query and not source.fragment
                    allowed = allowed and (source.scheme, source.hostname, port(source)) == (target.scheme, target.hostname, port(target))
                except ValueError:
                    allowed = False
            if not allowed:
                await JSONResponse({"error": "origin_rejected", "message": "请从本机工作台提交请求。"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _SessionSigner:
    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def sign(self, user_id: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"user_id": user_id, "expires": int(time.time()) + _SESSION_SECONDS}, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def read(self, token: str | None) -> str:
        if not token or len(token) > 2048:
            raise _HTTPProblem("authentication_required", "请先选择员工身份。", 401)
        try:
            payload, supplied = token.split(".", 1)
            expected = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(decoded, dict) or not isinstance(decoded.get("user_id"), str):
                raise ValueError("payload")
            expires = decoded.get("expires")
            if type(expires) is not int or expires <= time.time():
                raise ValueError("expiry")
            return decoded["user_id"]
        except (ValueError, UnicodeError, TypeError, json.JSONDecodeError) as exc:
            raise _HTTPProblem("authentication_required", "登录会话已失效，请重新选择员工。", 401) from exc


async def _json_body(request: Request, *, optional: bool = False) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > _MAX_BODY:
                raise _HTTPProblem("body_too_large", "请求内容过大。", 413)
        except ValueError as exc:
            raise _HTTPProblem("invalid_body", "请求长度不合法。") from exc
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > _MAX_BODY:
            raise _HTTPProblem("body_too_large", "请求内容过大。", 413)
    if optional and not received:
        return {}
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise _HTTPProblem("unsupported_media_type", "请使用 JSON 请求。", 415)
    try:
        result = json.loads(received)
    except (ValueError, UnicodeError) as exc:
        raise _HTTPProblem("invalid_json", "JSON 格式不正确。") from exc
    if not isinstance(result, dict):
        raise _HTTPProblem("invalid_body", "请求必须为 JSON 对象。")
    return result


def _fields(payload: dict, allowed: set[str], required: set[str] | None = None) -> None:
    if set(payload) - allowed:
        raise _HTTPProblem("unexpected_fields", "请求包含不支持的字段；身份由服务端会话确定。")
    if required and required - set(payload):
        raise _HTTPProblem("missing_fields", "请求缺少必要字段。")


def _version(payload: dict) -> int:
    value = payload.get("expected_version")
    if type(value) is not int or value < 1:
        raise _HTTPProblem("invalid_version", "请提供当前草稿版本。")
    return value


def _string(payload: dict, name: str, maximum: int = 4000) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _HTTPProblem("invalid_field", f"字段 {name} 不能为空或超过长度限制。")
    return value.strip()


def _encode(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _response(value, status_code: int = 200) -> JSONResponse:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=_encode))
    return JSONResponse(normalized, status_code=status_code, headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>EnterpriseFlow · 企业业务工作流平台</title>
<style>
:root{--bg:#f4f6f9;--paper:#fff;--ink:#17253a;--muted:#66748a;--line:#e4e9f0;--blue:#315fec;--green:#177c5b;--amber:#a66813}
*{box-sizing:border-box}body{margin:0;font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--ink)}button,input,textarea,select{font:inherit}button{cursor:pointer}a{color:var(--blue)}.shell{display:grid;grid-template-columns:240px 1fr;min-height:100vh}.sidebar{background:#14243b;color:#cfdaeb;padding:28px 22px;display:flex;flex-direction:column}.brand{color:#fff;font-size:23px;letter-spacing:-.8px;font-weight:750}.brand span{color:#77a4ff}.tagline{font-size:12px;color:#8fa3bf;margin-top:5px}.navitem{margin-top:32px;padding:11px 14px;border-radius:8px;background:#263c5b;color:#fff}.side-note{margin-top:auto;font-size:12px;color:#a9bad1;border-top:1px solid #32455e;padding-top:20px;line-height:1.9}.main{max-width:1440px;width:100%;margin:auto;padding:26px 34px 40px}.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:26px;gap:16px}.eyebrow{font-size:11px;font-weight:700;letter-spacing:1.2px;color:var(--blue)}h1{font-size:28px;letter-spacing:-.8px;margin:4px 0}h2{font-size:17px;margin:0 0 14px}h3{font-size:14px;margin:0 0 8px}.muted{color:var(--muted);font-size:13px}.pill{display:inline-block;font-size:11px;border:1px solid #d8e2fa;color:#365baf;background:#edf2ff;padding:4px 9px;border-radius:16px}.pill.good{background:#eaf7f0;color:var(--green);border-color:#c8e9da}.pill.warn{background:#fff6e9;color:var(--amber);border-color:#f2dfbb}.notice{padding:11px 15px;background:#fff8e8;border:1px solid #efddb1;border-radius:9px;color:#795722;font-size:12px;margin-bottom:22px}.grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:20px;align-items:start}.card{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:20px;box-shadow:0 4px 18px #18345104}.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.stack{display:grid;gap:10px}.fieldrow{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:block;color:#465973;font-size:12px;font-weight:600;margin:12px 0 5px}textarea,input,select{width:100%;padding:10px 11px;border:1px solid #ccd7e5;border-radius:7px;background:#fff;color:var(--ink)}textarea{min-height:108px;resize:vertical}input:focus,textarea:focus,select:focus{outline:3px solid #315fec1b;border-color:var(--blue)}button{border:0;border-radius:7px;padding:10px 14px;background:var(--blue);color:#fff;font-weight:600}button.secondary{background:#f0f3f8;color:#435874;border:1px solid #dbe3ee}button.small{font-size:12px;padding:6px 10px}button:disabled{opacity:.48;cursor:wait}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:16px}.identity{display:flex;align-items:center;gap:8px;max-width:430px}.identity select{font-size:12px;min-width:220px}.identity button{white-space:nowrap}.empty{padding:22px 12px;border:1px dashed #d4deeb;border-radius:8px;text-align:center;color:var(--muted);font-size:12px}.steps{display:flex;gap:5px;margin:18px 0}.step{flex:1;font-size:11px;text-align:center;border-radius:5px;background:#f0f3f8;padding:8px 3px;color:#6a7b92}.step.active{background:#eaf0ff;color:#315fec}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0}.metric{background:#f5f7fb;border-radius:8px;padding:12px}.metric strong{font-size:21px;display:block;font-variant-numeric:tabular-nums}.metric span{font-size:11px;color:var(--muted)}.metric.eligible{background:#ecf7f2;color:var(--green)}.metric.excess{background:#fff6e9;color:var(--amber)}.item{border-top:1px solid var(--line);padding:12px 0}.item:last-child{padding-bottom:0}.item .sub{font-size:11px;color:var(--muted);margin-top:3px}.source{background:#f7f9fc;border-left:3px solid #9db8f9;padding:10px 12px;font-size:12px;margin:10px 0}.source strong{display:block;margin-bottom:5px}.source p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.check{display:flex;gap:9px;align-items:flex-start;margin-top:15px;font-size:12px;color:#4b607a}.check input{width:16px;margin-top:3px;flex:none}.listbutton{display:block;width:100%;text-align:left;background:#fff;color:var(--ink);border-bottom:1px solid var(--line);border-radius:0;padding:12px 0;font-weight:400}.listbutton:hover{color:var(--blue)}.listbutton span{display:block;color:var(--muted);font-size:11px;margin-top:4px}.scroll{max-height:340px;overflow:auto}.checkbox-item{display:flex;gap:9px;align-items:flex-start;font-weight:400;margin:0}.checkbox-item input{width:15px;margin-top:4px;flex:none}.feedback{display:none;margin:12px 0;padding:10px 12px;border-radius:8px;background:#eaf7f0;color:var(--green);font-size:12px;white-space:pre-wrap}.feedback.error{background:#fff0ee;color:#a24135}details{margin-top:12px}summary{font-size:12px;color:var(--muted);cursor:pointer}pre{max-height:350px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5fa;padding:12px;border-radius:8px;font-size:11px}footer{color:#7b8ca4;font-size:11px;margin-top:22px}.hide{display:none!important}.caption{font-size:11px;color:var(--muted);margin:8px 0}.submission{border:1px solid #bfe2d1;background:#eef8f3;border-radius:8px;padding:16px;color:#24644c}.submission strong{display:block;font-size:17px;overflow-wrap:anywhere}.divider{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}
@media(max-width:1100px){.shell{grid-template-columns:190px 1fr}.sidebar{padding:24px 18px}.main{padding:24px}.grid{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}}@media(max-width:650px){.shell{display:block}.sidebar{padding:16px 20px}.brand{font-size:21px}.navitem,.side-note,.tagline{display:none}.main{padding:20px 14px}.card{padding:18px}.identity{flex-wrap:wrap}.fieldrow{grid-template-columns:1fr}.summary{gap:5px}.metric{padding:10px}.metric strong{font-size:17px}h1{font-size:25px}}
</style></head><body>
<div class="shell"><aside class="sidebar"><div class="brand">Enterprise<span>Flow</span></div><div class="tagline">企业业务工作流平台</div><div class="navitem">◈ &nbsp; 业务工作台</div><div class="side-note">企业业务场景 · 订单与制度对照<br>确定性核算与制度引用<br>版本确认 · 幂等提交<br>任务状态持久保存</div></aside>
<main class="main"><header class="topbar"><div><div class="eyebrow">WORKFLOW OPERATIONS</div><h1>业务工作台</h1><div class="muted">从制度依据到业务申请，每一步都可核对。</div></div><div class="identity"><select id="users" aria-label="员工身份"></select><button id="login" class="small">切换身份</button><button id="logout" class="small secondary hide">退出</button></div></header>
<div class="notice">本地部署 · 内置企业、制度与订单数据集，可直接核对每一步依据。身份切换用于验证权限隔离。当前执行模式：<strong>__MODE_LABEL__</strong>。</div>
<div id="feedback" class="feedback" role="status"></div>
<div class="grid"><section>
<div class="card"><div class="row"><h2>发起业务任务</h2><span id="identity-status" class="pill">尚未选择员工</span></div><textarea id="message" aria-label="业务需求">10 月 9 日入住、11 日退房，广州住宿共 860 元，车票 430 元。请用已有订单帮我准备报销。</textarea><div class="actions"><button id="start">分析并准备草稿</button><button id="refresh" class="secondary">刷新任务和草稿</button></div><p class="caption">模型模式负责提取与澄清；金额、权限和提交状态始终由业务服务校验。固定流程模式不会调用大模型。</p><div id="workflow" class="hide divider"><div class="row"><h3>当前任务</h3><span id="workflow-status" class="pill"></span></div><div id="workflow-detail"></div><div id="clarification" class="hide"><p class="caption">勾选下方本人订单，并补充需要的业务字段。</p><div class="fieldrow"><div><label for="resume-start">开始日期</label><input id="resume-start" type="date"></div><div><label for="resume-end">结束日期</label><input id="resume-end" type="date"></div></div><label for="resume-city">目的地</label><input id="resume-city" placeholder="例如：广州"><button id="resume" class="secondary">使用补充信息继续任务</button></div><details><summary>查看持久任务记录</summary><pre id="workflow-json"></pre></details></div></div>
<div class="card"><div class="row"><h2>本人的订单</h2><span class="pill">服务端身份过滤</span></div><div id="orders" class="empty">选择员工身份后加载本人订单。</div><div class="fieldrow"><div><label for="cost-center">本次成本中心</label><select id="cost-center"><option value="">使用已确认偏好或稍后补充</option></select></div><div><label for="notes">业务备注</label><input id="notes" placeholder="可选备注" maxlength="500"></div></div><div class="actions"><button id="create-draft" class="secondary">用勾选订单创建草稿</button></div><p class="caption">这条入口直接验证业务闭环，可在模型服务不可用时使用。</p></div>
<div class="card"><h2>制度检索</h2><div class="fieldrow"><div><label for="policy-query">检索词</label><input id="policy-query" value="住宿 车票"></div><div><label for="policy-date">业务日期</label><input id="policy-date" type="date" value="2026-10-09"></div></div><div class="actions"><button id="search" class="secondary">检索适用制度</button></div><div id="policies"></div><div class="divider"><h3>制度问答</h3><label for="policy-question">业务问题</label><textarea id="policy-question" style="min-height:76px">广州住宿的报销限额是什么？超额部分如何处理？</textarea><div class="actions"><button id="policy-answer" class="secondary">依据适用制度回答</button></div><p class="caption">固定流程模式直接展示匹配条款；模型模式生成带引用的回答，仍需人工核对。</p><div id="policy-answer-result"></div></div></div>
<div class="card"><div class="row"><h2>多角色只读分析</h2><span class="pill">不产生业务写入</span></div><p class="muted">订单核对、条款检索与确定性核算由三个受限角色分别完成，结论交叉检查后返回。</p><label for="agent-message">分析任务</label><textarea id="agent-message" style="min-height:76px">我于 2026-10-09 至 2026-10-11 去广州出差，请处理 alice-hotel-001 和 alice-train-001，成本中心 CC-ALPHA-OPS。</textarea><div class="actions"><button id="agent-run" class="secondary">运行多角色分析</button><button id="plan-run" class="small secondary">查看规划轨迹</button></div><p class="caption">规划循环只允许查询与核算工具；任何角色都不能创建、确认或提交草稿。</p><div id="agent-result"></div></div>
<div class="card"><h2>已确认偏好</h2><p class="muted">成本中心偏好可跨任务复用，本次明确输入优先。</p><div id="preferences" class="caption">暂无已确认偏好。</div><div class="actions"><button id="save-pref" class="small secondary">将所选成本中心保存为偏好</button><button id="delete-pref" class="small secondary">删除偏好</button></div></div>
</section><section>
<div class="card"><div class="row"><h2>草稿与申请</h2><span id="draft-count" class="pill">0 份</span></div><div id="drafts" class="empty">暂无草稿。</div></div>
<div class="card"><div class="row"><h2>核对与确认</h2><span id="draft-status" class="pill">等待草稿</span></div><div class="steps"><div class="step active">制度与订单</div><div class="step">核算草稿</div><div class="step">版本确认</div><div class="step">提交结果</div></div><div id="draft" class="empty">选择草稿后查看金额、来源和版本。</div><div id="draft-actions" class="hide"><label class="check"><input type="checkbox" id="checked"><span>我已核对订单、费用和适用制度，同意确认当前显示的草稿版本。</span></label><div class="actions"><button id="confirm">确认当前版本</button><button id="submit" class="secondary">提交已确认草稿</button><button id="edit" class="secondary">更新成本中心与备注</button></div><p class="caption">确认绑定版本和内容摘要。修改草稿会使旧确认失效；重复提交不会重复生成申请。</p></div></div>
</section></div><footer>EnterpriseFlow · 本地部署工作台 · 提交生成带版本与内容摘要的申请记录，并写入审计事件。</footer></main></div>
<script>
const $=id=>document.getElementById(id), esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let me=null,currentDraft=null,currentWorkflow=null,orders=[],busy=0;
const money=value=>'¥'+(Number(value||0)/100).toFixed(2), label=value=>({draft:'待确认',confirmed:'已确认',submitted:'已提交',awaiting_confirmation:'等待确认',needs_input:'待补信息',awaiting_input:'待补信息',waiting_fields:'待补信息',completed:'已完成',failed:'处理失败',running:'执行中'}[value]||value||'等待处理');
function feedback(message,error=false){$('feedback').textContent=message;$('feedback').className='feedback'+(error?' error':'');$('feedback').style.display='block';}
async function api(path,options={}){const init={credentials:'same-origin',...options};if(init.body&&typeof init.body!=='string'){init.headers={'content-type':'application/json',...init.headers};init.body=JSON.stringify(init.body);}const response=await fetch(path,init);let data;try{data=await response.json();}catch{throw Error('服务返回了无法解析的内容。');}if(!response.ok)throw Error(data.message||data.error||'请求失败。');return data;}
function guarded(id,action){$(id).addEventListener('click',async()=>{if(busy)return;busy++;$(id).disabled=true;try{await action();}catch(error){feedback(error.message,true);}finally{busy--;$(id).disabled=['confirm','edit'].includes(id)&&currentDraft?.status==='submitted';}});}
function entries(value){return Array.isArray(value)?value:(value?.items||value?.data||[]);}
function requireLogin(){if(!me)throw Error('请先选择员工身份并切换登录。');}
function renderProposal(p){const conflicts=(p.conflicts||[]).map(c=>'<div class="item"><div class="row"><strong>'+esc(c.code)+'</strong><span class="pill '+(c.severity==='blocking'?'warn':'')+'">'+esc(c.severity)+'</span></div><div class="sub">'+esc(c.detail)+'</div></div>').join('');const findings=(p.findings||[]).map(f=>'<div class="item"><div class="row"><strong>'+esc(f.agent)+'</strong><span class="pill '+(f.ok?'good':'warn')+'">'+esc(f.tool_calls)+' 次工具调用</span></div><div>'+esc(f.conclusion)+'</div></div>').join('');const c=p.calculation;const summary=c?'<div class="summary"><div class="metric"><strong>'+money(c.total_cents)+'</strong><span>票据合计</span></div><div class="metric eligible"><strong>'+money(c.eligible_cents)+'</strong><span>可申请金额</span></div><div class="metric excess"><strong>'+money(c.excess_cents)+'</strong><span>超出普通流程</span></div></div>':'';return summary+'<h3 class="divider">角色结论</h3>'+(findings||'<div class="empty">没有角色产出结论。</div>')+'<h3 class="divider">交叉检查</h3>'+(conflicts||'<div class="caption">未发现冲突。</div>')+'<p class="caption">'+esc(p.next_action||'')+' · 工具调用 '+esc(p.tool_calls)+' 次 · 只读 '+esc(p.read_only)+' · 业务写入 '+esc(p.business_effects)+'</p>';}
function sourcesHtml(sources){return (sources||[]).map(s=>'<div class="source"><strong>'+esc(s.title||s.doc_id||s.policy_id||'适用制度')+' · '+esc(s.version||s.policy_version||'')+' · '+esc(s.section||s.section_id||s.clause_id||'')+'</strong><p>'+esc(s.content||s.text||s.quote||JSON.stringify(s))+'</p></div>').join('');}
function renderOrders(){const target=$('orders');target.className='scroll';target.innerHTML=orders.length?orders.map(o=>'<div class="item"><label class="checkbox-item"><input type="checkbox" name="order" value="'+esc(o.order_id)+'"><span>'+esc(({hotel:'住宿',train:'铁路票据'}[o.kind])||o.kind||o.type||o.order_type||'订单')+' · '+money(o.amount_cents)+'<div class="sub">'+esc(o.order_id)+' · '+esc(o.destination||o.city||'')+' · '+esc(o.start_date||o.date||'')+(o.end_date?' → '+esc(o.end_date):'')+' · '+esc(o.status||'')+'</div></span></label></div>').join(''):'<div class="empty">该员工没有可展示的订单。</div>';}
function workflowOwns(draft){return currentWorkflow&&((currentWorkflow.draft_id||currentWorkflow.draft?.draft_id||currentWorkflow.pending?.draft_id)===draft.draft_id)&&currentWorkflow.pending?.kind==='approval';}
function renderDraft(draft){currentDraft=draft;$('checked').checked=false;$('draft-status').textContent=label(draft.status);$('draft-status').className='pill'+(draft.status==='submitted'?' good':'');$('draft').className='';let out='<div class="muted">'+esc(draft.draft_id)+' · 版本 '+esc(draft.version)+'</div><div class="summary"><div class="metric"><strong>'+money(draft.total_cents)+'</strong><span>票据合计</span></div><div class="metric eligible"><strong>'+money(draft.eligible_cents)+'</strong><span>可申请金额</span></div><div class="metric excess"><strong>'+money(draft.excess_cents)+'</strong><span>超出普通流程</span></div></div>';out+=(draft.items||[]).map(item=>'<div class="item"><div class="row"><strong>'+esc(({hotel:'住宿',train:'铁路票据'}[item.kind])||item.kind||item.type||item.order_id)+'</strong><span>'+money(item.eligible_cents??item.amount_cents)+'</span></div><div class="sub">'+esc(item.order_id||'')+' · '+esc(item.reason||item.explanation||'')+'</div></div>').join('');out+='<div class="caption">成本中心：'+esc(draft.input?.cost_center||'未填写')+' · 备注：'+esc(draft.input?.notes||'无')+'</div>';out+='<h3 class="divider">核算依据</h3>'+sourcesHtml(draft.sources);if(draft.submission){out+='<div class="submission">申请已提交<strong>'+esc(draft.submission.submission_id||draft.submission.application_id||draft.submission.id||'')+'</strong><span>再次提交将返回同一申请编号。</span></div>';}out+='<details><summary>查看草稿版本与审计字段</summary><pre>'+esc(JSON.stringify(draft,null,2))+'</pre></details>';$('draft').innerHTML=out;$('draft-actions').classList.remove('hide');$('confirm').textContent=workflowOwns(draft)?'确认并提交任务':'确认当前版本';$('confirm').disabled=draft.status==='submitted';$('edit').disabled=draft.status==='submitted';$('submit').disabled=false;$('submit').textContent=draft.status==='submitted'?'再次获取提交结果':'提交已确认草稿';document.querySelectorAll('.step').forEach((step,index)=>step.classList.toggle('active',index<=(draft.status==='submitted'?3:draft.status==='confirmed'?2:1)));if(draft.input?.cost_center)$('cost-center').value=draft.input.cost_center;}
async function refreshDrafts(){const drafts=entries(await api('/api/drafts'));$('draft-count').textContent=drafts.length+' 份';$('drafts').className='scroll';$('drafts').innerHTML=drafts.length?drafts.map(d=>'<button class="listbutton" data-draft="'+esc(d.draft_id)+'">'+esc(d.input?.destination||d.destination||'业务申请')+' · '+money(d.eligible_cents)+'<span>'+esc(d.draft_id)+' · v'+esc(d.version)+' · '+esc(label(d.status))+'</span></button>').join(''):'<div class="empty">暂无草稿。</div>';$('drafts').querySelectorAll('[data-draft]').forEach(button=>button.addEventListener('click',async()=>{try{renderDraft(await api('/api/drafts/'+encodeURIComponent(button.dataset.draft)));}catch(error){feedback(error.message,true);}}));if(currentDraft)renderDraft(await api('/api/drafts/'+encodeURIComponent(currentDraft.draft_id)));}
async function refreshPreferences(){const prefs=entries(await api('/api/preferences'));$('preferences').textContent=prefs.length?prefs.map(p=>(p.key==='cost_center'?'常用成本中心':p.key)+'：'+p.value+' · v'+p.version).join('；'):'暂无已确认偏好。';}
async function loadIdentity(){me=await api('/api/me');$('identity-status').textContent=me.display_name+' · '+me.tenant_id;$('users').value=me.user_id;$('logout').classList.remove('hide');orders=entries(await api('/api/orders'));renderOrders();const centers=entries(await api('/api/cost-centers'));$('cost-center').innerHTML='<option value="">使用已确认偏好或稍后补充</option>'+centers.map(c=>'<option value="'+esc(typeof c==='string'?c:(c.cost_center||c.code||c.id))+'">'+esc(typeof c==='string'?c:(c.label||c.name||c.cost_center||c.code||c.id))+'</option>').join('');await Promise.all([refreshDrafts(),refreshPreferences()]);const saved=localStorage.getItem('enterprise-flow-run-'+me.user_id);if(saved&&!currentWorkflow){try{await showWorkflow(await api('/api/workflows/'+encodeURIComponent(saved)));}catch{localStorage.removeItem('enterprise-flow-run-'+me.user_id);}}}
async function showWorkflow(record){currentWorkflow=record;if(me&&record.run_id)localStorage.setItem('enterprise-flow-run-'+me.user_id,record.run_id);$('workflow').classList.remove('hide');$('workflow-status').textContent=label(record.status||record.stage);$('workflow-json').textContent=JSON.stringify(record,null,2);const prompts=record.pending?.questions||record.questions||record.missing_fields||record.state?.missing_fields||[];$('workflow-detail').textContent=record.message||record.summary||(prompts.length?'待补充：'+prompts.join('、'):'任务状态已保存。');$('clarification').classList.toggle('hide',record.pending?.kind!=='clarification');if(record.pending?.kind==='clarification'){const fields=record.pending.fields||{};$('resume-start').value=fields.start_date||'';$('resume-end').value=fields.end_date||'';$('resume-city').value=fields.destination||'';if(fields.cost_center)$('cost-center').value=fields.cost_center;document.querySelectorAll('input[name="order"]').forEach(input=>{input.checked=(fields.order_ids||[]).includes(input.value);});}const draftId=record.draft_id||record.state?.draft_id||record.result?.draft_id;if(record.draft?.draft_id)renderDraft(record.draft);else if(draftId)renderDraft(await api('/api/drafts/'+encodeURIComponent(draftId)));await refreshDrafts();}
guarded('login',async()=>{await api('/api/login',{method:'POST',body:{user_id:$('users').value}});currentDraft=null;currentWorkflow=null;$('workflow').classList.add('hide');$('draft-actions').classList.add('hide');$('draft').className='empty';$('draft').textContent='选择草稿后查看金额、来源和版本。';await loadIdentity();feedback('已切换身份。所有订单、草稿和任务都按当前员工过滤。');});
guarded('logout',async()=>{await api('/api/logout',{method:'POST',body:{}});location.reload();});
guarded('refresh',async()=>{requireLogin();await loadIdentity();if(currentWorkflow)await showWorkflow(await api('/api/workflows/'+encodeURIComponent(currentWorkflow.run_id)));feedback('已刷新持久业务状态。');});
guarded('start',async()=>{requireLogin();await showWorkflow(await api('/api/workflows',{method:'POST',body:{message:$('message').value,request_id:crypto.randomUUID()}}));feedback('任务已执行，核算与提交结果以业务服务记录为准。');});
guarded('resume',async()=>{requireLogin();if(!currentWorkflow)throw Error('没有可继续的任务。');const fields={...(currentWorkflow.pending?.fields||{})},selected=[...document.querySelectorAll('input[name="order"]:checked')].map(i=>i.value);if(selected.length)fields.order_ids=selected;if($('cost-center').value)fields.cost_center=$('cost-center').value;if($('resume-start').value)fields.start_date=$('resume-start').value;if($('resume-end').value)fields.end_date=$('resume-end').value;if($('resume-city').value.trim())fields.destination=$('resume-city').value.trim();await showWorkflow(await api('/api/workflows/'+encodeURIComponent(currentWorkflow.run_id)+'/resume',{method:'POST',body:{decision:{action:'provide_fields',fields}}}));});
guarded('create-draft',async()=>{requireLogin();const ids=[...document.querySelectorAll('input[name="order"]:checked')].map(i=>i.value);if(!ids.length)throw Error('请先勾选本人订单。');renderDraft(await api('/api/drafts',{method:'POST',body:{order_ids:ids,cost_center:$('cost-center').value||null,notes:$('notes').value}}));await refreshDrafts();feedback('已生成版本化草稿，请核对费用与制度。');});
guarded('confirm',async()=>{requireLogin();if(!currentDraft||!$('checked').checked)throw Error('请核对并勾选当前版本确认声明。');if(workflowOwns(currentDraft)){await showWorkflow(await api('/api/workflows/'+encodeURIComponent(currentWorkflow.run_id)+'/resume',{method:'POST',body:{decision:{action:'approve',expected_version:currentDraft.version,expected_hash:currentDraft.content_hash}}}));feedback('当前版本已确认，申请记录已生成。');}else{renderDraft(await api('/api/drafts/'+encodeURIComponent(currentDraft.draft_id)+'/confirm',{method:'POST',body:{expected_version:currentDraft.version,expected_hash:currentDraft.content_hash}}));await refreshDrafts();feedback('当前版本已确认，可以提交。');}});
guarded('submit',async()=>{requireLogin();if(!currentDraft)throw Error('请先选择草稿。');const result=await api('/api/drafts/'+encodeURIComponent(currentDraft.draft_id)+'/submit',{method:'POST',body:{expected_version:currentDraft.version,idempotency_key:'workbench-'+currentDraft.draft_id+'-v'+currentDraft.version}});renderDraft(await api('/api/drafts/'+encodeURIComponent(currentDraft.draft_id)));await refreshDrafts();feedback('申请记录已入库：'+(result.submission_id||result.application_id||result.submission?.submission_id||currentDraft.submission?.submission_id||'已返回业务编号'));});
guarded('edit',async()=>{requireLogin();if(!currentDraft)throw Error('请先选择草稿。');const wasWorkflow=workflowOwns(currentDraft);renderDraft(await api('/api/drafts/'+encodeURIComponent(currentDraft.draft_id),{method:'PATCH',body:{expected_version:currentDraft.version,order_ids:currentDraft.input.order_ids,cost_center:$('cost-center').value||null,notes:$('notes').value}}));if(wasWorkflow)await showWorkflow(await api('/api/workflows/'+encodeURIComponent(currentWorkflow.run_id)+'/resume',{method:'POST',body:{decision:{action:'refresh'}}}));await refreshDrafts();feedback('已更新草稿；原确认失效，请核对新版本。');});
guarded('agent-run',async()=>{requireLogin();const result=await api('/api/agent-proposals',{method:'POST',body:{message:$('agent-message').value}});$('agent-result').innerHTML=renderProposal(result);feedback(result.blocked?'分析发现问题，请按结论补充信息或人工核查。':'分析完成：结论仅供核对，创建与提交仍需人工确认。');});
guarded('plan-run',async()=>{requireLogin();const result=await api('/api/plan',{method:'POST',body:{message:$('agent-message').value}});const steps=(result.steps||[]).map(s=>'<div class="item"><div class="sub">步骤 '+esc(s.observation.step)+' · '+esc(s.observation.tool)+' · '+esc(s.observation.ok?'成功':'失败')+' · '+esc(s.observation.duration_ms)+'ms</div><div>'+esc(s.thought)+'</div></div>').join('');$('agent-result').innerHTML='<h3>规划轨迹</h3>'+(steps||'<div class="empty">规划未执行任何工具。</div>')+'<p class="caption">停止原因：'+esc(result.stop_reason)+' · 工具调用 '+esc(result.tool_calls)+' 次 · 只读 '+esc(result.read_only)+' · 业务写入 '+esc(result.business_effects)+'</p>';feedback('规划轨迹已生成，未产生任何业务写入。');});
guarded('search',async()=>{requireLogin();const policies=entries(await api('/api/policies?query='+encodeURIComponent($('policy-query').value)+'&trip_date='+encodeURIComponent($('policy-date').value)));$('policies').innerHTML=policies.length?sourcesHtml(policies):'<div class="empty">当前权限和日期下没有匹配制度。</div>';});
guarded('policy-answer',async()=>{requireLogin();const result=await api('/api/policy-answers',{method:'POST',body:{question:$('policy-question').value,trip_date:$('policy-date').value}});$('policy-answer-result').innerHTML='<p>'+esc(result.answer||result.message||'资料不足，无法回答。')+'</p>'+sourcesHtml(result.sources||[])+'<p class="caption">'+(result.model_used?'模型生成，语义支持待人工核对。':'直接展示条款，未调用大模型。')+'</p>';});
guarded('save-pref',async()=>{requireLogin();if(!$('cost-center').value)throw Error('请先选择要保存的成本中心。');await api('/api/preferences',{method:'PATCH',body:{key:'cost_center',value:$('cost-center').value,confirmed:true}});await refreshPreferences();feedback('已保存用户明确确认的成本中心偏好。');});
guarded('delete-pref',async()=>{requireLogin();await api('/api/preferences',{method:'DELETE',body:{key:'cost_center'}});await refreshPreferences();feedback('已删除成本中心偏好。');});
(async()=>{try{const users=entries(await api('/api/demo-users'));$('users').innerHTML=users.map(u=>'<option value="'+esc(u.user_id)+'">'+esc(u.display_name)+' · '+esc(u.tenant_id)+' / '+esc(u.department_id)+'</option>').join('');try{await loadIdentity();}catch{me=null;}}catch(error){feedback(error.message,true);}})();
</script></body></html>'''


def create_app(database_path: Path | str | None = None, model_mode: str = "fixture") -> Starlette:
    if model_mode not in {"fixture", "qwen", "api"}:
        raise ValueError("model_mode must be fixture, qwen, or api")
    database_path = Path(database_path or os.environ.get("ENTERPRISEFLOW_DB", "runs/enterprise.db"))
    service = EnterpriseService(database_path)
    service.seed_demo()
    signer = _SessionSigner(secrets.token_bytes(32))
    engine_lock = asyncio.Lock()

    async def engine():
        async with engine_lock:
            if app.state.workflow_engine is None:
                from .workflow import WorkflowEngine
                app.state.workflow_engine = await WorkflowEngine.open(service, database_path.with_name(database_path.stem + "-checkpoints.sqlite"), mode=model_mode)
            return app.state.workflow_engine

    def principal(request: Request):
        return service.authenticate_demo(signer.read(request.cookies.get(_COOKIE)))

    @asynccontextmanager
    async def lifespan(application):
        yield
        if application.state.workflow_engine is not None:
            await application.state.workflow_engine.aclose()

    async def home(request):
        mode_label = {"fixture": "固定业务工作流（不调用大模型）", "qwen": "Qwen 模型字段提取", "api": "模型 API 字段提取"}[model_mode]
        return HTMLResponse(_HTML.replace("__MODE_LABEL__", html.escape(mode_label)), headers={"cache-control": "no-store", "x-content-type-options": "nosniff", "referrer-policy": "same-origin"})

    async def health(request):
        return _response({"status": "ok", "service": "EnterpriseFlow", "mode": model_mode, "model_validated": False, "demo": True})

    async def demo_users(request):
        return _response(service.list_demo_users())

    async def login(request):
        payload = await _json_body(request)
        _fields(payload, {"user_id"}, {"user_id"})
        identity = service.authenticate_demo(_string(payload, "user_id", 128))
        response = _response(identity)
        response.set_cookie(_COOKIE, signer.sign(identity.user_id), max_age=_SESSION_SECONDS, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    async def logout(request):
        principal(request)
        await _json_body(request, optional=True)
        response = _response({"logged_out": True})
        response.delete_cookie(_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    async def me(request):
        return _response(principal(request))

    async def policies(request):
        return _response(service.search_policies(principal(request), query=request.query_params.get("query", ""), trip_date=request.query_params.get("trip_date", "2026-10-09")))

    async def my_orders(request):
        return _response(service.list_orders(principal(request), start_date=request.query_params.get("start_date"), end_date=request.query_params.get("end_date")))

    async def policy_answer(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"question", "trip_date"}, {"question", "trip_date"})
        from .policy_qa import PolicyQA
        result = await PolicyQA(service, mode=model_mode).answer(identity, _string(payload, "question", 500), _string(payload, "trip_date", 10))
        return _response(result)

    async def cost_centers(request):
        return _response(service.list_cost_centers(principal(request)))

    async def plan(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"message", "max_steps"}, {"message"})
        steps = payload.get("max_steps", 6)
        if type(steps) is not int or not 1 <= steps <= 20:
            raise _HTTPProblem("invalid_field", "max_steps 需要 1–20 的整数。")
        from .planner import Planner
        result = await Planner(service, mode=model_mode, max_steps=steps).plan(identity, _string(payload, "message"))
        return _response(result)

    async def agent_proposals(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"message"}, {"message"})
        from .agents import Supervisor
        proposal = await Supervisor(service, mode=model_mode).collaborate(identity, _string(payload, "message"))
        return _response(proposal)

    async def drafts(request):
        identity = principal(request)
        if request.method == "GET":
            return _response(service.list_drafts(identity))
        payload = await _json_body(request)
        _fields(payload, _DRAFT_FIELDS, {"order_ids"})
        return _response(service.create_draft(identity, payload), 201)

    async def draft(request):
        identity, draft_id = principal(request), request.path_params["draft_id"]
        if request.method == "GET":
            return _response(service.get_draft(identity, draft_id))
        payload = await _json_body(request)
        _fields(payload, _DRAFT_FIELDS | {"expected_version"}, {"expected_version", "order_ids"})
        version = _version(payload)
        return _response(service.edit_draft(identity, draft_id, {k: v for k, v in payload.items() if k != "expected_version"}, version))

    async def confirm(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"expected_version", "expected_hash"}, {"expected_version", "expected_hash"})
        digest = _string(payload, "expected_hash", 64)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise _HTTPProblem("invalid_hash", "请提供页面显示草稿的内容摘要。")
        return _response(service.confirm_draft(identity, request.path_params["draft_id"], _version(payload), expected_hash=digest))

    async def submit(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"expected_version", "idempotency_key"}, {"expected_version", "idempotency_key"})
        return _response(service.submit_draft(identity, request.path_params["draft_id"], _version(payload), _string(payload, "idempotency_key", 128)))

    async def preferences(request):
        identity = principal(request)
        if request.method == "GET":
            return _response(service.get_preferences(identity))
        payload = await _json_body(request)
        if request.method == "DELETE":
            _fields(payload, {"key"}, {"key"})
            result = service.delete_preference(identity, _string(payload, "key", 64))
            return _response(result if result is not None else {"deleted": True})
        _fields(payload, {"key", "value", "confirmed"}, {"key", "value", "confirmed"})
        if payload["confirmed"] is not True:
            raise _HTTPProblem("confirmation_required", "保存长期偏好需要用户明确确认。")
        return _response(service.set_preference(identity, _string(payload, "key", 64), _string(payload, "value", 128), confirmed=True))

    async def workflow_start(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"message", "request_id"}, {"message"})
        request_id = _string(payload, "request_id", 128) if payload.get("request_id") is not None else None
        manager = await engine()
        return _response(await manager.start(identity, _string(payload, "message"), request_id=request_id), 201)

    async def workflow_get(request):
        identity = principal(request)
        manager = await engine()
        return _response(await manager.get(identity, request.path_params["run_id"]))

    async def workflow_resume(request):
        identity = principal(request)
        payload = await _json_body(request)
        _fields(payload, {"decision"}, {"decision"})
        if not isinstance(payload["decision"], dict):
            raise _HTTPProblem("invalid_decision", "继续任务的 decision 必须是对象。")
        manager = await engine()
        return _response(await manager.resume(identity, request.path_params["run_id"], payload["decision"]))

    async def problem(request, exc):
        return _response({"error": exc.code, "message": exc.message}, exc.status_code)

    routes = [
        Route("/", home), Route("/health", health), Route("/api/demo-users", demo_users),
        Route("/api/login", login, methods=["POST"]), Route("/api/logout", logout, methods=["POST"]),
        Route("/api/me", me), Route("/api/policies", policies), Route("/api/policy-answers", policy_answer, methods=["POST"]), Route("/api/orders", my_orders), Route("/api/cost-centers", cost_centers),
        Route("/api/plan", plan, methods=["POST"]), Route("/api/agent-proposals", agent_proposals, methods=["POST"]),
        Route("/api/drafts", drafts, methods=["GET", "POST"]), Route("/api/drafts/{draft_id}", draft, methods=["GET", "PATCH"]),
        Route("/api/drafts/{draft_id}/confirm", confirm, methods=["POST"]), Route("/api/drafts/{draft_id}/submit", submit, methods=["POST"]),
        Route("/api/preferences", preferences, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/workflows", workflow_start, methods=["POST"]), Route("/api/workflows/{run_id}", workflow_get),
        Route("/api/workflows/{run_id}/resume", workflow_resume, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={_HTTPProblem: problem, DomainError: problem})
    app.state.service, app.state.workflow_engine, app.state.model_mode = service, None, model_mode
    app.add_middleware(SameOriginMiddleware)
    return app
