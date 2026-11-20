const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; }

function tagClass(status){
  if (status==='已完成') return 'tag tag-ok';
  if (status==='失败闭') return 'tag tag-fail';
  if (status==='执行中') return 'tag tag-run';
  return 'tag';
}

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card ${w.status==='失败闭'?'fail-closed':''}">
      <h3>${w.table_name}</h3>
      <p class="muted">${w.start_date} → ${w.end_date}</p>
      <span class="${tagClass(w.status)}">${w.status}</span>
      ${w.status==='失败闭' ? `<p><span class="retention">留存号 ${w.retention_no||'无'}</span></p>` : ''}
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count} · 失败 ${w.failed_count} · 待重放 ${w.pending_count}</p>
      ${w.status==='失败闭' ? '<p class="pill">半窗结局：失败闭（可续灌）</p>' : ''}
    </article>`).join('')}</div>`;
}

function winOptions(selId){ return windows.map(w=>`<option value="${w.id}" ${String(selId)===String(w.id)?'selected':''}>#${w.id} ${w.table_name}（${w.status}）</option>`).join(''); }

function renderPlan(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function renderReplay(){
  await loadWindows();
  const wid = windows[0]?.id;
  const slices = wid ? (await api(`/api/windows/${wid}/slices`)).items : [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions(wid)}</select></label>
    <div class="actions">
      <button class="primary" id="replay-all" type="button">整窗重放</button>
    </div>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card ${s.status==='失败'?'fail-closed':''}">
        <h3>${s.slice_date}</h3>
        <span class="${s.status==='失败'?'tag tag-fail':s.status==='已落库'?'tag tag-ok':'tag'}">${s.status}</span>
        <p class="muted">行数 ${s.row_count}</p>
        ${s.error_text?`<p class="err">${s.error_text}</p>`:''}
        <button data-id="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放本片</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(e.message,true); await draw(); await loadWindows(); }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  document.getElementById('replay-all').onclick = async ()=>{
    try{
      const id = document.getElementById('wid').value;
      const j = await api(`/api/windows/${id}/replay`, {method:'POST', body:'{}'});
      setMsg(`整窗重放完成：${j.detail.loaded_count}/${j.detail.slice_count} 片落库`);
      await draw(); await loadWindows();
    }catch(e){ setMsg(e.message,true); await draw(); await loadWindows(); }
  };
  await draw();
}

