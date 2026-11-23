'use strict';
const $ = id => document.getElementById(id);
let state, selected = new Set(), current = null, busy = false, pollTimer;
let appToken = APP_TOKEN, sessionRefresh;
let workspaceView = 'cleaning', alignmentKey = '', alignmentBusy = false;
let pendingAdvance = null;
let processingKey = '', processingBusy = false;
const reviewButtons = {keep:'cloudKeep', pending:'cloudPending', reject:'cloudReject'};
try { $('autoNextAlignment').checked = localStorage.getItem('pointcloud-auto-next') === '1'; } catch {}
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
const cloudDecision = row => row.cloud_decision || 'pending';
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
  if (busy || alignmentBusy || processingBusy) return;
  busy = true; controls();
  try { await fn(); } catch (e) { message(e.message, true); }
  finally { busy = false; controls(); advanceAfterSave(); }
}
function accept(data, paths = false, preserveCurrent = false) {
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
  render(preserveCurrent);
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
     filter === 'aligned' && r.has_alignment || filter === 'unaligned' && r.has_cloud && !r.has_alignment ||
     ['keep','pending','reject'].includes(filter) && r.has_cloud && cloudDecision(r) === filter ||
     filter === 'failed' && checkFailed(r) || filter === 'excluded' && r.excluded));
}
function renderList() {
  const rows = visibleRows();
  $('frames').replaceChildren();
  for (const row of rows) {
    const item = document.createElement('div'); item.className = 'frame' + (row.id === current ? ' active' : '');
    item.setAttribute('data-decision', cloudDecision(row));
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
      row.has_alignment ? (row.alignment_cached ? '已确认·待导出' : '坐标已对齐') : '',
      row.has_cleaning ? `已清理 ${row.removed_points.toLocaleString()} 点` : '',
      row.has_cloud ? '采集' + {keep:'保留',pending:'待定',reject:'淘汰'}[cloudDecision(row)] : '',
      row.cloud_review_stale ? '版本变化·筛选待确认' : '',
      row.has_alignment ? measurementLabel(row) : '',
      row.reviewed ? '分割已确认' : '分割待确认', row.decision === 'reject' ? '已淘汰' : '',
      checkFailed(row) ? '核对失败 · 自动跳过' : '', row.excluded ? '已标记跳过' : ''].filter(Boolean).join(' · ');
    if (blocked(row) || row.decision === 'reject' || cloudDecision(row) === 'reject') chips.classList.add('warn');
    item.title = row.relative_dir + (row.validation_errors?.length ? '\n'+row.validation_errors.join('\n') :
      row.missing.length ? '\n缺少：'+row.missing.join('、') : '');
    button.append(title, capture, chips); button.disabled = alignmentBusy;
    button.onclick = () => selectCapture(row.id);
    item.append(check, button); $('frames').append(item);
  }
  const visibleIds = new Set(rows.map(r => r.id)), hiddenSelection = [...selected].filter(id => !visibleIds.has(id)).length;
  $('selectedCount').textContent = `已勾选 ${selected.size}` + (hiddenSelection ? ` · 隐藏 ${hiddenSelection}` : '');
  $('selectedCount').title = hiddenSelection ? `当前筛选外还有 ${hiddenSelection} 次已勾选采集，生成／导出已勾选范围时会一并处理。` : '';
  $('selectAll').checked = rows.length > 0 && rows.every(r => selected.has(r.id));
  $('selectAll').indeterminate = rows.some(r => selected.has(r.id)) && !$('selectAll').checked;
  $('listFooter').textContent = `显示 ${rows.length} / ${state.frames.length} 次采集 · 已生成 ${state.clouds} · 已对齐 ${state.frames.filter(r => r.has_alignment).length}`;
}
function selectCapture(id) {
  if (alignmentBusy || processingBusy) return;
  current = id; renderList(); renderViewer(); controls();
}
function setWorkspaceView(view) {
  workspaceView = view;
  renderViewer(); controls();
}
function currentVersionLabel(row) {
  return (row.has_cleaning ? '处理后点集' : '原始点集') + ' · ' + (row.has_alignment ? '猪体坐标' : '相机坐标');
}
function measurementLabel(row) {
  if (!row.has_alignment) return '测量待对齐';
  if (row.measurement_ready) return '测量已复核';
  return row.measurement_slice_count ? `测量已确认 ${row.measurement_usable_slices || 0}/${row.measurement_slice_count} 片` : '测量待复核';
}
function currentFiles(row) {
  return {ply:row.current_ply || (row.has_alignment ? (row.has_cleaning ? 'cloud_cleaned_body_mm.ply' : 'cloud_body_mm.ply') :
    row.has_cleaning ? 'cloud_cleaned_mm.ply' : row.cloud_ply || '相机点云.ply'),
    npz:row.current_npz || (row.has_alignment ? (row.has_cleaning ? 'cleaned_body_points.npz' : 'body_points.npz') :
      row.has_cleaning ? 'cleaned_points.npz' : 'points.npz')};
}
function renderViewer() {
  const row = state.frames.find(r => r.id === current);
  const camera = row ? cameraLabel(row) : '';
  const mode = row?.cloud_input_mode || row?.input_mode;
  $('currentTitle').textContent = row ? [camera,mode === 'rgb' ? 'RGB 分割' : mode === 'infrared' ? 'IR 分割' : '',
    `${row.pig_id} / ${row.capture_id}`].filter(Boolean).join(' · ') : '选择一次采集查看点云';
  $('currentTitle').title = $('currentTitle').textContent;
  $('currentTag').hidden = !row?.has_cloud;
  if (row?.has_cloud) {
    $('currentTag').textContent = (row.has_cleaning ? '清理版本 · ' : '') + (row.cloud_reviewed ? '已确认分割' : '初稿预览');
    $('currentTag').classList.toggle('good', row.cloud_reviewed);
  }
  const hasCloud = !!row?.has_cloud;
  $('cloudReviewHint').hidden = !hasCloud;
  $('cloudReviewHint').classList.toggle('stale', !!row?.cloud_review_stale);
  $('cloudReviewHint').textContent = hasCloud ? `筛选对象：本次采集的${currentVersionLabel(row)}；${measurementLabel(row)}。` +
    (row.cloud_review_stale ? '处理版本变化或旧记录未绑定版本，筛选已回到待定，请重新确认。' : '保留表示采集继续采用；测量特征需在“测量复核”中逐片确认。') : '';
  const aligning = workspaceView === 'alignment' && hasCloud;
  const processing = ['cleaning','review'].includes(workspaceView) && hasCloud;
  $('empty').hidden = hasCloud;
  $('alignmentEditor').hidden = !aligning;
  $('processingEditor').hidden = !processing;
  $('processingEditor').title = workspaceView === 'review' ? '当前采集的测量复核' : '当前采集的点云查看与清理';
  $('toggleExcluded').hidden = aligning;
  $('autoNextOption').hidden = !aligning;
  for (const [view, id] of Object.entries({cleaning:'cleanCurrent', alignment:'alignCurrent', review:'reviewCurrent'})) {
    $(id).setAttribute('aria-selected', String(workspaceView === view));
  }
  $('alignmentStatus').textContent = !hasCloud ? '先生成或打开点云，再按顺序清理、对齐、复核' : row.has_alignment ? (row.alignment_cached ? '本条已确认 · 待统一导出' : '坐标已对齐 · 可进入测量复核') : '待坐标对齐 · 圈地面并标记尾根、颈根';
  $('alignmentStatus').title = row?.alignment_output || '';
  if (workspaceView === 'cleaning' && processing) $('alignmentStatus').textContent = row.has_cleaning ? `清理已暂存 · 删除 ${row.removed_points.toLocaleString()} 点 · 原始点云保留` : '先检测并确认飞点；确认后进入坐标对齐';
  if (workspaceView === 'review' && processing) $('alignmentStatus').textContent = row.has_alignment ? '已进入测量复核 · 使用猪体坐标检查切片边界' : '尚未完成坐标对齐 · 当前仅显示待处理提示';
  // Exporting changes the job, not the loaded point set. Keep editors alive
  // while the job locks editing, so they never reload against a busy workspace.
  const baseEditorKey = hasCloud ? JSON.stringify([state.dataset_id, state.results, row.id]) : '';
  const editorKey = hasCloud ? baseEditorKey + ':' + (row.cleaning_digest || '') : '';
  if (!editorKey || alignmentKey && editorKey !== alignmentKey) {
    $('alignmentEditor').removeAttribute('src'); alignmentKey = ''; alignmentBusy = false;
  }
  if (aligning && alignmentKey !== editorKey) {
    alignmentKey = editorKey; alignmentBusy = true;
    $('alignmentEditor').src = '/alignment?' + new URLSearchParams({id:row.id, dataset_id:state.dataset_id, embedded:'1'});
  }
  // A save updates this editor's existing context. Reload only for another
  // capture, workflow step or coordinate reference, so undo and slice position survive.
  const processKey = processing ? JSON.stringify([baseEditorKey, workspaceView, row.alignment_revision || '']) : '';
  if (!processKey || (processingKey && processKey !== processingKey)) {
    $('processingEditor').removeAttribute('src'); processingKey = ''; processingBusy = false;
  }
  if (processing && processingKey !== processKey) {
    processingKey = processKey; processingBusy = true;
    $('processingEditor').src = '/processing?' + new URLSearchParams({id:row.id, dataset_id:state.dataset_id, mode:workspaceView === 'review' ? 'review' : 'cleaning'});
  }
  const issue = row?.validation_errors?.join('；') || (row?.missing.length ? '缺少：'+row.missing.join('、') : '');
  $('captureIssue').hidden = !issue && !row?.excluded;
  $('captureIssue').textContent = [issue ? '核对失败，生成时自动跳过：'+issue : '', row?.excluded ? '本采集已手动标记为跳过。' : ''].filter(Boolean).join('\n');
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
  const locked = busy || running() || alignmentBusy || processingBusy;
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
  $('export').title = '选择导出内容、采集范围与保存目录，统一保存点云和筛选记录';
  $('exportCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud;
  $('cleanCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud;
  $('alignCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud;
  $('reviewCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud;
  $('autoNextAlignment').disabled = locked;
  for (const id of ['search','filter','cameraFilter','modeFilter']) $(id).disabled = alignmentBusy || processingBusy;
  document.querySelectorAll('.frame>button').forEach(el => { el.disabled = alignmentBusy || processingBusy; });
  $('alignmentEditor').inert = busy || running();
  $('processingEditor').inert = busy || running();
  $('reveal').disabled = busy || !state?.results;
  const active = state?.frames.find(r => r.id === current);
  for (const [decision, id] of Object.entries(reviewButtons)) {
    $(id).disabled = locked || !active?.has_cloud;
    $(id).setAttribute('aria-pressed', String(!!active?.has_cloud && cloudDecision(active) === decision));
  }
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
  for (const id of ['exportContent','exportParent','exportName','browseExportParent','exportKeep','exportPending','exportReject']) $(id).disabled = locked;
  if ($('exportDialog').open) exportPlan();
}
function destination() {
  const parent = $('parent').value.trim(), separator = parent.includes('\\') ? '\\' : '/';
  return parent.replace(/[\\/]$/, '') + separator + $('name').value;
}
function ensureVisibleCapture() {
  const rows = visibleRows();
  if (!rows.some(r => r.id === current)) {
    current = (rows.find(r => r.has_cloud) || rows[0])?.id || null;
  }
}
function render(preserveCurrent = false) { renderFilters(); if (!preserveCurrent) ensureVisibleCapture(); renderList(); renderViewer(); renderJob(); renderValidation(); controls(); }
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
$('directoryDialog').addEventListener('close', controls);
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
function advancePlan() {
  if (!$('autoNextAlignment').checked || workspaceView !== 'alignment') return null;
  const visible = new Set(visibleRows().map(r=>r.id)), index = state.frames.findIndex(r => r.id === current);
  return {dataset:state.dataset_id, id:current, candidates:state.frames.slice(index+1).filter(r=>visible.has(r.id)).map(r=>r.id)};
}
function advanceAfterSave() {
  if (!pendingAdvance || busy || alignmentBusy || running()) return;
  const plan = pendingAdvance; pendingAdvance = null;
  if (!$('autoNextAlignment').checked || workspaceView !== 'alignment' ||
      state.dataset_id !== plan.dataset || current !== plan.id) return;
  const rows = visibleRows();
  const next = plan.candidates.map(id=>rows.find(r=>r.id===id)).find(r=>r?.has_cloud && !r.has_alignment && cloudDecision(r)!=='reject');
  if (next) { selectCapture(next.id); message('上一条已保存，已自动切换到下一条待对齐采集。'); }
  else message('本条已保存，当前筛选范围后面已没有待对齐采集。可在左侧回看，或导出点云成果。');
}
for (const [decision, button] of Object.entries(reviewButtons)) $(button).onclick = () => {
  const id = current, plan = advancePlan();
  return action(async () => {
    accept(await api('review_cloud', {dataset_id:state.dataset_id, id, decision}), false, true);
    pendingAdvance = plan;
    message('当前采集处理版本已标记为' + {keep:'保留',pending:'待定',reject:'淘汰'}[decision] + '；测量复核进度单独记录。');
  });
};
$('autoNextAlignment').onchange = () => {
  if (!$('autoNextAlignment').checked) pendingAdvance = null;
  try { localStorage.setItem('pointcloud-auto-next', $('autoNextAlignment').checked ? '1' : '0'); } catch {}
};
for (const [view, id] of Object.entries({cleaning:'cleanCurrent', alignment:'alignCurrent', review:'reviewCurrent'})) $(id).onclick = () => {
  if (current && !busy && !running() && !alignmentBusy && !processingBusy) setWorkspaceView(view);
};
function editorLoadFailed() {
  alignmentBusy = false; alignmentKey = ''; $('alignmentEditor').removeAttribute('src');
  workspaceView = 'cleaning'; renderViewer(); controls();
  message('坐标对齐界面未能载入，请检查服务连接后重新点击“坐标对齐”。', true);
}
$('alignmentEditor').onerror = editorLoadFailed;
$('alignmentEditor').onload = () => {
  if (!alignmentKey) return;
  try {
    if ($('alignmentEditor').contentWindow.location.href === 'about:blank') return;
    if (!$('alignmentEditor').contentDocument?.getElementById('scene')) editorLoadFailed();
  }
  catch { editorLoadFailed(); }
};
window.addEventListener('message', async event => {
  const data = event.data;
  if (event.origin !== location.origin || event.source !== $('alignmentEditor').contentWindow ||
      data?.type !== 'pointcloud-alignment' || data.dataset_id !== state?.dataset_id || data.id !== current) return;
  if (data.action === 'busy') { alignmentBusy = data.busy === true; controls(); advanceAfterSave(); }
  if (data.action === 'view-saved') setWorkspaceView('review');
  if (data.action === 'saved') {
    const plan = advancePlan();
    try {
      const fresh = await api('state');
      if (data.dataset_id !== state.dataset_id || fresh.dataset_id !== state.dataset_id || data.id !== current) return;
      accept(fresh, false, true);
      pendingAdvance = plan;
      message('坐标对齐已保存，可进入“测量复核”检查切片；采集筛选请单独确认。');
      advanceAfterSave();
    } catch(e) { message('成果已保存，工作台状态更新失败：'+e.message+'；请刷新页面。', true); }
  }
});
window.addEventListener('message', async event => {
  const data = event.data;
  if (event.origin !== location.origin || event.source !== $('processingEditor').contentWindow ||
      data?.type !== 'pointcloud-processing' || !processingKey || data.dataset_id !== state?.dataset_id || data.id !== current) return;
  if (data.action === 'busy') { processingBusy = data.busy === true; controls(); }
  if (data.action === 'saved') {
    try {
      const fresh = await api('state');
      if (fresh.dataset_id === state?.dataset_id && data.id === current) accept(fresh, false, true);
    } catch (e) { message('处理记录已保存，列表更新失败：'+e.message, true); }
  }
});
$('processingEditor').onerror = () => {
  processingBusy = false; processingKey = ''; $('processingEditor').removeAttribute('src');
  controls(); message('处理页面未能载入，请检查服务连接后重新点击当前步骤。', true);
};
$('processingEditor').onload = () => {
  if (!processingKey) return;
  try {
    if ($('processingEditor').contentWindow.location.href === 'about:blank') return;
    if (!$('processingEditor').contentDocument?.getElementById('cloud')) $('processingEditor').onerror();
  } catch { $('processingEditor').onerror(); }
};
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
function exportScopeRows() {
  const scope = document.querySelector('input[name=scope]:checked').value;
  return state.frames.filter(r => r.has_cloud && (scope === 'all' || scope === 'current' && r.id === current || scope === 'selected' && selected.has(r.id)));
}
function exportRows() {
  const decisions = exportDecisions();
  return exportScopeRows().filter(r => decisions.includes(cloudDecision(r)) &&
    ($('exportContent').value !== 'aligned' || r.has_alignment));
}
const exportReviewOptions = {keep:'exportKeep', pending:'exportPending', reject:'exportReject'};
const reviewLabels = {keep:'保留', pending:'待定', reject:'淘汰'};
function exportDecisions() { return Object.entries(exportReviewOptions).filter(([,id])=>$(id).checked).map(([decision])=>decision); }
function exportDestination() {
  const parent = $('exportParent').value.trim(), separator = parent.includes('\\') ? '\\' : '/';
  return parent.replace(/[\\/]$/, '') + separator + $('exportName').value;
}
function exportPlan() {
  const rows = exportRows(), scope = exportScopeRows(), aligned = $('exportContent').value === 'aligned';
  $('exportHint').textContent = '每条采集均取当前完整点集：应用已有清理；已对齐则取猪体坐标，未对齐则取相机坐标。切换清理／对齐／复核页签不改变导出版本，原始文件和处理记录一并附带。' +
    (aligned ? '本次仅导出已对齐采集，未对齐项只记入筛选清单。' : '本次包含已对齐和未对齐采集。');
  $('exportPath').textContent = exportDestination();
  $('exportCount').textContent = `${rows.length} 次采集 · 当前完整处理版本`;
  $('exportEligibility').textContent = `范围内 ${scope.length} 次：保留 ${scope.filter(r=>cloudDecision(r)==='keep').length}、待定 ${scope.filter(r=>cloudDecision(r)==='pending').length}、淘汰 ${scope.filter(r=>cloudDecision(r)==='reject').length}；已对齐 ${scope.filter(r=>r.has_alignment).length}。` +
    ` 本次导出中测量已复核 ${rows.filter(r=>r.measurement_ready).length} 次，其余保留待复核标记。` +
    (!rows.length ? (!exportDecisions().length ? ' 请至少勾选一种导出状态。' : aligned ? ' 所选状态中没有已对齐点云，请调整范围或先完成对齐。' : ' 当前范围没有所选状态的点云。') : '');
  $('exportTree').textContent = exportDecisions().map(decision=>{
    const group=rows.filter(r=>cloudDecision(r)===decision);
    return `${reviewLabels[decision]}/（${group.length} 次）\n` + group.slice(0,2).map(r=>{
      const files = currentFiles(r);
      return '  '+r.relative_dir+'/\n    主点云：'+files.ply+' · '+files.npz+'\n    '+currentVersionLabel(r)+' · '+measurementLabel(r)+
        '\n    current_pointcloud.json · 原始生成文件 · 筛选记录'+
        (r.has_alignment ? '\n    alignment.json · 对齐检查图' : '')+
        (r.has_cleaning ? '\n    清理索引与历史 · measurement_summary.json'+(r.has_alignment ? ' · slice_measurements.csv' : '') : '');
    }).join('\n')+
      (group.length>2 ? `\n  …另 ${group.length-2} 次` : '');
  }).join('\n')+'\ncloud_screening.json · cloud_screening.csv';
  $('confirmExport').disabled = !rows.length || busy || running() || alignmentBusy || processingBusy;
}
function openExport(scope) {
  document.querySelector(`input[name=scope][value=${scope}]`).checked = true;
  $('exportContent').value = 'all';
  for (const id of Object.values(exportReviewOptions)) $(id).checked = true;
  $('exportParent').value = $('parent').value;
  $('exportName').value = '处理点云_' + new Date().toISOString().replace(/[-:TZ.]/g,'').slice(0,17);
  $('exportMessage').textContent = '';
  exportPlan(); $('exportDialog').showModal();
}
$('exportCurrent').onclick = () => openExport('current');
$('export').onclick = () => openExport([...selected].some(id => state.frames.some(r => r.id === id && r.has_cloud)) ? 'selected' : 'all');
$('exportContent').onchange = exportPlan;
document.querySelectorAll('input[name=scope]').forEach(input => input.onchange = exportPlan);
for (const id of Object.values(exportReviewOptions)) $(id).onchange = exportPlan;
for (const id of ['exportParent','exportName']) $(id).addEventListener('input', exportPlan);
$('browseExportParent').onclick = () => action(() => choose('exportParent','选择点云导出目录'));
$('confirmExport').onclick = () => action(async () => {
  $('confirmExport').disabled = true; $('exportMessage').textContent = '';
  try {
    accept(await api('export', {dataset_id:state.dataset_id, ids:exportRows().map(r=>r.id), scope_ids:exportScopeRows().map(r=>r.id),
      parent:$('exportParent').value.trim(), name:$('exportName').value,
      aligned_only:$('exportContent').value === 'aligned', decisions:exportDecisions(), group_by_decision:true}));
    $('parent').value = $('exportParent').value; $('name').value = state.suggested_name;
    $('exportDialog').close(); message('开始导出当前处理版本及原始文件，主点云和测量复核状态将写入清单。');
  } catch(e) { $('exportMessage').textContent = e.message; throw e; }
  finally { exportPlan(); }
});
action(async () => { accept(await api('state')); });
window.addEventListener('resize', () => requestAnimationFrame(() => document.querySelector('.frame.active')?.scrollIntoView({block:'nearest'})));
