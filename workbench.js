'use strict';
const $ = id => document.getElementById(id);
let state, selected = new Set(), current = null, busy = false, viewerKey = '', pollTimer;
let appToken = APP_TOKEN, sessionRefresh, returnExportScope;
const running = () => ['running', 'stopping'].includes(state?.job?.status);
const dirty = () => state && ($('source').value.trim() !== state.source || $('segmentation').value.trim() !== state.segmentation);
const checkFailed = row => !!row.validation_errors?.length || !!row.missing.length;
const blocked = row => checkFailed(row) || !!row.excluded;
const cameraType = row => {
  const types = [row.has_cloud && row.cloud_camera?.type, row.camera?.type];
  return types.find(type => ['itof', 'stereo'].includes(type)) || 'unknown';
};
const cameraLabel = row => ({itof:'iToF', stereo:'双目', unknown:'相机待识别'}[cameraType(row)]);
const segmentationMode = row => row.has_cloud ? row.cloud_input_mode || row.input_mode : row.input_mode;
const message = (text = '', error = false) => {
  for (const id of ['message', 'directoryMessage']) { $(id).textContent = text; $(id).title = text; $(id).classList.toggle('error', error); }
};
async function api(path, payload, retry = true) {
  const response = await fetch('/api/' + path, payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-App-Token': appToken}, body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (response.status === 403 && data.code === 'session_expired' && retry) {
    // A stale page survives a local-server restart. Refresh only an explicitly rejected request.
    if (!sessionRefresh) sessionRefresh = api('session', undefined, false).then(data => { appToken = data.token; }).finally(() => { sessionRefresh = null; });
    await sessionRefresh;
    return api(path, payload, false);
  }
  if (!response.ok) throw new Error(data.error || '操作失败');
  return data;
}
async function action(fn) {
  if (busy) return;
  busy = true; controls();
  try { await fn(); } catch (e) { message(e.message, true); }
  finally { busy = false; controls(); }
}
function accept(data, paths = false) {
  const first = !state;
  const changedInputs = state && (data.source !== state.source || data.segmentation !== state.segmentation);
  const changedResults = state && data.results !== state.results;
  const previousCurrent = current;
  state = data;
  clearTimeout(pollTimer);
  if (changedInputs) { selected.clear(); current = null; }
  if (first || paths) {
    $('source').value = state.source; $('segmentation').value = state.segmentation;
  }
  if (first) {
    $('parent').value = state.export_parent; $('name').value = state.suggested_name;
    selected = new Set(state.frames.filter(r => r.has_cloud).map(r => r.id));
  }
  selected = new Set([...selected].filter(id => state.frames.some(r => r.id === id)));
  if (!state.frames.some(r => r.id === current) || changedResults && !state.frames.find(r => r.id === current)?.has_cloud)
    current = (state.frames.find(r => r.has_cloud) || state.frames[0])?.id;
  render();
  if (first || paths || previousCurrent !== current) document.querySelector('.frame.active')?.scrollIntoView({block:'nearest'});
  if (state.warning) message(state.warning, true);
  if (running()) schedulePoll();
}
function renderFilters() {
  const rows = state.frames;
  const fill = (id, choices) => {
    const element = $(id), previous = element.value || 'all';
    element.replaceChildren();
    for (const [value, label, count] of choices) {
      const option = document.createElement('option');
      option.value = value; option.textContent = `${label}（${count}）`;
      element.append(option);
    }
    element.value = choices.some(([value]) => value === previous) ? previous : 'all';
  };
  const cameras = [['all', '全部相机', rows.length],
    ['itof', 'iToF', rows.filter(r => cameraType(r) === 'itof').length],
    ['stereo', '双目', rows.filter(r => cameraType(r) === 'stereo').length]];
  const unknown = rows.filter(r => cameraType(r) === 'unknown').length;
  if (unknown) cameras.push(['unknown', '未识别相机', unknown]);
  fill('cameraFilter', cameras);
  const modes = [['all', '全部分割', rows.length],
    ['rgb', 'RGB 分割', rows.filter(r => segmentationMode(r) === 'rgb').length],
    ['infrared', 'IR 分割', rows.filter(r => segmentationMode(r) === 'infrared').length]];
  const unknownModes = rows.filter(r => !['rgb', 'infrared'].includes(segmentationMode(r))).length;
  if (unknownModes) modes.push(['unknown', '未识别来源', unknownModes]);
  fill('modeFilter', modes);
}
function visibleRows() {
  const query = $('search').value.toLowerCase().trim(), filter = $('filter').value;
  const camera = $('cameraFilter').value || 'all', mode = $('modeFilter').value || 'all';
  return state.frames.filter(r => (camera === 'all' || cameraType(r) === camera) &&
    (mode === 'all' || (segmentationMode(r) || 'unknown') === mode) &&
    (!query || `${r.pig_id} ${r.capture_id} ${r.relative_dir}`.toLowerCase().includes(query)) &&
    (filter === 'all' || filter === 'cloud' && r.has_cloud || filter === 'reviewed' && r.reviewed ||
     filter === 'draft' && !r.reviewed && !blocked(r) || filter === 'missing' && r.missing.length ||
     filter === 'failed' && checkFailed(r) || filter === 'excluded' && r.excluded));
}
function renderList() {
  const rows = visibleRows();
  $('frames').replaceChildren();
  for (const row of rows) {
    const item = document.createElement('div'); item.className = 'frame' + (row.id === current ? ' active' : '');
    const check = document.createElement('input'); check.type = 'checkbox'; check.checked = selected.has(row.id);
    check.setAttribute('aria-label', '勾选 ' + row.relative_dir); check.disabled = running() || busy;
    check.onchange = () => { check.checked ? selected.add(row.id) : selected.delete(row.id); renderList(); controls(); };
    const button = document.createElement('button'); button.type = 'button';
    const title = document.createElement('strong'); title.textContent = '耳标 ' + row.pig_id;
    const capture = document.createElement('span'); capture.className = 'capture'; capture.textContent = row.capture_id;
    const chips = document.createElement('span'); chips.className = 'chips';
    const mode = segmentationMode(row);
    chips.textContent = [cameraLabel(row), mode === 'rgb' ? 'RGB 分割' : mode === 'infrared' ? 'IR 分割' : '',
      row.has_cloud ? `${row.points.toLocaleString()} 点` : '未生成',
      row.reviewed ? '分割已确认' : '分割待确认', row.decision === 'reject' ? '已淘汰' : '',
      checkFailed(row) ? '核对失败 · 自动跳过' : '', row.excluded ? '已标记跳过' : ''].filter(Boolean).join(' · ');
    if (blocked(row) || row.decision === 'reject') chips.classList.add('warn');
    item.title = row.relative_dir + (row.validation_errors?.length ? '\n'+row.validation_errors.join('\n') :
      row.missing.length ? '\n缺少：'+row.missing.join('、') : '');
    button.append(title, capture, chips); button.onclick = () => { current = row.id; renderList(); renderViewer(); controls(); };
    item.append(check, button); $('frames').append(item);
  }
  const visibleIds = new Set(rows.map(r => r.id)), hiddenSelection = [...selected].filter(id => !visibleIds.has(id)).length;
  $('selectedCount').textContent = `已勾选 ${selected.size}` + (hiddenSelection ? ` · 隐藏 ${hiddenSelection}` : '');
  $('selectedCount').title = hiddenSelection ? `当前筛选外还有 ${hiddenSelection} 次已勾选采集，生成／导出已勾选范围时会一并处理。` : '';
  $('selectAll').checked = rows.length > 0 && rows.every(r => selected.has(r.id));
  $('selectAll').indeterminate = rows.some(r => selected.has(r.id)) && !$('selectAll').checked;
  $('listFooter').textContent = `显示 ${rows.length} / ${state.frames.length} 次采集 · 已生成 ${state.clouds}`;
}
function media(row, file) {
  return '/api/media?' + new URLSearchParams({dataset_id: state.dataset_id, id: row.id, file});
}
function renderViewer() {
  const row = state.frames.find(r => r.id === current);
  const camera = row ? cameraLabel(row) : '';
  const mode = row?.cloud_input_mode || row?.input_mode;
  $('currentTitle').textContent = row ? [camera,mode === 'rgb' ? 'RGB 分割' : mode === 'infrared' ? 'IR 分割' : '',
    `${row.pig_id} / ${row.capture_id}`].filter(Boolean).join(' · ') : '选择一次采集查看点云';
  $('currentTag').hidden = !row?.has_cloud;
  if (row?.has_cloud) {
    $('currentTag').textContent = row.cloud_reviewed ? '已确认分割' : '初稿预览';
    $('currentTag').classList.toggle('good', row.cloud_reviewed);
  }
  const key = row?.has_cloud ? media(row, 'preview.html') + '&job=' + (state.job?.id || '') : '';
  $('viewer').hidden = !key; $('empty').hidden = !!key;
  const issue = row?.validation_errors?.join('；') || (row?.missing.length ? '缺少：'+row.missing.join('、') : '');
  $('captureIssue').hidden = !issue && !row?.excluded;
  $('captureIssue').textContent = [issue ? '核对失败，生成时自动跳过：'+issue : '', row?.excluded ? '本采集已手动标记为跳过。' : ''].filter(Boolean).join('\n');
  if (key !== viewerKey) { key ? $('viewer').src = key : $('viewer').removeAttribute('src'); viewerKey = key; }
}
function validationSummary() {
  const rows = state?.frames || [], checked = rows.filter(r => r.input_loaded !== false);
  const failed = checked.filter(checkFailed).length, excluded = checked.filter(r => r.excluded).length;
  return `目录核对：通过 ${checked.length-failed} 次，失败 ${failed} 次；手动标记跳过 ${excluded} 次。`;
}
function renderValidation() {
  $('validationSummary').textContent = validationSummary();
  const issues = state.frames.filter(r => checkFailed(r) || r.excluded);
  $('directoryValidation').hidden = !issues.length || dirty();
  $('validationIssues').replaceChildren();
  if (!issues.length) $('validationIssues').textContent = '没有核对失败或已标记跳过的采集。';
  for (const row of issues) {
    const item = document.createElement('section'); item.className = 'validationitem';
    const info = document.createElement('div'), title = document.createElement('strong'), reason = document.createElement('p');
    title.textContent = row.relative_dir;
    reason.textContent = [row.validation_errors?.join('；') || (row.missing.length ? '缺少：'+row.missing.join('、') : ''),
      row.excluded ? '已手动标记跳过' : '核对失败，生成时自动跳过'].filter(Boolean).join(' · ');
    const button = document.createElement('button'); button.type = 'button'; button.className = 'markskip';
    button.textContent = row.excluded ? '取消标记' : '标记跳过'; button.disabled = busy || running() || row.input_loaded === false;
    button.onclick = () => action(() => markSkipped([row.id], !row.excluded));
    info.append(title, reason); item.append(info, button); $('validationIssues').append(item);
  }
  $('downloadValidation').href = '/api/validation?'+new URLSearchParams({dataset_id:state.dataset_id});
}
function renderJob() {
  const job = state.job; $('jobPanel').hidden = !job;
  $('showJob').hidden = !job;
  if (!job) return;
  $('jobPanel').classList.toggle('finished', !running());
  const status = {running:'处理中', stopping:'准备停止', completed:'完成', completed_with_errors:'部分完成', stopped:'已停止', failed:'失败'}[job.status];
  $('jobTitle').textContent = (job.mode === 'generate' ? '生成点云' : '按文件夹导出') + ' · ' + status;
  $('jobCounts').textContent = `${job.done} / ${job.total}，成功 ${job.success}`;
  const problems = job.errors.length + job.skipped.length;
  $('showJob').textContent = `${status} ${job.done}/${job.total}${problems ? ' · '+problems+' 项待查看' : ''} · 详情`;
  $('showJob').title = '查看任务进度、成功数量、跳过与失败原因';
  $('progress').max = job.total || 1; $('progress').value = job.done;
  $('jobCurrent').textContent = [job.current, !running() && job.success ? job.output : '', job.error, job.warning].filter(Boolean).join(' · ');
  $('stop').hidden = !running(); $('stop').disabled = busy || job.status === 'stopping';
  const errors = [...job.skipped.map(r => `${r.relative_dir}：跳过，${r.reason}`), ...job.errors.map(r => `${r.relative_dir}：${r.error}`)];
  $('jobDetails').hidden = !errors.length; $('jobErrors').replaceChildren();
  for (const text of errors) { const p = document.createElement('p'); p.textContent = text; $('jobErrors').append(p); }
}
function controls() {
  const locked = busy || running();
  document.querySelectorAll('.browse').forEach(el => { el.disabled = locked; });
  for (const id of ['source','segmentation','parent','name','configure','openResults','load','finishDirectories','draft','selectAll']) $(id).disabled = locked;
  document.querySelectorAll('.pathshortcut').forEach(el => { el.disabled = locked; });
  const plan = generationPlan();
  $('generate').disabled = locked || !!plan.error;
  $('generate').textContent = selected.size ? '生成并导出' : ($('draft').checked ? '生成可用采集' : '生成已确认采集');
  $('generate').title = plan.error || plan.hint;
  $('generationHint').textContent = running() ? '任务处理中，请等待完成或停止后重新选择目录。' : plan.error || plan.hint;
  $('generationHint').title = $('generationHint').textContent;
  $('export').disabled = locked || !state?.clouds;
  $('exportCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud;
  $('reveal').disabled = busy || !state?.results;
  const active = state?.frames.find(r => r.id === current);
  $('toggleExcluded').disabled = locked || !active || active.input_loaded === false;
  $('toggleExcluded').textContent = active?.excluded ? '取消跳过标记' : '标记跳过';
  $('excludeFailed').disabled = locked || !state?.frames.some(r => checkFailed(r) && !r.excluded && r.input_loaded !== false);
  $('showValidation').disabled = !state?.frames.length;
  $('showValidation').textContent = state ? `核对失败 ${state.frames.filter(checkFailed).length} · 查看／标记` : '查看核对结果／标记';
  document.querySelectorAll('.markskip').forEach(el => { el.disabled = locked; });
  document.querySelectorAll('.frame>input').forEach(el => { el.disabled = locked; });
  if (state) {
    $('directoryValidation').hidden = !state.frames.some(r => blocked(r)) || dirty();
    $('datasetSummary').textContent = dirty() ? '输入目录已更改\n请重新核对载入' : `${state.pigs} 头猪 · ${state.frames.length} 次采集\n分割已确认 ${state.reviewed} 次`;
    $('destination').textContent = '导出位置：' + destination();
    $('destination').title = destination();
    const context = '当前成果：' + (state.results || '尚未生成点云');
    $('contextPath').textContent = context; $('contextPath').title = context;
    for (const field of ['source','segmentation','parent']) {
      const path = $(field).value.trim();
      $(field + 'Summary').textContent = path ? path.split(/[\\/]/).filter(Boolean).slice(-2).join('/') : '未选择';
      $(field + 'Summary').parentElement.title = path || '打开目录设置';
      $(field).title = path;
    }
  }
}
function destination() {
  const parent = $('parent').value.trim(), separator = parent.includes('\\') ? '\\' : '/';
  return parent.replace(/[\\/]$/, '') + separator + $('name').value;
}
function ensureVisibleCapture() {
  const rows = visibleRows();
  if (!rows.some(r => r.id === current)) current = (rows.find(r => r.has_cloud) || rows[0])?.id || null;
}
function render() { renderFilters(); ensureVisibleCapture(); renderList(); renderViewer(); renderJob(); renderValidation(); controls(); }
function generationPlan() {
  if (!state?.source || !state?.segmentation) return {error:'请打开“目录设置”，选择原始采集目录与分割目录并载入。'};
  if (state.results_match_inputs === false) return {error:'当前成果与载入的分割不是同一批输入；请在目录设置中重新核对载入后生成。'};
  if (dirty()) return {error:'输入目录已更改，请打开“目录设置”并点击“完成并载入”。'};
  const rows = selected.size ? state.frames.filter(r => selected.has(r.id)) : visibleRows();
  const usable = rows.filter(r => !blocked(r) && r.decision !== 'reject' &&
    ($('draft').checked || r.reviewed && r.status === 'reviewed'));
  if (!usable.length) return {error: selected.size ?
    '勾选范围内没有可生成采集：请检查核对结果、跳过标记、分割确认状态，或显式勾选“允许初稿预览”。' :
    '没有可生成的已确认采集：请先确认分割，或显式勾选“允许初稿预览”；核对失败、已标记跳过和已淘汰采集会跳过。'};
  const skippedHint = ` 核对失败 ${rows.filter(checkFailed).length} 次，手动标记 ${rows.filter(r=>r.excluded).length} 次，均自动跳过。`;
  return {ids:(selected.size ? rows : usable).map(r => r.id), hint:(selected.size ?
    `已勾选 ${rows.length} 次采集，其中 ${usable.length} 次可生成；可调整勾选后重试。` :
    `未勾选采集，将生成当前筛选范围内全部${$('draft').checked ? '可用' : '已确认'}采集，共 ${usable.length} 次；也可在左侧勾选指定范围。`)+skippedHint};
}
function schedulePoll() { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 850); }
async function poll() {
  const datasetId = state.dataset_id, jobId = state.job?.id;
  try {
    const {job} = await api('job');
    if (state.dataset_id !== datasetId || state.job?.id !== jobId) return;
    state.job = job; renderJob(); controls();
    if (running()) schedulePoll();
    else {
      const fresh = await api('state');
      if (state.dataset_id !== datasetId || state.job?.id !== jobId) return;
      accept(fresh);
      if (job?.success) { $('name').value = state.suggested_name; controls(); }
      message(job?.error || (job?.success ? `已保存 ${job.success} 次采集：${job.output}` : '任务已停止'), job?.status === 'failed');
    }
  } catch(e) {
    if (state.dataset_id !== datasetId || state.job?.id !== jobId) return;
    message('进度连接中断：' + e.message, true); schedulePoll();
  }
}
async function choose(field, title) {
  message('系统文件夹窗口已打开，请选择目录或取消。');
  const result = await api('choose_directory', {path: $(field).value, title});
  if (!result.cancelled) { $(field).value = result.path; message(); }
  else message('已取消选择，保留原路径。');
  controls();
}
document.querySelectorAll('.browse').forEach(button => button.onclick = () => action(() => choose(button.dataset.field,
  {source:'选择原始采集根目录', segmentation:'选择分割结果根目录', parent:'选择点云导出目录'}[button.dataset.field])));
