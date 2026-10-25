// paint_gallery 真实浏览器验收驱动（Edge CDP, Node 24 原生 WebSocket/fetch, 零依赖）
// 用法: node tools/_gallery_browser.mjs phase1|phase2|corrupt
// 前置: 正式服务 8787 运行中; Edge 以 --remote-debugging-port=9224 启动
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const DEBUG_PORT = 9224;
const BASE = "http://127.0.0.1:8787";
const EVID = path.join(path.dirname(process.argv[1]), "paint_gallery_evidence");
fs.mkdirSync(EVID, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws = null;
let msgId = 0;
const pending = new Map();

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++msgId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(`timeout ${method}`)); }
    }, 30000);
  });
}

async function connect() {
  let target = null;
  for (let i = 0; i < 40 && !target; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
      target = list.find((t) => t.type === "page" && t.url.startsWith(BASE));
      if (!target && i > 10) target = list.find((t) => t.type === "page");
    } catch { /* Edge not ready yet */ }
    if (!target) await sleep(500);
  }
  if (!target) throw new Error("找不到 Edge page target");
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    }
  };
  await send("Page.enable");
  await send("Runtime.enable");
  return target;
}

async function evalJs(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("evaluate: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result.value;
}

async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  const p = path.join(EVID, name);
  fs.writeFileSync(p, Buffer.from(r.data, "base64"));
  console.log("  📸", name);
}

async function clickSel(sel, desc = "") {
  const pt = await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});
    if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();
    return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  if (!pt) throw new Error(`找不到元素 ${sel} ${desc}`);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: pt.x, y: pt.y, button: "left", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: pt.x, y: pt.y, button: "left", clickCount: 1 });
  await sleep(350);
  return pt;
}

async function typeInto(sel, text) {
  await clickSel(sel);
  await send("Input.insertText", { text });
  await sleep(600); // 超过防抖 250ms
}

async function setVal(sel, text) {
  await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});
    e.value=${JSON.stringify(text)};
    e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  await sleep(600);
}

async function waitGrid(minCards = 1) {
  for (let i = 0; i < 40; i++) {
    const n = await evalJs(`document.querySelectorAll('#grid .card').length`);
    if (n >= minCards) return n;
    await sleep(300);
  }
  throw new Error("页面卡片未渲染");
}

function assert(cond, msg) {
  if (!cond) throw new Error("断言失败: " + msg);
  console.log("  ✓", msg);
}

