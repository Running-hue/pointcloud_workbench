'use strict';
const $ = id => document.getElementById(id);
let state, selected = new Set(), current = null, busy = false, pollTimer;
let appToken = APP_TOKEN, sessionRefresh;
let workspaceView = 'cleaning', alignmentKey = '', alignmentBusy = false;
let pendingAdvance = null;
let processingKey = '', processingBusy = false;
let processingInstance = 0;
let filtersCollapsed = false;
const reviewButtons = {keep:'cloudKeep', pending:'cloudPending', reject:'cloudReject'};
try { $('autoNextAlignment').checked = localStorage.getItem('pointcloud-auto-next') === '1'; } catch {}
try { filtersCollapsed = localStorage.getItem('pointcloud-filters-collapsed') === '1'; } catch {}
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
const stageLabels = {cleaning:'点云清理', alignment:'坐标对齐', review:'测量复核'};
const stageShortLabels = {cleaning:'清理', alignment:'对齐', review:'复核'};
const stageOptions = {cleaning:'exportCleaning', alignment:'exportAlignment', review:'exportMeasurement'};
const filterLabels = {all:'全部采集', cleaning:'清理已确认', alignment:'清理与对齐已确认', completed:'三阶段已完成', excluded:'已跳过'};
const stageState = (row, stage = workspaceView) => row.workflow_stages?.[stage] || {
  decision:cloudDecision(row), stale:!!row.cloud_review_stale, available:!!row.has_cloud,
  has_output:stage === 'cleaning' || !!row.has_alignment, reviewed:false, blocked_by:''};
