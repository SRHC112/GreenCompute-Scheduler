"""看板与报告导出：把一次运行变成可截图、可复核的产物。

产出的 console.html 是零依赖单文件（无 CDN、无外链），
既能在参赛演示里当“操作台”展示，也能直接截图进方案书。
"""

from __future__ import annotations

import json
from pathlib import Path

from .orchestrator import Artifact

NAVY = "#1F3352"
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
     background:#F2F4F8;color:#232A33;-webkit-font-smoothing:antialiased}
.wrap{max-width:1500px;margin:0 auto;padding:0 26px 40px}
header{background:#1F3352;color:#fff;padding:22px 26px 20px;border-bottom:5px solid #E08A3C}
header .kicker{font-size:13px;color:#9FB6D8;letter-spacing:.04em}
header h1{font-size:27px;font-weight:700;margin-top:6px;letter-spacing:.02em}
header .meta{margin-top:8px;font-size:12.5px;color:#B9C9E2}
header .meta b{color:#fff;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:20px 0 6px}
.kpi{background:#fff;border:1px solid #E2E7EF;border-radius:6px;padding:14px 16px;
     border-top:3px solid #2F6DB5}
.kpi .l{font-size:12px;color:#7A8699}
.kpi .v{font-size:26px;font-weight:700;color:#1F3352;margin-top:6px;line-height:1.1}
.kpi .s{font-size:11.5px;color:#8B96A8;margin-top:4px}
.kpi.g{border-top-color:#1F8A70}.kpi.o{border-top-color:#E08A3C}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;margin-top:14px}
.card{background:#fff;border:1px solid #E2E7EF;border-radius:6px;padding:16px 18px}
.card h2{font-size:14.5px;color:#1F3352;font-weight:700;margin-bottom:2px}
.card .hint{font-size:11.5px;color:#8B96A8;margin-bottom:12px}
#dag{position:relative;height:400px}
.node{position:absolute;border-radius:5px;border:1px solid #C9D3E3;background:#F7F9FC;
      padding:6px 8px;font-size:11.5px;line-height:1.25;overflow:hidden}
.node .id{font-size:9.5px;color:#8B96A8;letter-spacing:.03em}
.node.done{background:#EAF3EE;border-color:#1F8A70}
.node.failed{background:#FBECEC;border-color:#C0392B}
.node.loop{border-style:dashed;background:#FDF3E8;border-color:#E08A3C}
.node .st{position:absolute;top:5px;right:6px;font-size:9px;color:#1F8A70}
.legend{display:flex;gap:16px;font-size:11px;color:#7A8699;margin-top:6px;flex-wrap:wrap}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.trace{height:400px;overflow:auto;font-size:12px}
.ev{display:grid;grid-template-columns:44px 1fr;gap:8px;padding:7px 0;border-bottom:1px dashed #EDF0F5}
.ev .sq{font-size:10.5px;color:#A6B0C0;padding-top:2px}
.ev .st{display:inline-block;font-size:9.5px;padding:1px 6px;border-radius:3px;background:#E8EEF7;
        color:#2F6DB5;margin-right:6px;letter-spacing:.02em}
.ev .st.REWORK{background:#FDF0E2;color:#B4702A}
.ev .st.REFLECT{background:#FDF0E2;color:#B4702A}
.ev .st.REPORT{background:#E7F3EE;color:#1F8A70}
.ev .ag{color:#8B96A8;font-size:10.5px;margin-left:4px}
.ev .dt{color:#3A4553;line-height:1.45;margin-top:2px}
.bars{margin-top:6px}
.bar{margin-bottom:11px}
.bar .top{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px}
.bar .top span:last-child{color:#5A6779}
.track{height:8px;background:#EDF0F5;border-radius:4px;overflow:hidden}
.fill{height:100%;border-radius:4px;background:#2F6DB5}
.fill.ok{background:#1F8A70}.fill.no{background:#C0392B}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{background:#1F3352;color:#fff;font-weight:600;padding:7px 8px;text-align:left;font-size:11px}
td{padding:6px 8px;border-bottom:1px solid #EDF0F5;color:#3A4553}
tr:nth-child(even) td{background:#F8FAFD}
.bad{color:#C0392B;font-weight:600}.good{color:#1F8A70}
.scroll{max-height:300px;overflow:auto}
footer{margin-top:18px;font-size:11px;color:#98A3B3;display:flex;justify-content:space-between}
"""


def _dag_js() -> str:
    """内联到看板的原生 JS：按层级画 DAG 节点与依赖曲线，返工回路用橙色虚线标出。"""
    return """
function drawDag(g){
  const host=document.getElementById('dag');
  const levels=g.levels, W=host.clientWidth, H=host.clientHeight;
  const padL=8,padR=8,padT=10,padB=10;
  const colW=(W-padL-padR)/levels.length;
  const pos={};
  levels.forEach((ids,li)=>{
    const n=ids.length, gap=8, h=(H-padT-padB-gap*(n-1))/n;
    ids.forEach((id,ni)=>{
      pos[id]={x:padL+li*colW+4,y:padT+ni*(h+gap),w:colW-26,h:Math.min(h,52)};
    });
  });
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width',W);svg.setAttribute('height',H);
  svg.style.position='absolute';svg.style.left=0;svg.style.top=0;svg.style.pointerEvents='none';
  g.nodes.forEach(n=>{
    n.deps.forEach(d=>{
      const a=pos[d],b=pos[n.id];if(!a||!b)return;
      const x1=a.x+a.w,y1=a.y+16,x2=b.x,y2=b.y+16,dx=Math.max(18,(x2-x1)/2);
      const p=document.createElementNS('http://www.w3.org/2000/svg','path');
      p.setAttribute('d',`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`);
      p.setAttribute('fill','none');
      p.setAttribute('stroke',n.loop?'#E08A3C':'#BFD0E6');
      p.setAttribute('stroke-width',n.loop?1.6:1.2);
      if(n.loop)p.setAttribute('stroke-dasharray','4,3');
      svg.appendChild(p);
    });
  });
  host.appendChild(svg);
  g.nodes.forEach(n=>{
    const p=pos[n.id];if(!p)return;
    const d=document.createElement('div');
    d.className='node '+(n.loop?'loop ':'')+(n.status==='done'?'done':n.status);
    d.style.cssText+=`left:${p.x}px;top:${p.y}px;width:${p.w}px;height:${p.h}px`;
    d.innerHTML=`<div class="id">${n.agent} · ${n.type}</div>${n.label}`+
      `<div class="st">${n.status==='done'?'✓':''}</div>`;
    d.title=n.detail||'';
    host.appendChild(d);
  });
}
"""


def render_console(art: Artifact, pack_version: str, out: Path) -> Path:
    """把一次运行渲染成单文件 HTML 看板：KPI、DAG、事件轨迹、rubric、成本明细。

    不引任何 CDN，双击即可打开，答辩现场断网也能演示。
    """
    data = art.to_dict()
    m = data["metrics"]
    rubric = data["rubric"][-1] if data["rubric"] else {"dims": [], "score": 0, "threshold": 80}
    events = data["events"]
    top_jobs = sorted(data["results"], key=lambda r: r["cost"], reverse=True)[:14]

    kpis = [
        ("SLA 达成率", f"{m['sla_rate'] * 100:.1f}%", "硬约束：截止时间", ""),
        ("单位算力成本", f"¥{m['cost_per_gpu_hour']:.2f}", "元 / GPU·h", "o"),
        ("平均绿电占比", f"{m['avg_green_share'] * 100:.1f}%", "作业时段加权", "g"),
        ("平均排队", f"{m['avg_wait_min']:.0f} min", f"P95 {m['p95_wait_min']:.0f} min", ""),
        ("返工轮次", f"{data['rounds']}", f"rubric {rubric['score']} / {rubric['threshold']}", "o"),
    ]
    kpi_html = "".join(
        f'<div class="kpi {c}"><div class="l">{l}</div><div class="v">{v}</div>'
        f'<div class="s">{s}</div></div>' for l, v, s, c in kpis)

    ev_html = "".join(
        f'<div class="ev"><div class="sq">#{e["seq"]:02d}</div><div>'
        f'<span class="st {e["state"]}">{e["state"]}</span>'
        f'<span class="ag">{e["agent"]}</span>'
        f'<div class="dt">{e["detail"]}</div></div></div>'
        for e in events)

    dims = rubric.get("dims", [])
    maxw = max([d["weight"] for d in dims], default=1)
    bars = "".join(
        f'<div class="bar"><div class="top"><span>{d["name"]}</span>'
        f'<span>{d["weight"]} 分 · {"通过" if d["passed"] else "未通过"}</span></div>'
        f'<div class="track"><div class="fill {"ok" if d["passed"] else "no"}" '
        f'style="width:{d["weight"] / maxw * 100:.0f}%"></div></div></div>'
        for d in dims)

    rows = "".join(
        f'<tr><td>{r["job_id"]}</td><td>{r["type"]}</td><td>{r["pool_id"]}</td>'
        f'<td>{r["start_min"]}</td><td>{r["wait_min"]}</td>'
        f'<td>¥{r["cost"]:.1f}</td><td>{r["green_share"] * 100:.0f}%</td>'
        f'<td class="{"good" if r["sla_ok"] else "bad"}">{"达成" if r["sla_ok"] else "超时"}</td></tr>'
        for r in top_jobs)

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>绿算调度 · 运行看板</title><style>{CSS}</style></head><body>
<header><div class="wrap" style="padding:0">
  <div class="kicker">2026 第五届“未来杯”创新大赛 · 赛道一 Agent 编排创新应用</div>
  <h1>绿算调度 · 运行看板</h1>
  <div class="meta">策略组 <b>{data["policy"]}</b>　·　知识包 <b>{pack_version}</b>　·
    作业 <b>{m["jobs"]}</b> 个　·　模型 <b>{data["llm"].get("mode", "-")}</b>　·
    token <b>{data["llm"].get("tokens_total", 0)}</b>　·　耗时 <b>{data["elapsed_s"]}s</b></div>
</div></header>
<div class="wrap">
  <div class="kpis">{kpi_html}</div>
  <div class="row">
    <div class="card"><h2>DAG 拓扑与节点状态</h2>
      <div class="hint">虚线为返工边：验证不通过时才激活，只退责任节点，不整链重跑</div>
      <div id="dag"></div>
      <div class="legend"><span><i style="background:#1F8A70"></i>已完成</span>
        <span><i style="background:#E08A3C"></i>返工节点</span>
        <span><i style="background:#C9D3E3"></i>依赖边</span></div>
    </div>
    <div class="card"><h2>运行轨迹</h2>
      <div class="hint">每个事件都带状态、角色与节点，可直接用于复盘与答辩</div>
      <div class="trace">{ev_html}</div></div>
  </div>
  <div class="row">
    <div class="card"><h2>rubric 评分</h2>
      <div class="hint">六维加权，阈值 {rubric.get("threshold")} 分；不达阈值触发定向返工</div>
      <div class="bars">{bars}</div>
      <div class="hint" style="margin-top:14px">{art.narrative}</div></div>
    <div class="card"><h2>成本最高的作业</h2>
      <div class="hint">按成本降序，SLA 未达成的标红</div>
      <div class="scroll"><table><thead><tr><th>作业</th><th>类型</th><th>资源池</th>
      <th>开始</th><th>等待</th><th>成本</th><th>绿电</th><th>SLA</th></tr></thead>
      <tbody>{rows}</tbody></table></div></div>
  </div>
  <footer><span>绿算调度 GreenCompute-Scheduler · 数据由同一模拟器与同一知识包生成</span>
  <span>2026 未来杯</span></footer>
</div>
<script>
const G={json.dumps(data["dag"], ensure_ascii=False)};
{_dag_js()}
drawDag(G);
</script></body></html>"""
    out.write_text(html, encoding="utf-8")
    return out


def render_markdown(art: Artifact, out: Path) -> Path:
    """导出纯文本版运行报告：指标、rubric 明细与逐条运行轨迹，便于进 Git 留痕。"""
    d = art.to_dict()
    m = d["metrics"]
    lines = [
        f"# 运行报告 · {art.policy}",
        "",
        f"- 知识包：`{'knowledge/ningxia_cluster.yaml'}`",
        f"- 作业数：{m['jobs']}　SLA 达成：{m['sla_rate']:.1%}　"
        f"单位成本：¥{m['cost_per_gpu_hour']:.2f}/GPU·h　绿电：{m['avg_green_share']:.1%}",
        f"- 返工轮次：{d['rounds']}　token：{d['llm'].get('tokens_total', 0)}　"
        f"耗时：{d['elapsed_s']}s",
        "",
        "## rubric",
        "",
        "| 维度 | 权重 | 结果 |",
        "|---|---|---|",
    ]
    for dim in d["rubric"][-1]["dims"]:
        lines.append(f"| {dim['name']} | {dim['weight']} | {'通过' if dim['passed'] else '未通过'} |")
    lines += ["", f"**总分 {d['rubric'][-1]['score']} / 阈值 {d['rubric'][-1]['threshold']}**", "",
              "## 运行轨迹", ""]
    for e in d["events"]:
        lines.append(f"{e['seq']:02d}. `{e['state']}` {e['agent']} — {e['detail']}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