// ---------------------------------------------------------------- phase 1 ---
async function phase1() {
  await connect();
  await evalJs(`document.getElementById('btn-reset')?.click()`);
  await sleep(600);
  const n = await waitGrid(40);
  console.log(`首页渲染 ${n} 张卡片`);
  const meta = await evalJs(`document.getElementById('meta').textContent`);
  assert(/共 452 条记录/.test(meta), `meta 行: ${meta.slice(0, 80)}...`);
  await shot("01_home_grid.png");

  // 1) 搜索找回旧图 (hirasawa_yui)
  await typeInto("#q", "hirasawa_yui");
  const cnt1 = await evalJs(`document.getElementById('pg-info').textContent`);
  assert(/筛选 [1-9]\d* 条/.test(cnt1) && !/筛选 0 条/.test(cnt1), `搜索 hirasawa_yui → ${cnt1}`);
  await shot("02_search_hirasawa.png");

  // 2) 打开原图 (lightbox)
  await clickSel("#grid .card .imgbox img", "第一张缩略图");
  await sleep(800); // 等大图加载
  const lbVisible = await evalJs(`document.getElementById('lightbox').style.display`);
  assert(lbVisible === "flex", "lightbox 已打开");
  await shot("03_lightbox_original.png");
  await clickSel("#lb-close");
  await sleep(300);

  // 3) 对比: 提示词完全相同、seed 不同的两条记录（同题材确定性定位）
  await evalJs(`document.getElementById('btn-reset').click()`);
  await sleep(400);
  const all = await (await fetch(BASE + "/data")).json();
  const groups = new Map();
  for (const r of all.records) {
    if (!r.images[0]?.exists) continue;
    const k = r.en + "||" + r.zh;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }
  let pair = null;
  for (const g of groups.values()) {
    if (g.length >= 2 && g[0].seed !== g[1].seed) { pair = g; break; }
  }
  assert(!!pair, `找到同提示词不同 seed 的记录对 (seed ${pair?.[0]?.seed} / ${pair?.[1]?.seed})`);
  await setVal("#q", "");
  await setVal("#seed", pair[0].seed);
  const rid1 = await evalJs(`document.querySelector('#grid .card')?.dataset.rid`);
  assert(!!rid1, `seed ${pair[0].seed} 定位到卡片`);
  await clickSel(`#grid .card[data-rid="${rid1}"] .cmp`);
  await setVal("#seed", pair[1].seed);
  const rid2 = await evalJs(`document.querySelector('#grid .card')?.dataset.rid`);
  assert(!!rid2, `seed ${pair[1].seed} 定位到卡片`);
  await clickSel(`#grid .card[data-rid="${rid2}"] .cmp`);
  const cmpN = await evalJs(`cmp.length`);
  assert(cmpN === 2, `已选 ${cmpN}/2 张（跨筛选状态保持）`);
  await clickSel("#cmp-go");
  await sleep(600);
  const ov = await evalJs(`document.getElementById('overlay').style.display`);
  assert(ov === "block", "对比浮层已打开");
  const ovHas = await evalJs(`document.querySelectorAll('#ov-cols .ovpanel').length + ' 面板 / 含未记录: ' + document.getElementById('ov-cols').textContent.includes('未记录')`);
  assert(/2 面板/.test(ovHas) && /true$/.test(ovHas), `并排 ${ovHas}`);
  await shot("04_compare_side_by_side.png");
  await clickSel("#ov-close");
  await sleep(300);

  // 4) 缺失图占位 + 标记
  await evalJs(`document.getElementById('btn-reset').click()`);
  await sleep(300);
  await evalJs(`const s=document.getElementById('f-state'); s.value='none'; s.dispatchEvent(new Event('change',{bubbles:true}));`);
  await sleep(600);
  const missCnt = await evalJs(`document.getElementById('pg-info').textContent`);
  assert(/筛选 12[0-9] 条/.test(missCnt), `全部丢失筛选 → ${missCnt}`);
  const missCard = await evalJs(`document.querySelector('#grid .card').dataset.rid`);
  const missIid = await evalJs(`document.querySelector('#grid .card .cmp').dataset.iid`);
  await clickSel(`#grid .card[data-rid="${missCard}"] .chip[data-label="喜欢"]`);
  const on = await evalJs(`document.querySelector('#grid .card[data-rid="${missCard}"] .chip[data-label="喜欢"]').classList.contains('on')`);
  assert(on, "缺失图卡标记'喜欢'成功（chip 激活）");
  await shot("05_mark_missing.png");
  const marks = await (await fetch(BASE + "/marks")).json();
  assert(marks.marks[missIid] && marks.marks[missIid].labels.includes("喜欢"), "标记库落盘含该缺失图");

  // 5) 三图记录占位
  await evalJs(`document.getElementById('btn-reset').click()`);
  await sleep(300);
  const multi = await (await fetch(BASE + "/data")).json();
  const m3 = multi.records.find((r) => r.images.length === 3);
  assert(!!m3, `找到三图记录 ts=${m3.ts}`);
  await setVal("#q", "");
  await setVal("#seed", m3.seed);
  await sleep(400);
  const m3imgs = await evalJs(`document.querySelectorAll('#grid .card .imgbox .miss').length`);
  assert(m3imgs === 3, `三图记录显示 ${m3imgs} 张占位卡`);
  await shot("06_multi_placeholder.png");

  // 6) 复制参数
  await evalJs(`document.getElementById('btn-reset').click()`);
  await sleep(400);
  const copyRid = await evalJs(`document.querySelector('#grid .card').dataset.rid`);
  await clickSel(`#grid .card[data-rid="${copyRid}"] button[data-copy="params"]`);
  await sleep(500);
  const copied = await evalJs(`navigator.clipboard ? 'clipboard-api' : 'fallback'`);
  console.log("  复制通道:", copied);
  const fallbackVisible = await evalJs(`document.getElementById('copymodal').style.display`);
  if (fallbackVisible === "flex") {
    const ta = await evalJs(`document.getElementById('copyta').value`);
    assert(ta.startsWith("seed="), "降级文本框含参数串");
  }
  await shot("07_copy_params.png");

  // 7) 快速连续切换不同图片标签
  await clickSel(`#grid .card:nth-child(1) .chip[data-label="喜欢"]`);
  await clickSel(`#grid .card:nth-child(2) .chip[data-label="可用"]`);
  await clickSel(`#grid .card:nth-child(3) .chip[data-label="需要重做"]`);
  await clickSel(`#grid .card:nth-child(1) .chip[data-label="可用"]`);
  await sleep(400);
  const rapid = await (await fetch(BASE + "/marks")).json();
  assert(Object.keys(rapid.marks).length >= 3, `快速切换后标记库 ${Object.keys(rapid.marks).length} 条`);
  await shot("08_rapid_toggles.png");

  // 8) 特殊字符按普通文本显示（真实数据含逗号/引号的提示词）
  const esc = await evalJs(`(()=>{const cards=[...document.querySelectorAll('#grid .card')];
    for(const c of cards){const p=c.querySelector('.prompt');
      if(p.textContent.includes('&')||p.textContent.includes('"')||p.textContent.includes(',')){
        return {text:p.textContent.slice(0,60), rawLen:p.textContent.length, htmlLen:p.innerHTML.length};}}
    return null;})()`);
  console.log("  特殊字符样本:", esc ? esc.text : "（本页无 & \" , 样本，夹具测试已覆盖）");

  console.log("PHASE1 DONE");
  ws.close();
}

