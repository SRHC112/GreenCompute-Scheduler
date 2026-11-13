"""零依赖可视化操作台：只用 Python 标准库起一个 HTTP 服务。

为什么自己写而不用 Flask/FastAPI：
1. 参赛要求「一条命令跑起来」，评委机器上不保证有第三方框架；
2. 本机零安装依赖 = 现场演示不会因为 pip 失败而开天窗；
3. 前端用原生 JS + CSS，避免 CDN 断网导致白屏（离线评审场景很常见）。

四区块（对应演示动线）：
    ① 任务发起：填作业数/种子/策略组 → 触发一次真实编排
    ② DAG 拓扑：八个状态机与节点状态，返工边高亮
    ③ 运行轨迹：逐条事件，含角色、节点、缺陷归因
    ④ 评测看板：六组对照实验结果与相对提升

启动：
    python app/server.py            # 默认 http://127.0.0.1:8000
    python app/server.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gcs.demo import POLICIES  # noqa: E402
from gcs.knowledge import KnowledgePack  # noqa: E402
from gcs.llm import ModelGateway  # noqa: E402
from gcs.orchestrator import Orchestrator  # noqa: E402
from gcs.report import render_console  # noqa: E402
from gcs.simulator import make_jobs  # noqa: E402
from app.auth import AuthConfig, WWW_AUTHENTICATE  # noqa: E402

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
RUNS_DIR = ROOT / "runs"
MAX_BODY = 1024 * 1024          # P1-2.1：请求体上限 1 MiB，超限返回 413
AUTH = AuthConfig(root=ROOT)    # 进程启动时读取一次鉴权配置，无热加载

# 内存中的最近若干次运行，供看板取用
_STORE: dict[str, dict] = {}
_LOCK = threading.Lock()

# 运行编号白名单：只允许 run_YYYYmmdd_HHMMSS_<策略键> 的形式。
# 这既是入参校验，也顺手堵掉了 /console/../../x 这类路径穿越。
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[A-Za-z0-9_-]+$")


def safe_run_id(rid: str) -> str | None:
    """合法则原样返回，非法返回 None（调用方按 400 处理）。"""
    return rid if RUN_ID_RE.match(rid) else None


def run_once(jobs_n: int, seed: int, policy_key: str) -> dict:
    """一键生一个可回放运行：先跑通编排，再渲染单文件看板并落入内存缓存。"""
    jobs = make_jobs(PACK, n=jobs_n, seed=seed)
    llm = ModelGateway(token_budget=int(PACK.rework_budget["max_tokens"]))
    art = Orchestrator(PACK, llm=llm, policy=POLICIES[policy_key]).run(jobs)

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_dir = RUNS_DIR / f"{run_id}_{policy_key}"
    out_dir.mkdir(parents=True, exist_ok=True)
    render_console(art, PACK.fingerprint(), out_dir / "console.html")
    (out_dir / "artifact.json").write_text(
        json.dumps(art.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps(art.metrics.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    payload = {
        "run_id": out_dir.name,
        "policy": art.policy,
        "policy_key": policy_key,
        "seed": seed,
        "jobs": art.metrics.jobs,
        "metrics": art.metrics.as_dict(),
        "rounds": art.rounds,
        "rubric": art.rubric_history[-1].as_dict() if art.rubric_history else None,
        "defects": art.defect_history[-1] if art.defect_history else None,
        "dag": art.dag.to_dict(),
        "events": [
            # P2-3.4：把「每个角色只拿到自己需要的工具」的审计记录并入轨迹，
            # 供操作台「使用工具」列展示；_emit 保证 audit 与 events 一一对应。
            {**e.as_dict(),
             "tool": art.audit[i]["tool"] if i < len(art.audit) else "-"}
            for i, e in enumerate(art.events)
        ],
        "narrative": art.narrative,
        "llm": art.llm,
        "elapsed_s": round(art.elapsed_s, 3),
        "console_url": f"/console/{out_dir.name}",
    }
    with _LOCK:
        _STORE[out_dir.name] = payload
    return payload


def eval_board() -> dict:
    """拿到对照实验汇总（若未跑过则返回 available=False + 提示），交给前端做评测看板。"""
    p = ROOT / "evaluation" / "summary.json"
    if not p.exists():
        return {"available": False, "message": "尚未运行对照实验，请先执行 python evaluation/run_eval.py"}
    return {"available": True, "groups": json.loads(p.read_text(encoding="utf-8"))}


def list_runs() -> list[dict]:
    """P2-3.2：只读列出磁盘上的历史运行，支持重启后回溯。

    刻意不把历史 artifact.json 全量载入 _STORE（避免启动耗时与内存膨胀）；
    实际内容在访问 /api/run/<rid> 时按需从磁盘读取。
    """
    if not RUNS_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(RUNS_DIR.iterdir(), key=lambda x: x.name, reverse=True):
        if not d.is_dir() or safe_run_id(d.name) is None:
            continue
        out.append({
            "run_id": d.name,
            "has_console": (d / "console.html").is_file(),
            "has_artifact": (d / "artifact.json").is_file(),
            "console_url": f"/console/{d.name}",
        })
    return out[:50]


def load_run_from_disk(rid: str) -> dict | None:
    """把历史 artifact.json 转成与 run_once 返回值同形的 payload。"""
    f = RUNS_DIR / rid / "artifact.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rubric = data.get("rubric") or []
    return {
        "run_id": rid,
        "policy": data.get("policy"),
        "policy_key": None,
        "seed": None,
        "jobs": (data.get("metrics") or {}).get("jobs"),
        "metrics": data.get("metrics") or {},
        "rounds": data.get("rounds"),
        "rubric": rubric[-1] if rubric else None,
        "defects": (data.get("defects") or [None])[-1],
        "dag": data.get("dag"),
        "events": data.get("events") or [],
        "narrative": data.get("narrative") or "",
        "llm": data.get("llm") or {},
        "elapsed_s": data.get("elapsed_s"),
        "console_url": f"/console/{rid}",
    }


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>绿算调度 · 操作台</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
     background:#EEF1F6;color:#232A33}
header{background:#1F3352;color:#fff;padding:16px 24px;border-bottom:4px solid #E08A3C;
       display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
header h1{font-size:20px;font-weight:700}
header .sub{font-size:12px;color:#AFC2DD}
nav{display:flex;gap:8px;padding:14px 24px 0}
nav button{border:1px solid #D2DAE6;background:#fff;color:#33415A;font-size:13px;
           padding:8px 16px;border-radius:4px 4px 0 0;cursor:pointer;font-family:inherit}
nav button.on{background:#1F3352;color:#fff;border-color:#1F3352}
main{padding:0 24px 40px}
.panel{display:none;padding-top:14px}
.panel.on{display:block}
.card{background:#fff;border:1px solid #E2E7EF;border-radius:6px;padding:16px 18px;margin-bottom:14px}
.card h2{font-size:15px;color:#1F3352;margin-bottom:3px}
.card .hint{font-size:12px;color:#8B96A8;margin-bottom:12px;line-height:1.6}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
label{display:block;font-size:12px;color:#5A6779;margin-bottom:5px}
input,select{font-family:inherit;font-size:13px;padding:7px 10px;border:1px solid #C9D3E3;
             border-radius:4px;background:#fff;min-width:120px}
button.go{background:#E08A3C;color:#fff;border:none;padding:9px 20px;border-radius:4px;
          font-size:13.5px;cursor:pointer;font-family:inherit}
button.go:disabled{background:#B9C2CF;cursor:not-allowed}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:14px}
.kpi{border:1px solid #E2E7EF;border-radius:6px;padding:12px 14px;background:#fff;border-top:3px solid #2F6DB5}
.kpi .l{font-size:11.5px;color:#7A8699}
.kpi .v{font-size:22px;font-weight:700;color:#1F3352;margin-top:4px}
.kpi .s{font-size:11px;color:#8B96A8;margin-top:3px}
.kpi.g{border-top-color:#1F8A70}.kpi.o{border-top-color:#E08A3C}
#dag{position:relative;height:420px}
.node{position:absolute;border-radius:5px;border:1px solid #C9D3E3;background:#F7F9FC;padding:5px 7px;
      font-size:11px;line-height:1.25;overflow:hidden}
.node .id{font-size:9px;color:#8B96A8}
.node.done{background:#EAF3EE;border-color:#1F8A70}
.node.loop{border-style:dashed;background:#FDF3E8;border-color:#E08A3C}
.node .st{position:absolute;top:4px;right:5px;font-size:9px;color:#1F8A70}
.legend{font-size:11px;color:#7A8699;margin-top:8px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.trace{max-height:520px;overflow:auto;font-size:12.5px}
.ev{display:grid;grid-template-columns:42px 1fr 92px;gap:8px;padding:7px 0;border-bottom:1px dashed #EDF0F5}
.ev .sq{font-size:10.5px;color:#A6B0C0}
.ev .st{display:inline-block;font-size:9.5px;padding:1px 6px;border-radius:3px;background:#E8EEF7;color:#2F6DB5;margin-right:6px}
.ev .ag{color:#8B96A8;font-size:10.5px;margin-left:6px}
.ev .dt{color:#3A4553;line-height:1.45;margin-top:2px}
.ev .tool{font-size:10px;color:#2F6DB5;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.auth{display:flex;gap:6px;align-items:center}
.auth label{margin:0;color:#AFC2DD;font-size:11px}
.auth input{min-width:170px;padding:5px 8px;font-size:12px;background:#fff}
.auth .sub{font-size:11px;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#1F3352;color:#fff;padding:8px;text-align:left;font-size:11.5px}
td{padding:7px 8px;border-bottom:1px solid #EDF0F5;color:#3A4553}
tr:nth-child(even) td{background:#F8FAFD}
.track{height:8px;background:#EDF0F5;border-radius:4px;overflow:hidden;min-width:120px}
.fill{height:100%;background:linear-gradient(90deg,#2F6DB5,#1F8A70);border-radius:4px}
.muted{color:#8B96A8;font-size:12.5px;padding:8px 0}
.err{background:#FBECEC;border:1px solid #E8B7B3;color:#8C2F26;padding:10px 12px;border-radius:4px;font-size:13px}
</style></head><body>
<header><div><h1>绿算调度 · 多智能体编排操作台</h1>
<div class="sub">2026 第五届“未来杯”创新大赛 · 赛道一 Agent 编排创新应用 · 知识包 """ + "{ver}" + """</div></div>
<div class="auth"><label for="tok">访问凭据</label>
<input id="tok" type="password" placeholder="对外演示时填写" autocomplete="off">
<span id="authstate" class="sub">本机运行 · 确定性可复现</span></div></header>
<nav>
  <button class="on" data-p="p1">① 任务发起</button>
  <button data-p="p2">② DAG 拓扑</button>
  <button data-p="p3">③ 运行轨迹</button>
  <button data-p="p4">④ 评测看板</button>
</nav>
<main>
<section class="panel on" id="p1">
  <div class="card"><h2>发起一次调度任务</h2>
    <div class="hint">选择策略组与作业规模，服务端会真实跑一遍多智能体编排（画像→调度→执行→五层校验→自省→定向返工），并把结果写进 runs/ 目录。</div>
    <div class="row">
      <div><label>策略组</label><select id="policy"></select></div>
      <div><label>作业数量</label><input id="jobs" type="number" value="60" min="10" max="200"></div>
      <div><label>随机种子</label><input id="seed" type="number" value="20261111"></div>
      <div><button class="go" id="run">开始编排</button></div>
    </div>
    <div id="msg" style="margin-top:12px"></div>
    <div class="kpis" id="kpis"></div>
    <div id="narr" class="muted"></div>
    <div id="hist" class="hint" style="margin-top:8px"></div>
  </div>
</section>
<section class="panel" id="p2">
  <div class="card"><h2>DAG 拓扑与节点状态</h2>
    <div class="hint">实线为依赖边，虚线为返工边；返工边只在验证不通过时激活，实现「只退责任节点、不整链重跑」。</div>
    <div id="dag"></div>
    <div class="legend"><span><i style="background:#1F8A70"></i>已完成</span>
      <span><i style="background:#E08A3C"></i>返工节点</span>
      <span><i style="background:#C9D3E3"></i>依赖边</span></div>
  </div>
</section>
<section class="panel" id="p3">
  <div class="card"><h2>运行轨迹（逐条事件）</h2>
    <div class="hint">每个事件带状态、角色、节点与细节；缺陷会给出责任角色与定向返工指令；最右列是该事件实际使用的工具（角色工具白名单审计）。</div>
    <div class="trace" id="trace"></div>
  </div>
</section>
<section class="panel" id="p4">
  <div class="card"><h2>六组对照实验看板</h2>
    <div class="hint">同一模拟器数据、同一知识包、同一评测脚本，3 个种子重复 3 次，报均值±标准差。</div>
    <div id="board"></div>
  </div>
</section>
</main>
<script>
const $=s=>document.querySelector(s);
let lastDag=null;
// 静态 Token 只存当前标签页的 sessionStorage：不进 URL、不进 localStorage、
// 不进日志，关闭标签即失效；由 authFetch 统一注入 Authorization 头。
function getToken(){return sessionStorage.getItem('gcs_auth_token')||'';}
function authFetch(url,opts){
  opts=opts||{};
  const h=Object.assign({},opts.headers||{});
  if(getToken()) h['Authorization']='Bearer '+getToken();
  return fetch(url,Object.assign({},opts,{headers:h}));
}
$('#tok').value=getToken();
$('#authstate').textContent=getToken()?'凭据已设置':'本机运行 · 确定性可复现';
$('#tok').addEventListener('change',()=>{
  sessionStorage.setItem('gcs_auth_token',$('#tok').value.trim());
  $('#authstate').textContent=getToken()?'凭据已设置':'本机运行 · 确定性可复现';
});
function goPanel(pid){
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x.dataset.p===pid));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on',p.id===pid));
  if(pid==='p2'&&lastDag) requestAnimationFrame(()=>drawDag(lastDag));
  if(pid==='p4') loadBoard();
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>goPanel(b.dataset.p));
window.addEventListener('resize',()=>{
  if(lastDag&&$('#p2').classList.contains('on')) drawDag(lastDag);
});
authFetch('/api/policies').then(r=>r.json()).then(d=>{
  const sel=$('#policy');
  d.policies.forEach(p=>{const o=document.createElement('option');o.value=p.key;o.textContent=p.name;sel.appendChild(o);});
});
function drawDag(g){
  lastDag=g;
  const host=$('#dag');host.innerHTML='';
  const W=host.clientWidth,H=host.clientHeight,padL=8,padT=10,gap=8;
  if(W<120) return;  // 面板尚未可见，等切换回来再画
  const colW=(W-padL-16)/g.levels.length,pos={};
  g.levels.forEach((ids,li)=>{const n=ids.length,h=(H-padT-20-gap*(n-1))/n;
    ids.forEach((id,ni)=>{pos[id]={x:padL+li*colW+4,y:padT+ni*(h+gap),w:colW-26,h:Math.min(h,54)};});});
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width',W);svg.setAttribute('height',H);
  svg.style.position='absolute';svg.style.left=0;svg.style.top=0;svg.style.pointerEvents='none';
  g.nodes.forEach(n=>{n.deps.forEach(d=>{const a=pos[d],b=pos[n.id];if(!a||!b)return;
    const x1=a.x+a.w,y1=a.y+16,x2=b.x,y2=b.y+16,dx=Math.max(18,(x2-x1)/2);
    const p=document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d',`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`);
    p.setAttribute('fill','none');p.setAttribute('stroke',n.loop?'#E08A3C':'#BFD0E6');
    p.setAttribute('stroke-width',n.loop?1.6:1.2);if(n.loop)p.setAttribute('stroke-dasharray','4,3');
    svg.appendChild(p);});});
  host.appendChild(svg);
  g.nodes.forEach(n=>{const p=pos[n.id];if(!p)return;const d=document.createElement('div');
    d.className='node '+(n.loop?'loop ':'')+(n.status==='done'?'done':'');
    d.style.cssText+=`left:${p.x}px;top:${p.y}px;width:${p.w}px;height:${p.h}px`;
    d.innerHTML=`<div class="id">${n.agent} · ${n.type}</div>${n.label}<div class="st">${n.status==='done'?'✓':''}</div>`;
    d.title=n.detail||'';host.appendChild(d);});
}
function renderRun(d){
  const m=d.metrics;
  const k=[['SLA 达成率',(m.sla_rate*100).toFixed(1)+'%','硬约束：截止时间',''],
    ['单位算力成本','¥'+m.cost_per_gpu_hour.toFixed(2),'元 / GPU·h','o'],
    ['平均绿电占比',(m.avg_green_share*100).toFixed(1)+'%','作业时段加权','g'],
    ['平均排队',m.avg_wait_min.toFixed(0)+' min','P95 '+m.p95_wait_min.toFixed(0)+' min',''],
    ['返工轮次',String(d.rounds),'rubric '+(d.rubric?d.rubric.score:'-')+' / '+(d.rubric&&d.rubric.threshold!=null?d.rubric.threshold:'-'),'o']];
  $('#kpis').innerHTML=k.map(x=>`<div class="kpi ${x[3]}"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="s">${x[2]}</div></div>`).join('');
  $('#narr').textContent='运行摘要：'+d.narrative;
  drawDag(d.dag);
  $('#trace').innerHTML=d.events.map(e=>`<div class="ev"><div class="sq">#${String(e.seq).padStart(2,'0')}</div><div>`+
    `<span class="st">${e.state}</span><span class="ag">${e.agent} · ${e.node}</span><div class="dt">${e.detail}</div></div>`+
    `<div class="tool" title="使用工具">${e.tool||'-'}</div></div>`).join('');
}
$('#run').onclick=async()=>{
  $('#run').disabled=true;$('#msg').innerHTML='<div class="muted">编排执行中，请稍候……</div>';
  try{
    const r=await authFetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({jobs:parseInt($('#jobs').value,10),seed:parseInt($('#seed').value,10),policy:$('#policy').value})});
    const d=await r.json();
    if(r.status===401)throw new Error('未授权：请在页首「访问凭据」填入 Token 后重试');
    if(!r.ok)throw new Error(d.error||'运行失败');
    $('#msg').innerHTML=`<div class="muted">已完成：${d.run_id}（耗时 ${d.elapsed_s}s，模型 ${d.llm.mode}，token ${d.llm.tokens_total}）`+
      ` <button id="openconsole" class="go" style="padding:4px 10px;margin-left:8px">打开单文件看板</button></div>`;
    $('#openconsole').onclick=async()=>{
      try{
        // /console/<rid> 是浏览器导航，无法带自定义头；改为取回单文件 HTML
        // 后写入新窗口，保持「零改动看板 + 鉴权头可用」。
        const rr=await authFetch(d.console_url);
        if(rr.status===401)throw new Error('未授权：请先填写访问凭据');
        if(!rr.ok)throw new Error('看板读取失败');
        const html=await rr.text();
        const w=window.open('','_blank');
        if(!w)throw new Error('浏览器拦截了新窗口，请允许弹出窗口');
        w.document.open();w.document.write(html);w.document.close();
      }catch(e){$('#msg').innerHTML=`<div class="err">看板打开失败：${e.message}</div>`;}
    };
    renderRun(d);
    loadHistory();
  }catch(e){$('#msg').innerHTML=`<div class="err">运行失败：${e.message}</div>`;}
  $('#run').disabled=false;
};
async function loadBoard(){
  const d=await (await authFetch('/api/eval')).json();
  if(!d.available){$('#board').innerHTML=`<div class="muted">${d.message}</div>`;return;}
  const metrics=[['sla_rate','SLA 达成率',1],['budget_rate','预算达标率',1],
    ['cost_per_gpu_hour','单位成本 元/GPU·h',0],['green_window_ratio','绿电窗口作业占比',1],
    ['avg_wait_min','平均等待 分钟',0],['rubric_score','rubric 得分',0]];
  let h='';
  metrics.forEach(m=>{
    const mx=Math.max(...Object.values(d.groups).map(g=>g[m[0]][0]))||1;
    h+=`<h3 style="font-size:13.5px;color:#1F3352;margin:14px 0 6px">${m[1]}</h3><table><thead><tr><th>组</th><th>配置</th><th>对比</th><th>值</th><th>波动</th></tr></thead><tbody>`;
    Object.entries(d.groups).forEach(([k,g])=>{
      const v=g[m[0]][0],sd=g[m[0]][1];
      h+=`<tr><td><b>${k}</b></td><td>${g.name}</td><td><div class="track"><div class="fill" style="width:${(v/mx*100).toFixed(1)}%"></div></div></td>`+
        `<td>${m[2]?(v*100).toFixed(1)+'%':v.toFixed(2)}</td><td>±${m[2]?(sd*100).toFixed(1)+'%':sd.toFixed(2)}</td></tr>`;
    });
    h+='</tbody></table>';
  });
  $('#board').innerHTML=h;
}
// P2-3.2：重启后仍可回溯 runs/ 下已归档的运行（服务端按需从磁盘读取）
async function loadHistory(){
  try{
    const d=await (await authFetch('/api/runs')).json();
    const runs=(d.runs||[]).slice(0,5);
    if(!runs.length){$('#hist').textContent='历史运行：暂无归档记录';return;}
    $('#hist').innerHTML='历史运行（最近 5 次，点击回看轨迹）： '+
      runs.map(r=>`<a href="#" data-rid="${r.run_id}" style="margin-right:10px">${r.run_id}</a>`).join('');
    $('#hist').querySelectorAll('a').forEach(a=>a.onclick=async(ev)=>{
      ev.preventDefault();
      try{
        const rr=await authFetch('/api/run/'+a.dataset.rid);
        const dd=await rr.json();
        if(!rr.ok)throw new Error(dd.error||'读取失败');
        renderRun(dd);goPanel('p3');
      }catch(e){$('#msg').innerHTML=`<div class="err">历史读取失败：${e.message}</div>`;}
    });
  }catch(e){$('#hist').textContent='历史运行列表不可用';}
}
loadHistory();
// 深链接：?panel=2&autorun=1 可直接落到某个面板并自动跑一次，方便录屏与截图
const QS=new URLSearchParams(location.search);
if(QS.get('panel')) goPanel('p'+QS.get('panel'));
if(QS.get('autorun')==='1'){
  if(QS.get('jobs')) $('#jobs').value=QS.get('jobs');
  if(QS.get('seed')) $('#seed').value=QS.get('seed');
  if(QS.get('policy')) $('#policy').value=QS.get('policy');
  const iv=setInterval(()=>{
    if($('#policy').options.length>0){clearInterval(iv);$('#run').click();}
  },120);
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    """HTTP 服务端：前端页面、运行 API、看板静态文件全部走这里。

    只允许 run_YYYYmmdd_HHMMSS_<策略键> 这种格式的运行编号穿过，保护
    磁盘路径不被 ../ 之类写入的路径穿越攻击。
    """

    server_version = "GreenCompute/1.0"
    auth_config = AUTH

    def log_message(self, fmt, *args):
        """受限访问日志：只记时间 / 来源 / 请求行 / 状态码 / 字节数。

        鉴权相关：原实现被 pass 掉，导致越权完全不可观测。默认 fmt 不含
        Authorization 头与请求正文，因此这里不会泄露凭据。
        """
        try:
            msg = fmt % args
        except Exception:
            msg = " ".join(str(a) for a in args)
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {self.address_string()} {msg}\n")

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        """回复客户端：先发状态行与头，再发正文，确保 Content-Length 准确。

        注意：状态行/响应头只能发送一次，重复发送会让 Content-Length 与实际
        字节数错位，客户端解析必然出错。extra 用于 401 的 WWW-Authenticate。
        """
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200,
              extra: dict[str, str] | None = None) -> None:
        """以 UTF-8 JSON 回复；中文不转义，便于直接读接口。"""
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra=extra)

    def _unauthorized(self, reason: str) -> None:
        """统一 401：不区分无凭据 / 错误 / 过期 / 未配置，避免凭据探测。"""
        path = self.path.split("?")[0]
        sys.stderr.write(f"[AUTH] 401 {self.address_string()} {self.command} "
                         f"{path} reason={reason}\n")
        self._json({"error": "未授权：需要有效的访问凭据"}, 401,
                   extra={"WWW-Authenticate": WWW_AUTHENTICATE})

    def _guard(self, method: str) -> bool:
        """所有路由的统一入口判定；失败时已自行发出 401。"""
        path = self.path.split("?")[0]
        result = self.auth_config.check(self.headers, self.client_address,
                                        method, path)
        if result.ok:
            return True
        self._unauthorized(result.reason)
        return False

    def do_GET(self) -> None:  # noqa: N802
        """GET 路由：首页、策略列表、评测看板、单次运行结果与看板文件。

        所有带运行编号的路由都先过 safe_run_id，非法编号直接 400，
        不合法就根本拼不出路径，这是路径穿越的防线。
        分级鉴权：写接口、运行轨迹与看板文件过 _guard；公开只读 GET 放行。
        """
        if not self._guard("GET"):
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.replace("{ver}", PACK.version).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/policies":
            self._json({"policies": [{"key": k, "name": v.name, "desc": v.description}
                                      for k, v in POLICIES.items()]})
        elif path == "/api/eval":
            self._json(eval_board())
        elif path == "/api/runs":
            self._json({"runs": list_runs()})
        elif path.startswith("/api/run/"):
            rid = safe_run_id(path.rsplit("/", 1)[-1])
            if rid is None:
                self._json({"error": "运行编号格式不合法"}, 400)
                return
            with _LOCK:
                data = _STORE.get(rid)
            if not data:
                # 重启后 _STORE 为空，按 P2-3.2 从磁盘按需回读历史产物
                data = load_run_from_disk(rid)
            if not data:
                self._json({"error": "未找到该次运行"}, 404)
            else:
                self._json(data)
        elif path.startswith("/console/"):
            rid = safe_run_id(path.rsplit("/", 1)[-1])
            if rid is None:
                self._json({"error": "运行编号格式不合法"}, 400)
                return
            f = RUNS_DIR / rid / "console.html"
            if not f.exists():
                self._json({"error": "未找到该次运行的看板"}, 404)
            else:
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        else:
            self._json({"error": "未找到的路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        """POST /api/run：鉴权 + 请求体上限 + 参数校验后跑一次，返回结果 JSON。"""
        if not self._guard("POST"):
            return
        if self.path.split("?")[0] != "/api/run":
            self._json({"error": "未找到的路径"}, 404)
            return
        try:
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._json({"error": "Content-Length 不合法"}, 400)
                return
            if n < 0 or n > MAX_BODY:
                # 超限不读取正文，直接关闭连接，避免残留字节污染后续请求
                self.close_connection = True
                self._json({"error": f"请求体过大（上限 {MAX_BODY} 字节）"}, 413)
                return
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            jobs_n = max(5, min(300, int(body.get("jobs", 60))))
            seed = int(body.get("seed", 20261111))
            key = body.get("policy", "full")
            if key not in POLICIES:
                raise ValueError(f"未知策略组 {key}")
            self._json(run_once(jobs_n, seed, key))
        except (ValueError, TypeError) as exc:
            self._json({"error": f"请求参数不合法：{exc}"}, 400)
        except Exception as exc:
            # F1 加固：不向客户端回显内部异常类型/信息（防信息泄露），
            # 仅在服务端 stderr 记录完整异常，兼顾可观测性与安全性。
            sys.stderr.write(
                f"[ERROR] 未捕获异常 {self.command} {self.path.split('?')[0]}："
                f"{type(exc).__name__}: {exc}\n")
            self._json({"error": "服务内部错误，请稍后重试"}, 500)


def main() -> int:
    """命令行入口：解析 --host/--port/--open，启动线程化 HTTP 服务，Ctrl+C 退出。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="绿算调度操作台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    for line in AUTH.startup_messages():
        print(f"[AUTH] {line}")
    if not AUTH.may_start:
        print("[AUTH] 启动中止（fail-closed）：请先执行 "
              "`python app/auth_tool.py generate` 或 `rotate`，"
              "或将 GCS_AUTH_MODE 设为 loopback / off（仅限可信本机调试）。")
        return 2
    if AUTH.enabled and not AUTH.token_hash:
        print(f"[AUTH] 当前绑定 {args.host}；未配置凭据时，"
              "非回环来源的写接口与轨迹接口一律 401。")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"操作台已启动：{url}")
    print("按 Ctrl+C 停止。")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
