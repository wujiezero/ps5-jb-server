'use strict';

/* ── 策略保存 / 回退 / 退出 ─────────────────────────────────────────── */

async function mutate(path, body, action, pendingLabel, successMessage, afterSuccess) {
  if (busy || !authenticated) return;
  stopPolling();
  busy = true;
  epoch += 1; // 丢弃本次写入之前取回的状态响应
  const requestEpoch = epoch;
  actBusy(action, true, pendingLabel);
  updateControls();
  let succeeded = false;
  try {
    const result = await api(path, body);
    if (requestEpoch !== epoch) return;
    if (result.ok !== true) throw new ApiError('服务未确认操作成功，请刷新状态后检查。');
    succeeded = true;
    if (afterSuccess) afterSuccess();
  } catch (error) {
    if (requestEpoch !== epoch) return;
    if (error.status === 401) showLogin(`${error.message}${dirty ? ' 未保存的编辑已暂存。' : ''}`);
    else toast(error.message, 'error');
  } finally {
    busy = false;
    actBusy(action, false);
    updateControls();
  }
  if (succeeded && authenticated) {
    if (successMessage) toast(successMessage, 'success');
    const appliedEpoch = epoch;
    try {
      refreshing = true;
      updateControls();
      const data = await api('/api/state');
      if (appliedEpoch === epoch) renderState(data, false);
    } catch (error) {
      if (appliedEpoch === epoch) {
        if (error.status === 401) showLogin(error.message);
        else if (successMessage) toast(`${successMessage} 但状态读取失败：${error.message}`, 'info');
      }
    } finally {
      refreshing = false;
      updateControls();
    }
  }
  schedulePoll();
}

function saveConfig() {
  if (busy || !authenticated || !dirty) return;
  const config = readConfig();
  if (!validate(config)) return;
  mutate('/api/config', config, 'save', '正在应用…', '配置已保存并应用。', () => {
    baseline = signature(config);
    remoteSignature = baseline;
    dirty = false;
    updateControls();
  });
}

function openRollbackDialog() {
  if (!authenticated || busy) return;
  $('rollback-dialog').showModal();
  $('rollback-cancel').focus({ preventScroll: true });
}

function confirmRollback() {
  $('rollback-dialog').close();
  if (busy || !authenticated) return;
  mutate('/api/rollback', {}, 'rollback', '正在回滚…', '已回滚到上一版配置。', () => {
    baseline = signature(readConfig());
    dirty = false;
  });
}

function logout() {
  if (busy || !authenticated) return;
  if (dirty && !window.confirm('有未保存的修改。退出登录将丢弃这些修改，是否继续？')) return;
  mutate('/api/logout', {}, 'logout', '正在退出…', '', () => showLogin('已退出登录。', true));
}