// ---------------------------------------------------------------- phase 2 ---
async function phase2() {
  await connect();
  await send("Page.reload", {});
  await sleep(2500);
  await waitGrid(40);
  const likedOn = await evalJs(`(()=>{const chips=[...document.querySelectorAll('.chip.on[data-label="喜欢"]')];
    return chips.length;})()`);
  assert(likedOn >= 1, `服务重启+页面重载后 '喜欢' 标记仍激活 (${likedOn} 处)`);
  await shot("09_after_restart_marks_persist.png");
  const marks = await (await fetch(BASE + "/marks")).json();
  console.log("  标记库:", JSON.stringify(Object.fromEntries(
    Object.entries(marks.marks).map(([k, v]) => [k.slice(0, 8) + "…", v.labels])), null, 0));
  console.log("PHASE2 DONE");
  ws.close();
}

// ---------------------------------------------------------------- corrupt ---
async function phaseCorrupt() {
  // 指向 8789 (损坏标记库测试实例) 的标签页
  const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
  let t = list.find((p) => p.type === "page" && p.url.includes(":8789"));
  if (!t) throw new Error("未找到 8789 标签页（请先在 Edge 打开）");
  ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    }
  };
  await send("Page.enable");
  await send("Runtime.enable");
  await sleep(1500);
  const banner = await evalJs(`(()=>{const b=document.getElementById('corrupt-banner');
    return {shown:b.style.display!=='none', text:b.textContent.slice(0,120)};})()`);
  assert(banner.shown, "损坏标记库页面显示红色警告横幅");
  console.log("  横幅:", banner.text.replace(/\n/g, " | ").slice(0, 110), "…");
  await shot("10_corrupt_marks_banner.png");
  console.log("CORRUPT PHASE DONE");
  ws.close();
}

// ---------------------------------------------------------------- phase 3 ---
async function clickFast(sel) {
  const pt = await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});
    if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();
    return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  if (!pt) throw new Error(`找不到元素 ${sel}`);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: pt.x, y: pt.y, button: "left", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: pt.x, y: pt.y, button: "left", clickCount: 1 });
  await sleep(80); // 快速连点: 仅 80ms 间隔, 不等上一请求完成
}