const stageDecision = (row, stage = workspaceView) => stageState(row, stage).decision;
const stageComplete = (row, stage) => {
  const value = stageState(row, stage);
  return !!row.has_cloud && !!value.has_output && value.reviewed === true && value.decision === 'keep' &&
    !value.stale && !value.blocked_by;
};
const captureComplete = row => Object.keys(stageLabels).every(stage => stageComplete(row, stage));
function currentCameraScopes() {
  const selected = $('cameraFilter').value || 'all';
  if (selected !== 'all') return [selected];
  const present = ['itof', 'stereo'].filter(camera =>
    state.frames.some(row => cameraType(row) === camera));
  return present.length ? present : ['unknown'];
}
function framesForCamera(camera) {
  return state.frames.filter(row => cameraType(row) === camera);
}
function cameraProgress() {
  return currentCameraScopes().map(camera => {
    const rows = framesForCamera(camera);
    const groups = new Map();
    for (const row of rows) {
      const key = String(row.pig_id ?? '');
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    return {camera, totalPigs: groups.size,
      completePigs: [...groups.values()].filter(group => group.every(captureComplete)).length,
      completeCaptures: rows.filter(captureComplete).length, totalCaptures: rows.length};
  });
}
function cameraScopeLabel(camera) {
  return {itof:'iToF', stereo:'双目', unknown:'未识别相机'}[camera] || '猪头';
}
function renderCameraProgress(scopes) {
  const target = $('cameraProgressText');
  target.replaceChildren();
  for (const scope of scopes) {
    const line = document.createElement('span'), label = cameraScopeLabel(scope.camera);
    line.textContent = `${label}：${scope.completePigs} / ${scope.totalPigs} 头，` +
      `${scope.completeCaptures}/${scope.totalCaptures} 条`;
    target.append(line);
  }
  target.title = '同一耳标下全部对应采集的三阶段都完成，这头猪才算完成；采集完成数按当前相机范围统计。';
}
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
  const previousScrollTop = $('frames').scrollTop || 0;
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
  // Refreshing state or saving a decision must not move the capture list.
  // Explicit capture navigation is the only operation that centers a card.
  $('frames').scrollTop = previousScrollTop;
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
  const stages = {all:[], cleaning:['cleaning'], alignment:['cleaning','alignment'], completed:Object.keys(stageLabels)}[filter];
  return state.frames.filter(r => (camera === 'all' || cameraType(r) === camera) &&
    (mode === 'all' || (segmentationMode(r) || 'unknown') === mode) &&
    (!query || `${r.pig_id} ${r.capture_id} ${r.relative_dir}`.toLowerCase().includes(query)) &&
    (filter === 'excluded' ? !!r.excluded : !!stages && stages.every(stage => stageComplete(r, stage))));
}
function renderFilterDisclosure() {
  $('listFilters').hidden = filtersCollapsed;
  $('toggleFilters').textContent = filtersCollapsed ? '展开筛选' : '收起筛选';
  $('toggleFilters').setAttribute('aria-expanded', String(!filtersCollapsed));
  $('filterSummary').hidden = !filtersCollapsed;
  const summary = [filterLabels[$('filter').value] || filterLabels.all];
  const camera = $('cameraFilter').value, mode = $('modeFilter').value, query = $('search').value.trim();
  if (camera && camera !== 'all') summary.push({itof:'iToF',stereo:'双目',unknown:'未识别相机'}[camera] || camera);
  if (mode && mode !== 'all') summary.push({rgb:'RGB 分割',infrared:'IR 分割',unknown:'未识别来源'}[mode] || mode);
  if (query) summary.push('搜索“' + query + '”');
  $('filterSummary').textContent = summary.join(' · ');
  $('filterSummary').title = '当前筛选：' + summary.join('；');
}
function renderList() {
  const list = $('frames'), scrollTop = list.scrollTop || 0;
  const rows = visibleRows(), items = [];
  for (const row of rows) {
    const item = document.createElement('div'); item.className = 'frame' + (row.id === current ? ' active' : '');
    const check = document.createElement('input'); check.type = 'checkbox'; check.checked = selected.has(row.id);
    check.setAttribute('aria-label', '勾选 ' + row.relative_dir); check.disabled = running() || busy;
    check.onchange = () => { check.checked ? selected.add(row.id) : selected.delete(row.id); renderList(); controls(); };
    const button = document.createElement('button'); button.type = 'button';
    const title = document.createElement('strong'); title.textContent = '耳标 ' + row.pig_id;
    if (captureComplete(row)) {
      const complete = document.createElement('span'); complete.className = 'completionbadge';
      complete.textContent = '已采集完毕'; complete.title = '三个阶段的当前版本均已确认保留'; title.append(complete);
    }
    const capture = document.createElement('span'); capture.className = 'capture'; capture.textContent = row.capture_id;
    const chips = document.createElement('span'); chips.className = 'chips';
    for (const [stage, detail] of [
      ['cleaning', row.has_cleaning ? `已记录（已删 ${(row.removed_points || 0).toLocaleString()} 点）` : '待处理'],
      ['alignment', row.has_alignment ? '已确认' : '待对齐'],
      ['review', measurementLabel(row)],
    ]) {
      const value = stageState(row, stage), line = document.createElement('span');
      line.className = 'stage-status stage-' + (value.blocked_by ? 'reject' : value.decision || 'pending');
      line.textContent = stageLine(row, stage, detail); chips.append(line);
    }
    item.title = row.relative_dir + (row.validation_errors?.length ? '\n'+row.validation_errors.join('\n') :
      row.missing.length ? '\n缺少：'+row.missing.join('、') : '');
    button.append(title, capture, chips); button.disabled = alignmentBusy;
    button.onclick = () => selectCapture(row.id);
    item.append(check, button); items.push(item);
  }
  // Replace the complete list at once, then restore its position. Routine
  // selection and state updates must never inherit browser scroll anchoring.
  list.replaceChildren(...items);
  const visibleIds = new Set(rows.map(r => r.id)), hiddenSelection = [...selected].filter(id => !visibleIds.has(id)).length;
  $('selectedCount').textContent = `已勾选 ${selected.size}` + (hiddenSelection ? ` · 隐藏 ${hiddenSelection}` : '');
  $('selectedCount').title = hiddenSelection ? `当前筛选外还有 ${hiddenSelection} 次已勾选采集，生成／导出已勾选范围时会一并处理。` : '';
  $('selectAll').checked = rows.length > 0 && rows.every(r => selected.has(r.id));
  $('selectAll').indeterminate = rows.some(r => selected.has(r.id)) && !$('selectAll').checked;
  renderCameraProgress(cameraProgress());
  renderFilterDisclosure();
  list.scrollTop = scrollTop;
}
function selectCapture(id) {
  if (alignmentBusy || processingBusy) return;
  current = id; renderList(); renderViewer(); controls();
}
function nextVisibleCapture() {
  const rows = state ? visibleRows() : [], index = rows.findIndex(row => row.id === current);
  return index >= 0 ? rows[index+1] || null : null;
}
function setWorkspaceView(view) {
  const scrollTop = $('frames').scrollTop || 0;
  workspaceView = view;
  renderList(); renderViewer(); controls();
  // Changing the right-hand step must not move the selected card in the list.
  $('frames').scrollTop = scrollTop;
}
function centerActiveCapture(behavior = 'smooth') {
  const list = $('frames'), active = document.querySelector('.frame.active');
  if (!active) return;
  const listBox = list.getBoundingClientRect?.(), itemBox = active.getBoundingClientRect?.();
  if (listBox && itemBox && Number.isFinite(list.scrollTop)) {
    const delta = itemBox.top + itemBox.height/2 - (listBox.top + listBox.height/2);
    const top = Math.max(0, list.scrollTop + delta);
    if (typeof list.scrollTo === 'function') list.scrollTo({top, behavior});
    else list.scrollTop = top;
  } else {
    active.scrollIntoView?.({block:'center', behavior});
  }
}
function stageLine(row, stage, detail) {
  const value = stageState(row, stage);
  return stageLabels[stage] + '：' + (value.blocked_by ? '已停止（' + stageLabels[value.blocked_by] + '淘汰）' :
    (row.workflow_stages ? ({keep:'保留',pending:'待定',reject:'淘汰'}[value.decision] + ' · ') : '') + detail);
}
function currentVersionLabel(row) {
  return (row.has_cleaning ? '处理后点集' : '原始点集') + ' · ' + (row.has_alignment ? '猪体坐标' : '相机坐标');
}
function measurementLabel(row) {
  if (!row.has_alignment) return '待对齐';
  const total = row.measurement_slice_count || 0, usable = row.measurement_usable_slices || 0;
  const unusable = row.measurement_unusable_slices || 0;
  if (row.measurement_ready) return `已复核 ${usable}/${total} 片`;
  if (!total) return '待复核';
  return `已确认 ${usable}/${total} 片` + (unusable ? ` · 淘汰 ${unusable}` : '');
}
function currentFiles(row) {
  return {ply:row.current_ply || (row.has_alignment ? (row.has_cleaning ? 'cloud_cleaned_body_mm.ply' : 'cloud_body_mm.ply') :
    row.has_cleaning ? 'cloud_cleaned_mm.ply' : row.cloud_ply || '相机点云.ply'),
    npz:row.current_npz || (row.has_alignment ? (row.has_cleaning ? 'cleaned_body_points.npz' : 'body_points.npz') :
      row.has_cleaning ? 'cleaned_points.npz' : 'points.npz')};
}
function renderViewer() {
  const row = state.frames.find(r => r.id === current);
  if (row?.workflow_stages?.[workspaceView]?.blocked_by) workspaceView = row.workflow_stages[workspaceView].blocked_by;
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
  const stage = hasCloud ? stageState(row) : {};
  $('stageReviewLabel').textContent = {cleaning:'清理筛选',alignment:'对齐筛选',review:'测量筛选'}[workspaceView];
  $('cloudReviewHint').classList.toggle('stale', !!stage.stale);
  $('cloudReviewHint').textContent = hasCloud ? `筛选对象：本次采集的“${stageLabels[workspaceView]}”阶段；测量复核：${measurementLabel(row)}。` +
    (stage.stale ? '本阶段数据变化，筛选已回到待定，请重新确认。' : stage.decision === 'reject' ?
      '本阶段已淘汰，后续阶段停止；前面已完成的阶段仍可导出。' : '三个阶段分别筛选、分别导出；本阶段淘汰会停止后续阶段。') +
    (workspaceView === 'review' ? '“确认所选”仅确认选区；本阶段点击“保留”时，其余待定剖面自动淘汰，已有可测／不可测标记保留。' : '') : '';
  $('cloudKeep').title = workspaceView === 'review' ? '保留本次测量复核，并自动淘汰其余待定剖面；已有可测／不可测标记保留' : '本阶段保留，不替代其他阶段的筛选结论';
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
  const editorKey = hasCloud ? baseEditorKey + ':' + (row.removed_points ? row.cleaning_digest || '' : '') : '';
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
    processingInstance++;
    $('processingEditor').src = '/processing?' + new URLSearchParams({id:row.id, dataset_id:state.dataset_id,
      mode:workspaceView === 'review' ? 'review' : 'cleaning', editor_instance:String(processingInstance)});
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
  $('alignCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud ||
    !!state?.frames.find(r => r.id === current)?.workflow_stages?.alignment?.blocked_by;
  $('reviewCurrent').disabled = locked || !state?.frames.find(r => r.id === current)?.has_cloud ||
    !!state?.frames.find(r => r.id === current)?.workflow_stages?.review?.blocked_by;
  $('autoNextAlignment').disabled = locked;
  const nextCapture = nextVisibleCapture();
  $('nextCapture').disabled = locked || !nextCapture;
  $('nextCapture').title = nextCapture
    ? `切换到当前筛选结果中的下一条：${nextCapture.pig_id} / ${nextCapture.capture_id}`
    : '当前筛选结果中没有下一条采集';
  for (const id of ['search','filter','cameraFilter','modeFilter']) $(id).disabled = alignmentBusy || processingBusy;
  document.querySelectorAll('.frame>button').forEach(el => { el.disabled = alignmentBusy || processingBusy; });
  $('alignmentEditor').inert = busy || running();
  $('processingEditor').inert = busy || running();
  $('reveal').disabled = busy || !state?.results;
  const active = state?.frames.find(r => r.id === current);
  for (const [decision, id] of Object.entries(reviewButtons)) {
    $(id).disabled = locked || !active?.has_cloud || !stageState(active).available ||
      decision === 'keep' && !stageState(active).has_output;
    $(id).setAttribute('aria-pressed', String(!!active?.has_cloud && stageDecision(active) === decision));
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
    $('datasetSummary').textContent = dirty() ? '输入目录已更改\n请重新核对载入' : `${state.pigs} 头猪 · ${state.frames.length} 次采集\n已采集完毕 ${state.frames.filter(captureComplete).length} 次`;
    $('datasetSummary').title = `分割已确认 ${state.reviewed} 次；已采集完毕表示三个阶段的当前版本均已确认保留。`;
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
  for (const id of ['exportLayout','exportParent','exportName','browseExportParent','exportKeep','exportPending','exportReject', ...Object.values(stageOptions)]) $(id).disabled = locked;
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
  const next = plan.candidates.map(id=>rows.find(r=>r.id===id)).find(r=>r?.has_cloud && !r.has_alignment &&
    !stageState(r,'alignment').blocked_by && stageDecision(r,'alignment')!=='reject');
  if (next) { selectCapture(next.id); message('上一条已保存，已自动切换到下一条待对齐采集。'); }
  else message('本条已保存，当前筛选范围后面已没有待对齐采集。可在左侧回看，或导出点云成果。');
}
for (const [decision, button] of Object.entries(reviewButtons)) $(button).onclick = () => {
  const id = current, datasetId = state.dataset_id, plan = advancePlan(), stage = workspaceView, editorKey = processingKey;
  const version = state.frames.find(r=>r.id===id)?.workflow_stages?.[stage]?.version;
  return action(async () => {
    const response = await api('review_cloud', {dataset_id:datasetId, id, decision, stage, ...(version ? {stage_version:version} : {})});
    if (state.dataset_id !== datasetId || response.dataset_id !== datasetId) return;
    const finalizeMeasurement = stage === 'review' && decision === 'keep';
    if (finalizeMeasurement && current === id && workspaceView === 'review' && processingKey === editorKey) processingKey = '';
    accept(response, false, true);
    pendingAdvance = plan;
    const rejected = response.measurement_finalization?.auto_rejected_profiles;
    const finalizedRow = response.frames?.find(r=>r.id===id);
    const measurementSummary = finalizedRow?.measurement_slice_count ? [
      `当前 ${finalizedRow.measurement_usable_slices || 0}/${finalizedRow.measurement_slice_count} 片可测`,
      `${finalizedRow.measurement_unusable_slices || 0} 片淘汰`,
      `${finalizedRow.measurement_pending_slices || 0} 片待定`
    ].join(' · ') + '。' : '';
    message(finalizeMeasurement ? '测量复核已保留；' + (Number.isInteger(rejected) && rejected > 0 ?
      `本次自动淘汰 ${rejected} 片待定剖面。` : '当前没有待定剖面，无需再次淘汰。') + measurementSummary :
      stageLabels[stage] + '已标记为' + {keep:'保留',pending:'待定',reject:'淘汰'}[decision] +
      (decision === 'reject' ? '；本采集后续阶段停止，前序成果仍可导出。' : '；其他阶段的筛选结论分别保存。'));
  });
};
$('autoNextAlignment').onchange = () => {
  if (!$('autoNextAlignment').checked) pendingAdvance = null;
  try { localStorage.setItem('pointcloud-auto-next', $('autoNextAlignment').checked ? '1' : '0'); } catch {}
};
$('nextCapture').onclick = () => {
  const rows = state ? visibleRows() : [], index = rows.findIndex(row => row.id === current), next = nextVisibleCapture();
  if (!next) return;
  workspaceView = 'cleaning';
  selectCapture(next.id);
  requestAnimationFrame(() => centerActiveCapture('smooth'));
  message(`已切换到第 ${index+2}/${rows.length} 条采集：${next.pig_id} / ${next.capture_id}，并进入点云清理。`);
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
      const fresh = data.workspace_state || await api('state');
      if (data.dataset_id !== state.dataset_id || fresh.dataset_id !== state.dataset_id || data.id !== current) return;
      accept(fresh, false, true);
      pendingAdvance = plan;
      message(data.realigned ? '坐标对齐已更新，测量复核将按新坐标重新计算。请复核新剖面。' :
        '坐标对齐已保存，可进入“测量复核”检查切片；采集筛选请单独确认。');
      advanceAfterSave();
    } catch(e) { message('成果已保存，工作台状态更新失败：'+e.message+'；请刷新页面。', true); }
  }
});
window.addEventListener('message', async event => {
  const data = event.data;
  if (event.origin !== location.origin || event.source !== $('processingEditor').contentWindow ||
      data?.type !== 'pointcloud-processing' || !processingKey || data.dataset_id !== state?.dataset_id || data.id !== current ||
      data.editor_instance !== String(processingInstance)) return;
  if (data.action === 'busy') { processingBusy = data.busy === true; controls(); }
  if (data.action === 'saved') {
    try {
      const fresh = await api('state');
      if (processingKey && fresh.dataset_id === state?.dataset_id && data.id === current &&
          data.editor_instance === String(processingInstance)) accept(fresh, false, true);
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
  // Each filter is a new working scope: clear old hidden selections and
  // select every capture in the rows that remain visible.
  selected = new Set(visibleRows().map(row => row.id));
  ensureVisibleCapture();
  renderList(); renderViewer(); controls();
}
for (const id of ['search','filter']) $(id).addEventListener('input', applyFilters);
for (const id of ['cameraFilter','modeFilter']) $(id).onchange = applyFilters;
$('toggleFilters').onclick = () => {
  filtersCollapsed = !filtersCollapsed;
  try { localStorage.setItem('pointcloud-filters-collapsed', filtersCollapsed ? '1' : '0'); } catch {}
  renderFilterDisclosure();
};
renderFilterDisclosure();
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
  return exportScopeRows().filter(r => Object.keys(exportStageEntries(r)).length);
}
function exportStages() { return Object.entries(stageOptions).filter(([,id])=>$(id).checked).map(([stage])=>stage); }
function exportStageEntries(row) {
  const decisions = exportDecisions();
  return Object.fromEntries(exportStages().map(stage=>[stage,stageState(row,stage)]).filter(([,value])=>
    !value.blocked_by && (value.has_output || value.reviewed) && decisions.includes(value.decision)));
}
const exportReviewOptions = {keep:'exportKeep', pending:'exportPending', reject:'exportReject'};
function exportDecisions() { return Object.entries(exportReviewOptions).filter(([,id])=>$(id).checked).map(([decision])=>decision); }
function exportPlan() {
  const rows = exportRows(), scope = exportScopeRows();
  $('exportHint').textContent = '分别勾选要导出的阶段。后续未完成不影响前序成果，前序淘汰后停止后续。测量复核会一起导出整体几何特征（JSON＋批量 CSV）和剖面序列（CSV／NPZ），保留缺失值与复核标记。';
  const split = $('exportLayout').value === 'split';
  $('exportLayoutHint').textContent = split ? '各阶段只保存自身成果，按该阶段的保留／待定／淘汰分类；不重复复制原始文件、检查图和其他阶段的点云。精简成果用于交付与分析，需要继续编辑时选择“按采集合并存放”。' : '同一采集的所选阶段合并存放，按最后导出阶段的状态分类；包含原始生成文件与处理历史，可重新打开继续编辑。各阶段结论分别保存在清单。';
  $('exportCount').textContent = `${rows.length} 次采集 · ${rows.reduce((n,r)=>n+Object.keys(exportStageEntries(r)).length,0)} 项阶段成果`;
  $('exportEligibility').textContent = `范围内 ${scope.length} 次；本次导出：` + exportStages().map(stage=>
    stageLabels[stage] + ' ' + rows.filter(r=>exportStageEntries(r)[stage]).length + ' 次').join('、') + '。' +
    ` 测量已复核 ${rows.filter(r=>exportStageEntries(r).review && r.measurement_ready).length} 次；部分复核保留逐片状态。` +
    (!rows.length ? (!exportStages().length ? ' 请至少勾选一个导出阶段。' : !exportDecisions().length ? ' 请至少勾选一种导出状态。' : ' 当前范围没有符合阶段与状态的成果。') : '');
  $('confirmExport').disabled = !rows.length || busy || running() || alignmentBusy || processingBusy;
}
function openExport(scope) {
  document.querySelector(`input[name=scope][value=${scope}]`).checked = true;
  if (!['split','merged'].includes($('exportLayout').value)) $('exportLayout').value = 'split';
  for (const id of Object.values(exportReviewOptions)) $(id).checked = true;
  for (const id of Object.values(stageOptions)) $(id).checked = true;
  $('exportParent').value = $('parent').value;
  $('exportName').value = '处理点云_' + new Date().toISOString().replace(/[-:TZ.]/g,'').slice(0,17);
  $('exportMessage').textContent = '';
  exportPlan(); $('exportDialog').showModal();
}
$('exportCurrent').onclick = () => openExport('current');
$('export').onclick = () => openExport([...selected].some(id => state.frames.some(r => r.id === id && r.has_cloud)) ? 'selected' : 'all');
$('exportLayout').onchange = exportPlan;
document.querySelectorAll('input[name=scope]').forEach(input => input.onchange = exportPlan);
for (const id of Object.values(exportReviewOptions)) $(id).onchange = exportPlan;
for (const id of Object.values(stageOptions)) $(id).onchange = exportPlan;
for (const id of ['exportParent','exportName']) $(id).addEventListener('input', exportPlan);
$('browseExportParent').onclick = () => action(() => choose('exportParent','选择点云导出目录'));
$('confirmExport').onclick = () => action(async () => {
  $('confirmExport').disabled = true; $('exportMessage').textContent = '';
  try {
    accept(await api('export', {dataset_id:state.dataset_id, ids:exportRows().map(r=>r.id), scope_ids:exportScopeRows().map(r=>r.id),
      parent:$('exportParent').value.trim(), name:$('exportName').value,
      export_layout:$('exportLayout').value, stages:exportStages(), decisions:exportDecisions(), group_by_decision:true}));
    $('parent').value = $('exportParent').value; $('name').value = state.suggested_name;
    $('exportDialog').close(); message('开始'+($('exportLayout').value==='split'?'按阶段拆分':'按采集合并')+'导出，测量结果和各阶段筛选结论将一起保存。');
  } catch(e) { $('exportMessage').textContent = e.message; throw e; }
  finally { exportPlan(); }
});
action(async () => { accept(await api('state')); });