// —— 半窗收口：残片列表 / 窗口详情 / 失败卡片 / 进度目录 四面板并排 ——
async function renderClosure(){
  await loadWindows();
  const firstFail = windows.find(w=>w.status==='失败闭') || windows[0];
  const wid = firstFail?.id;
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions(wid)}</select></label>
    <div class="actions">
      <button id="btn-arm" type="button">挂闸（下一片必失败）</button>
      <button class="primary" id="btn-replay" type="button">整窗重放（中途出错即失败闭）</button>
      <button id="btn-resume" type="button">续灌纠偏</button>
      <button id="btn-refresh" type="button">刷新</button>
    </div>
    <p class="muted" id="head"></p>
    <div class="four-grid" id="panels"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    let d;
    try { d = (await api(`/api/windows/${id}/detail`)).detail; }
    catch(e){ setMsg(e.message,true); return; }
    setMsg('');
    document.getElementById('head').innerHTML =
      `窗口状态：<span class="${tagClass(d.status)}">${d.status}</span> ·
       可续灌：${d.resumable?'<b class="ok-text">是</b>':'否'} ·
       占用中：${d.locked?'是（有调度持锁）':'否'} ·
       续灌次数：${d.resume_count}`;
    // 续灌按钮严格吃后端同口径判定
    document.getElementById('btn-resume').disabled = !d.resumable;

    const remainSlices = d.slices.filter(s=>s.status!=='已落库');
    const failSlices = d.slices.filter(s=>s.status==='失败');
    const panelRemain = remainSlices.length ? remainSlices.map(s=>`
      <div class="pdir-row ${s.status==='失败'?'failed':''}">
        <span><span class="dot dot-${s.status==='失败'?'failed':'pending'}"></span> ${s.slice_date}</span>
        <span class="${s.status==='失败'?'err':'muted'}">${s.status}</span>
      </div>`).join('') : '<p class="muted">无残片，片界全齐</p>';

    const panelDetail = `
      <p class="muted">目标表：${d.table_name}</p>
      <p class="muted">区间：${d.start_date} → ${d.end_date}</p>
      <p>计划 ${d.slice_count} 片 · 已落库 ${d.loaded_count} · 失败 ${d.failed_count} · 待重放 ${d.pending_count}</p>
      <p>关闭时间：${d.closed_at||'—'}</p>
      <p>${d.lock?`持锁方：${d.lock.holder}`:'当前无锁'}</p>`;

    const panelFail = failSlices.length ? failSlices.map(s=>`
      <article class="card fail-closed" style="padding:0.6rem">
        <h3>${s.slice_date}</h3>
        <span class="tag tag-fail">失败闭</span>
        <p class="err">${s.error_text||'重放失败'}</p>
        <p class="retention">留存号 ${d.retention_no||'无'}</p>
      </article>`).join('') : '<p class="muted">当前没有失败片</p>';

    const panelProgress = d.slices.map(s=>`
      <div class="pdir-row ${s.status==='已落库'?'loaded':s.status==='失败'?'failed':''}">
        <span><span class="dot dot-${s.status==='已落库'?'loaded':s.status==='失败'?'failed':'pending'}"></span> ${s.slice_date}</span>
        <span class="${s.status==='失败'?'err':s.status==='已落库'?'ok-text':'muted'}">${s.status}${s.status==='已落库'?` ${s.row_count}行`:''}</span>
      </div>`).join('');

    const panelArchive = d.archives.length ? d.archives.map(a=>`
      <div class="pdir-row"><span>${a.slice_date}</span><span class="retention">${a.retention_no} · ${a.row_count}行</span></div>
    `).join('') : '<p class="muted">尚无历史留存档</p>';

    document.getElementById('panels').innerHTML = `
      <div class="card"><h4>残片列表</h4>${panelRemain}</div>
      <div class="card"><h4>窗口详情</h4>${panelDetail}</div>
      <div class="card"><h4>失败卡片</h4>${panelFail}</div>
      <div class="card"><h4>进度目录</h4><div class="progress-dir">${panelProgress}</div>
        <h4 style="margin-top:0.7rem">历史留存档</h4><div class="progress-dir">${panelArchive}</div></div>`;
  };

  document.getElementById('wid').onchange = draw;
  document.getElementById('btn-refresh').onclick = draw;
  document.getElementById('btn-arm').onclick = async ()=>{
    try{
      const id = document.getElementById('wid').value;
      const items = (await api(`/api/windows/${id}/slices`)).items.filter(s=>s.status==='待重放');
      if(!items.length){ setMsg('没有待重放片可挂闸',true); return; }
      await api(`/api/windows/${id}/arm-failure`, {method:'POST', body: JSON.stringify({slice_id: items[0].id})});
      setMsg(`已对 ${items[0].slice_date} 挂闸，下一次重放该片区会失败并收口`);
    }catch(e){ setMsg(e.message,true); }
  };
  document.getElementById('btn-replay').onclick = async ()=>{
    try{
      const id = document.getElementById('wid').value;
      const j = await api(`/api/windows/${id}/replay`, {method:'POST', body:'{}'});
      setMsg(`整窗重放完成：${j.detail.loaded_count}/${j.detail.slice_count} 片落库`);
    }catch(e){ setMsg(e.message,true); }
    await draw(); await loadWindows();
  };
  document.getElementById('btn-resume').onclick = async ()=>{
    try{
      const id = document.getElementById('wid').value;
      const j = await api(`/api/windows/${id}/resume`, {method:'POST', body: JSON.stringify({holder:'仓管'})});
      setMsg(`续灌纠偏完成：窗口已转 ${j.detail.status}，${j.detail.loaded_count}/${j.detail.slice_count} 片落库`);
    }catch(e){ setMsg(e.message,true); }
    await draw(); await loadWindows();
  };
  await draw();
}

async function renderCoverage(){
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${r.summary}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderCoverage();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function show(tab){
  setMsg(''); await loadWindows();
  if (tab==='windows') renderWindows();
  if (tab==='plan') renderPlan();
  if (tab==='replay') await renderReplay();
  if (tab==='closure') await renderClosure();
  if (tab==='coverage') renderCoverage();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('windows');