async function phase3() {
  await connect();
  await evalJs(`document.getElementById('btn-reset')?.click()`);
  await sleep(600);
  await waitGrid(40);
  // 找一张当前无任何标记的卡（452 条里 4 条标记, 第一页必有）
  const tgt = await evalJs(`(()=>{const cards=[...document.querySelectorAll('#grid .card')];
    for(const c of cards){const iid=c.querySelector('.cmp').dataset.iid;
      if(!MARKS.marks[iid]) return c.dataset.rid+"|"+iid;}
    return null;})()`);
  assert(!!tgt, "找到无标记基准卡");
  const [rid, iid] = tgt.split("|");
  console.log("  基准卡:", rid.slice(0, 8) + "…", iid.slice(0, 8) + "…");

  // 1) 同图快速连点 喜欢 + 可用（修复前: 两请求同读旧状态, 只留最后一个）
  await clickFast(`#grid .card[data-rid="${rid}"] .chip[data-label="喜欢"]`);
  await clickFast(`#grid .card[data-rid="${rid}"] .chip[data-label="可用"]`);
  await sleep(1500);
  let m = await (await fetch(BASE + "/marks")).json();
  const l1 = m.marks[iid]?.labels || [];
  assert(l1.includes("喜欢") && l1.includes("可用"),
    `快速连点后两标签都在: [${l1.join(",")}]（无丢失）`);

  // 2) 同图双击切换: 两次独立点击"喜欢" → toggle 往返, 最终状态应与点击前一致且无丢失
  const beforeDbl = (m.marks[iid]?.labels || []).slice().sort().join(",");
  await clickSel(`#grid .card[data-rid="${rid}"] .chip[data-label="喜欢"]`);
  await clickSel(`#grid .card[data-rid="${rid}"] .chip[data-label="喜欢"]`);
  await sleep(1200);
  m = await (await fetch(BASE + "/marks")).json();
  const afterDbl = (m.marks[iid]?.labels || []).slice().sort().join(",");
  assert(afterDbl === beforeDbl,
    `双击往返无损: [${afterDbl}] == 点击前 [${beforeDbl}]（中间态无丢失）`);

  // 3) 备注与标签连续修改: 点"需要重做" + 立刻写备注
  await clickFast(`#grid .card[data-rid="${rid}"] .chip[data-label="需要重做"]`);
  await evalJs(`(()=>{const inp=document.querySelector('input.note[data-iid="${iid}"]');
    inp.value='竞态补测备注'; inp.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await sleep(1500);
  m = await (await fetch(BASE + "/marks")).json();
  const ent = m.marks[iid] || {};
  assert((ent.labels || []).includes("需要重做") && ent.note === "竞态补测备注",
    `标签与备注连续修改均生效: [${(ent.labels || []).join(",")}] note="${ent.note}"`);

  // 4) UI 与确认态一致（syncImgUI 覆盖卡片）
  const ui = await evalJs(`(()=>{const on=[...document.querySelectorAll('#grid .card[data-rid="${rid}"] .chip.on')].map(b=>b.dataset.label);
    const note=document.querySelector('input.note[data-iid="${iid}"]').value;
    return on.join(",")+"|"+note;})()`);
  const uiLabels = ui.split("|")[0].split(",").filter(Boolean).sort().join(",");
  const wantLabels = (ent.labels || []).slice().sort().join(",");
  assert(uiLabels === wantLabels && ui.split("|")[1] === (ent.note || ""),
    `卡片 UI 与服务端确认态一致: [${uiLabels}] note="${ui.split("|")[1]}"`);
  await shot("11_race_fix.png");
  console.log("  最终标记库:", JSON.stringify(m.marks[iid]));
  console.log("PHASE3 DONE");
  ws.close();
}

// ---------------------------------------------------------------- phase 3b --
async function phase3b() {
  await connect();
  await send("Page.reload", {});
  await sleep(2500);
  await waitGrid(40);
  // 标记卡可能不在第一页: 勾选"只看喜欢"把它们带到当前页再核对
  await evalJs(`(()=>{const c=document.getElementById('f-liked'); c.checked=true;
    c.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await sleep(800);
  const m = await (await fetch(BASE + "/marks")).json();
  const liked = Object.keys(m.marks).filter((k) => m.marks[k].labels.includes("喜欢"));
  assert(liked.length >= 1, `重启后标记库 ${Object.keys(m.marks).length} 条, 含"喜欢" ${liked.length} 条`);
  let checked = 0;
  for (const iid of liked) {
    const ui = await evalJs(`(()=>{const on=[...document.querySelectorAll('.chip.on[data-iid="${iid}"]')].map(b=>b.dataset.label);
      const notes=[...document.querySelectorAll('input.note[data-iid="${iid}"]')].map(i=>i.value);
      return JSON.stringify({rendered:on.length>0||notes.length>0, on, note:notes[0]??""});})()`);
    const parsed = JSON.parse(ui);
    if (!parsed.rendered) continue; // 不在当前渲染页则跳过（分页限制, 非丢失）
    const ent = m.marks[iid];
    assert(JSON.stringify(parsed.on.slice().sort()) === JSON.stringify(ent.labels.slice().sort())
      && parsed.note === (ent.note || ""),
      `重启后 UI=磁盘: [${ent.labels.join(",")}] note="${ent.note || ""}"`);
    checked++;
    if (checked >= 3) break;
  }
  assert(checked >= 1, `实际核对了 ${checked} 张标记卡的 UI 一致性`);
  await shot("12_after_restart_race.png");
  console.log("PHASE3B DONE");
  ws.close();
}

const phase = process.argv[2] || "phase1";
({ phase1, phase2, corrupt: phaseCorrupt, phase3, phase3b }[phase])().catch((e) => {
  console.error("FAILED:", e.message);
  process.exit(1);
});
