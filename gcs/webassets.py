"""共享前端资源（单一来源，防漂移）。

`DAG_JS` 同时被 `gcs/report.py`（静态看板）与 `app/server.py`（交互操作台）引用，
消除原先两份 `drawDag` 的重复与漂移；`lastDag=g` 由操作台复用做 resize 重绘，
静态看板无副作用（浏览器隐式全局）。

`esc()` 是前端 HTML 插值的**唯一**转义入口：所有写入 innerHTML 的动态值先经它
原样处理一次；已经使用 textContent / DOM property（title、className、value）的
通道不再转义，避免「遗漏转义」与「重复转义」两类问题。
"""
from __future__ import annotations

DAG_JS = """
function esc(v){
  return String(v==null?'':v)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#x27;');
}
function drawDag(g){
  lastDag=g;
  const host=document.getElementById('dag');host.innerHTML='';
  const W=host.clientWidth,H=host.clientHeight,padL=8,padT=10,gap=8;
  if(W<120) return;
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
    d.className='node '+(n.loop?'loop ':'')+(n.status==='done'?'done':n.status);
    d.style.cssText+=`left:${p.x}px;top:${p.y}px;width:${p.w}px;height:${p.h}px`;
    d.innerHTML=`<div class="id">${esc(n.agent)} · ${esc(n.type)}</div>${esc(n.label)}<div class="st">${n.status==='done'?'✓':''}</div>`;
    d.title=n.detail||'';host.appendChild(d);});
}
"""