function openDirectories(field) {
  $('directoryDialog').showModal();
  if (field) $(field).focus();
}
$('configure').onclick = () => openDirectories();
document.querySelectorAll('.pathshortcut').forEach(button => button.onclick = () => openDirectories(button.dataset.field));
$('exportSettings').onclick = () => {
  returnExportScope = document.querySelector('input[name=scope]:checked').value;
  $('exportDialog').close(); openDirectories('parent');
};
$('directoryDialog').addEventListener('close', () => {
  controls();
  if (returnExportScope) { const scope = returnExportScope; returnExportScope = null; openExport(scope); }
});
$('openResults').onclick = () => action(async () => {
  message('系统文件夹窗口已打开，请选择已有点云成果根目录。');
  const result = await api('choose_directory', {path: state.results || $('parent').value, title:'打开已有点云成果目录'});
  if (result.cancelled) { message('已取消选择。'); return; }
  accept(await api('open_results', {path: result.path})); message('已载入点云目录：' + result.path);
});
async function loadDirectories(close = false) {
  accept(await api('load', {source:$('source').value.trim(), segmentation:$('segmentation').value.trim()}), true);
  message(validationSummary()+' 失败项自动跳过，不影响其余采集。');
  if (close) $('directoryDialog').close();
}
$('load').onclick = () => action(() => loadDirectories());
$('finishDirectories').onclick = () => action(() => loadDirectories(true));
async function markSkipped(ids, excluded) {
  accept(await api('mark_skipped', {dataset_id:state.dataset_id, ids, excluded}));
  message(excluded ? `已标记 ${ids.length} 次采集为跳过，其余采集可继续生成。` : '已取消手动标记；核对失败项仍自动跳过，修复后请重新核对目录。');
}
$('showValidation').onclick = () => { renderValidation(); $('validationDialog').showModal(); };
$('directoryIssues').onclick = $('showValidation').onclick;
$('closeValidation').onclick = () => $('validationDialog').close();
$('excludeFailed').onclick = () => action(() => markSkipped(state.frames.filter(r => checkFailed(r) && !r.excluded && r.input_loaded !== false).map(r=>r.id), true));
$('toggleExcluded').onclick = () => action(async () => {
  const row = state.frames.find(r => r.id === current);
  if (row) await markSkipped([row.id], !row.excluded);
});
for (const id of ['source','segmentation','parent','name']) $(id).addEventListener('input', controls);
function applyFilters() {
  if (!state) return;
  ensureVisibleCapture();
  renderList(); renderViewer(); controls();
}
for (const id of ['search','filter']) $(id).addEventListener('input', applyFilters);
for (const id of ['cameraFilter','modeFilter']) $(id).onchange = applyFilters;
$('showJob').onclick = () => $('jobDialog').showModal();
$('closeJob').onclick = () => $('jobDialog').close();
$('selectAll').onchange = () => { const checked = $('selectAll').checked; visibleRows().forEach(r => checked ? selected.add(r.id) : selected.delete(r.id)); renderList(); controls(); };
$('draft').onchange = controls;
function payload(ids) { return {dataset_id:state.dataset_id, ids, parent:$('parent').value.trim(), name:$('name').value, allow_draft:$('draft').checked}; }
$('generate').onclick = () => action(async () => {
  if (dirty()) throw new Error('输入目录已更改，请先核对并载入');
  const plan = generationPlan();
  if (plan.error) throw new Error(plan.error);
  message('正在提交点云生成任务…');
  accept(await api('generate', payload(plan.ids))); message('开始生成，成果将按原采集目录保存。');
});
$('stop').onclick = () => action(async () => { accept(await api('stop', {})); });
$('reveal').onclick = () => action(async () => { await api('reveal', {}); });
function exportRows() {
  const scope = document.querySelector('input[name=scope]:checked').value;
  return state.frames.filter(r => r.has_cloud && (scope === 'all' || scope === 'current' && r.id === current || scope === 'selected' && selected.has(r.id)));
}
function exportPlan() {
  const rows = exportRows(); $('exportPath').textContent = destination();
  $('exportCount').textContent = `${rows.length} 次采集 · 每次采集完整文件夹`;
  $('exportTree').textContent = rows.slice(0, 4).map(r => r.relative_dir + '/\n  ' +
    (r.cloud_ply || (r.cloud_input_mode === 'rgb' ? 'cloud_rgb_mm.ply' : 'cloud_ir_mm.ply')) + ' · ' +
    'points.npz\n  pointcloud.json · 两张检查图 · preview.html').join('\n') +
    (rows.length > 4 ? `\n…另 ${rows.length - 4} 次采集` : '') + '\n根目录另含：index.html · pointcloud_dataset.json · export.json';
  $('confirmExport').disabled = !rows.length || busy || running();
}
function openExport(scope) { document.querySelector(`input[name=scope][value=${scope}]`).checked = true; exportPlan(); $('exportDialog').showModal(); }
$('exportCurrent').onclick = () => openExport('current');
$('export').onclick = () => openExport([...selected].some(id => state.frames.some(r => r.id === id && r.has_cloud)) ? 'selected' : 'all');
document.querySelectorAll('input[name=scope]').forEach(input => input.onchange = exportPlan);
$('confirmExport').onclick = () => action(async () => {
  $('confirmExport').disabled = true;
  message('正在提交文件夹导出任务…');
  try { accept(await api('export', payload(exportRows().map(r => r.id)))); $('exportDialog').close(); message('开始导出完整采集文件夹。'); }
  finally { exportPlan(); }
});
action(async () => { accept(await api('state')); });
window.addEventListener('resize', () => requestAnimationFrame(() => document.querySelector('.frame.active')?.scrollIntoView({block:'nearest'})));
