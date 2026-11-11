"""零依赖可视化操作台：只用 Python 标准库起一个 HTTP 服务。

为什么自己写而不用 Flask/FastAPI：
1. 参赛要求「一条命令跑起来」，评委机器上不保证有第三方框架；
2. 本机零安装依赖 = 现场演示不会因为 pip 失败而开天窗；
3. 前端用原生 JS + CSS，避免 CDN 断网导致白屏（离线评审场景很常见）。

四区块（对应演示动线）：
    ① 任务发起：填作业数/种子/策略组 → 触发一次真实编排
    ② DAG 拓扑：八个状态机与节点状态，返工边高亮
    ③ 运行轨迹：逐条事件，含角色、节点、缺陷归因
    ④ 评测看板：五组对照实验结果与相对提升

启动：
    python app/server.py            # 默认 http://127.0.0.1:8000
    python app/server.py --port 8080
"""

from __future__ import annotations

import argparse
import json
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

PACK = KnowledgePack.load(ROOT / "knowledge" / "ningxia_cluster.yaml")
RUNS_DIR = ROOT / "runs"

# 内存中的最近若干次运行，供看板取用
_STORE: dict[str, dict] = {}
_LOCK = threading.Lock()


def run_once(jobs_n: int, seed: int, policy_key: str) -> dict:
    jobs = make_jobs(PACK, n=jobs_n, seed=seed)
    llm = ModelGateway(token_budget=int(PACK.rework_budget["max_tokens"]))
    art = Orchestrator(PACK, llm=llm, policy=POLICIES[policy_key]).run(jobs)

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_dir = RUNS_DIR / f"{run_id}_{policy_key}"
    out_dir.mkdir(parents=True, exist_ok=True)
    render_console(art, PACK.version, out_dir / "console.html")
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
        "events": [e.as_dict() for e in art.events],
        "narrative": art.narrative,
        "llm": art.llm,
        "elapsed_s": round(art.elapsed_s, 3),
        "console_url": f"/console/{out_dir.name}",
    }
    with _LOCK:
        _STORE[out_dir.name] = payload
    return payload


def eval_board() -> dict:
    p = ROOT / "evaluation" / "summary.json"
    if not p.exists():
        return {"available": False, "message": "尚未运行对照实验，请先执行 python evaluation/run_eval.py"}
    return {"available": True, "groups": json.loads(p.read_text(encoding="utf-8"))}


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
.ev{display:grid;grid-template-columns:42px 1fr;gap:8px;padding:7px 0;border-bottom:1px dashed #EDF0F5}
.ev .sq{font-size:10.5px;color:#A6B0C0}
.ev .st{display:inline-block;font-size:9.5px;padding:1px 6px;border-radius:3px;background:#E8EEF7;color:#2F6DB5;margin-right:6px}
.ev .ag{color:#8B96A8;font-size:10.5px;margin-left:6px}
.ev .dt{color:#3A4553;line-height:1.45;margin-top:2px}
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
<div class="sub">本机运行 · 数据由确定性模拟器生成，可复现</div></header>
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
    <div class="hint">每个事件带状态、角色、节点与细节；缺陷会给出责任角色与定向返工指令。</div>
    <div class="trace" id="trace"></div>
  </div>
</section>
<section class="panel" id="p4">
  <div class="card"><h2>五组对照实验看板</h2>
    <div class="hint">同一模拟器数据、同一知识包、同一评测脚本，3 个种子重复 3 次，报均值±标准差。</div>
    <div id="board"></div>
  </div>
</section>
</main>
<script>
const $=s=>document.querySelector(s);
let lastDag=null;
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
fetch('/api/policies').then(r=>r.json()).then(d=>{
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
    ['返工轮次',String(d.rounds),'rubric '+(d.rubric?d.rubric.score:'-')+' / 80','o']];
  $('#kpis').innerHTML=k.map(x=>`<div class="kpi ${x[3]}"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="s">${x[2]}</div></div>`).join('');
  $('#narr').textContent='运行摘要：'+d.narrative;
  drawDag(d.dag);
  $('#trace').innerHTML=d.events.map(e=>`<div class="ev"><div class="sq">#${String(e.seq).padStart(2,'0')}</div><div>`+
    `<span class="st">${e.state}</span><span class="ag">${e.agent} · ${e.node}</span><div class="dt">${e.detail}</div></div></div>`).join('');
}
$('#run').onclick=async()=>{
  $('#run').disabled=true;$('#msg').innerHTML='<div class="muted">编排执行中，请稍候……</div>';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({jobs:parseInt($('#jobs').value,10),seed:parseInt($('#seed').value,10),policy:$('#policy').value})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'运行失败');
    $('#msg').innerHTML=`<div class="muted">已完成：${d.run_id}（耗时 ${d.elapsed_s}s，模型 ${d.llm.mode}，token ${d.llm.tokens_total}）</div>`;
    renderRun(d);
  }catch(e){$('#msg').innerHTML=`<div class="err">运行失败：${e.message}</div>`;}
  $('#run').disabled=false;
};
async function loadBoard(){
  const d=await (await fetch('/api/eval')).json();
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
    server_version = "GreenCompute/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.replace("{ver}", PACK.version).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/policies":
            self._json({"policies": [{"key": k, "name": v.name, "desc": v.description}
                                      for k, v in POLICIES.items()]})
        elif path == "/api/eval":
            self._json(eval_board())
        elif path.startswith("/api/run/"):
            rid = path.rsplit("/", 1)[-1]
            with _LOCK:
                data = _STORE.get(rid)
            if not data:
                self._json({"error": "未找到该次运行"}, 404)
            else:
                self._json(data)
        elif path.startswith("/console/"):
            rid = path.rsplit("/", 1)[-1]
            f = RUNS_DIR / rid / "console.html"
            if not f.exists():
                self._json({"error": "未找到该次运行的看板"}, 404)
            else:
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        else:
            self._json({"error": "未找到的路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/run":
            self._json({"error": "未找到的路径"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            jobs_n = max(5, min(300, int(body.get("jobs", 60))))
            seed = int(body.get("seed", 20261111))
            key = body.get("policy", "full")
            if key not in POLICIES:
                raise ValueError(f"未知策略组 {key}")
            self._json(run_once(jobs_n, seed, key))
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="绿算调度操作台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

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
