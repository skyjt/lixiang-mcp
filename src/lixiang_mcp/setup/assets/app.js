'use strict';
const $ = id => document.getElementById(id);
let capability = location.hash.slice(1);
if (capability) { sessionStorage.setItem('lixiang-setup', capability); history.replaceState(null, '', '/'); }
else capability = sessionStorage.getItem('lixiang-setup') || '';
let state = null, pending = false;
const descriptions = {
  new: ['开始本机接入', '输入本人账号，应用配置和设备身份无需手工填写。'],
  cancelled: ['已取消', '密码、签名和会话已从向导检查点移除，设备身份保留供本人下次使用。'],
  authenticating: ['正在登录', '正在处理这一次请求；无需重复点击。'],
  verification_required: ['需要本人完成官方验证', '打开下方官方页面，完成后回到这里继续。'],
  signing_required: ['账号已登录，签名材料尚未就绪', '目前还不能读取车辆。可导入本人材料，或取消并保留设备身份。'],
  device_change_required: ['需要对齐设备身份', '签名材料与当前设备不一致；必须重新登录，不能直接拼接使用。'],
  discovering: ['正在读取账号车辆', '只读取获准车辆，不执行任何车辆控制。'],
  select_vehicles: ['请选择要接入的车辆', '确认所选车辆属于本人授权范围。所有控制和位置权限仍关闭。'],
  ready: ['只读配置已生成', '这表示本次接口流程完成，不等于车型功能或实车控制已验证。'],
  login_failed: ['本次接入未完成', '请核对下方固定错误码；可修改账号密码或明确重试。不要连续盲试。'],
  blocked: ['已达到本轮尝试上限', '等待冷却后由本人明确重试；重启不会清除次数记录。'],
  interrupted: ['上次接入已中断', '设备与本地进度已保留；只有点击继续才会重新联网。']
};
function show(id, yes) { $(id).hidden = !yes; }
function render(next) {
  state = next;
  const [title, detail] = descriptions[state.phase] || ['状态不可用', '请重启本机向导并检查本地文件。'];
  $('phase').textContent = title; $('detail').textContent = detail;
  $('error').textContent = state.error ? '错误码：' + state.error : '';
  $('budget').textContent = '本轮剩余尝试：' + state.attempts_remaining + (state.retry_after_seconds ? '；可重试前需等待约 ' + state.retry_after_seconds + ' 秒' : '');
  $('profile').textContent = '内置协议配置：' + state.profile;
  show('credentials', ['new', 'cancelled', 'login_failed', 'ready'].includes(state.phase));
  show('official', state.phase === 'verification_required');
  if (state.official_url) $('official-link').href = state.official_url;
  else $('official-link').removeAttribute('href');
  show('signing', ['signing_required', 'device_change_required', 'login_failed'].includes(state.phase));
  show('device', state.phase === 'device_change_required');
  show('vehicles', state.phase === 'select_vehicles');
  show('ready', state.phase === 'ready');
  show('retry', ['login_failed', 'blocked', 'interrupted'].includes(state.phase));
  show('cancel', !['new', 'cancelled', 'ready'].includes(state.phase));
  if (state.config_file) $('config').textContent = state.config_file;
  const choices = $('choices');
  if (choices.dataset.revision !== String(state.revision)) {
    choices.replaceChildren(); choices.dataset.revision = String(state.revision);
    for (const car of state.vehicles || []) {
      const label = document.createElement('label'), input = document.createElement('input');
      input.type = 'checkbox'; input.value = car.vehicle_id; input.checked = car.selected;
      label.append(input, document.createTextNode(car.label + ' · VIN 尾号 ' + car.vin_tail)); choices.append(label);
    }
  }
  for (const button of document.querySelectorAll('button')) button.disabled = pending;
}
async function api(path, body) {
  const response = await fetch(path, {method: body ? 'POST' : 'GET', cache: 'no-store',
    headers: {'Authorization': 'Bearer ' + capability, ...(body ? {'Content-Type': 'application/json'} : {})},
    ...(body ? {body: JSON.stringify(body)} : {})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'local_request_failed');
  return data;
}
async function action(name, extra = {}) {
  if (pending || !state) return;
  pending = true; render(state);
  try { render(await api('/api/action', {action: name, revision: state.revision, ...extra})); }
  catch (error) { $('error').textContent = '错误码：' + error.message; }
  finally { pending = false; for (const button of document.querySelectorAll('button')) button.disabled = false; }
}
$('credentials').addEventListener('submit', event => {
  event.preventDefault(); const form = new FormData(event.target);
  const values = {phone: form.get('phone'), password: form.get('password')};
  event.target.reset(); action('start', values);
});
$('continue').onclick = () => action('continue');
$('retry').onclick = () => action('retry');
$('cancel').onclick = () => action('cancel');
$('use-device').onclick = () => action('use_device');
$('select').onclick = () => action('select', {selected: [...$('choices').querySelectorAll('input:checked')].map(input => input.value)});
$('material').onchange = async event => {
  const file = event.target.files[0]; event.target.value = '';
  if (!file || file.size > 8192) { $('error').textContent = '签名材料文件大小不正确'; return; }
  try { await action('import_signing', {signing: JSON.parse(await file.text())}); }
  catch { $('error').textContent = '签名材料应为有效 JSON，不要上传其他文件'; }
};
async function poll() {
  if (!capability) { $('phase').textContent = '请从本机私密 launch.html 文件打开向导'; return; }
  if (!pending) {
    try { render(await api('/api/status')); }
    catch { $('error').textContent = '本机连接或授权已失效；若重启过服务，请重新打开私密 launch.html。'; }
  }
  setTimeout(poll, 1500);
}
poll();
