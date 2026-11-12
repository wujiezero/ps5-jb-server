'use strict';

/* ── 事件绑定 ───────────────────────────────────────────────────────── */
// Loaded last (see index.html): every other script's declarations exist by now.

function bindEvents() {
  for (const dialog of $$('dialog')) {
    animateDialogClose(dialog);
    bindSheetSwipe(dialog);
  }
  $('action-sheet-cancel').addEventListener('click', () => $('action-sheet').close());
  $('action-sheet').addEventListener('close', onActionSheetClosed);
  // Tap on the dimmed area above a phone sheet closes it, as native sheets do.
  $('action-sheet').addEventListener('click', (event) => {
    if (event.target === $('action-sheet')) $('action-sheet').close();
  });
  $('login-form').addEventListener('submit', submitLogin);
  $('mobile-more-trigger').addEventListener('click', openMobileMore);
  $('mobile-more-close').addEventListener('click', () => closeMobileMore());
  $('mobile-more-dialog').addEventListener('close', restoreMobileMoreFocus);
  for (const link of $$('.more-link[data-section]')) {
    link.addEventListener('click', () => {
      closeMobileMore('navigation');
      if (sectionFromHash() === link.dataset.section) showSection(link.dataset.section, { moveFocus: true });
    });
  }
  window.matchMedia('(max-width: 767px)').addEventListener('change', (event) => {
    if (event.matches) return;
    closeMobileMore('resize');
    // The action sheet is phone-only; wider layouts show the inline controls.
    if ($('action-sheet').open) $('action-sheet').close();
  });
  $('repo-search').addEventListener('input', () => {
    repoRendered = '';
    renderRepo();
  });
  $('activity-search').addEventListener('input', () => {
    renderActivityRows();
    updateActivityControls();
    flushActivityPending();
  });
  $('ps5-catalog-search').addEventListener('input', () => {
    ps5CatalogKey = '';
    const host = ps5SelectedHost();
    if (host) renderPs5Catalog(host);
  });

  for (const field of fields) {
    field.addEventListener('input', () => {
      updateControls();
      if (field.hasAttribute('aria-invalid')) clearErrors();
    });
  }

  $('config-form').addEventListener('submit', (event) => {
    event.preventDefault();
    saveConfig();
  });
  for (const button of acts('save')) {
    if (button.type === 'button') button.addEventListener('click', saveConfig);
  }
  for (const button of acts('rollback')) button.addEventListener('click', openRollbackDialog);
  $('rollback-cancel').addEventListener('click', () => $('rollback-dialog').close());
  $('rollback-confirm').addEventListener('click', confirmRollback);

  for (const button of acts('logout')) button.addEventListener('click', logout);
  for (const button of acts('refresh')) button.addEventListener('click', handleRefresh);
  for (const button of acts('upload')) button.addEventListener('click', () => openUploadDialog());
  $('repo-copy-url').addEventListener('click', copyRepoUrl);

  $('update-check').addEventListener('click', checkUpdates);
  $('update-apply').addEventListener('click', applyUpdate);
  $('update-retry').addEventListener('click', retryUpdateConnection);
  $('update-reload').addEventListener('click', () => location.reload());
  $('password-form').addEventListener('submit', submitPassword);
  for (const [field, error] of [['current-password', 'current-password-error'], ['new-password', 'new-password-error'], ['confirm-password', 'confirm-password-error']]) {
    $(field).addEventListener('input', () => {
      if ($(field).hasAttribute('aria-invalid')) {
        $(field).removeAttribute('aria-invalid');
        $(error).textContent = '';
        $(error).hidden = true;
      }
      if (!$('password-form-error').hidden) {
        $('password-form-error').textContent = '';
        $('password-form-error').hidden = true;
      }
      if (!$('password-success').hidden) {
        $('password-success').textContent = '';
        $('password-success').hidden = true;
      }
    });
  }

  bindUploadAutofill();
  $('repo-upload-form').addEventListener('submit', submitUpload);
  $('repo-upload-cancel').addEventListener('click', closeUploadDialog);
  for (const [field, error] of [['repo-upload-file', 'repo-upload-file-error'], ['repo-upload-name', 'repo-upload-name-error'], ['repo-upload-version', 'repo-upload-version-error'], ['repo-upload-category', 'repo-upload-category-error'], ['repo-upload-author', 'repo-upload-author-error'], ['repo-upload-description', 'repo-upload-description-error']]) {
    const clear = () => {
      $(field).removeAttribute('aria-invalid');
      $(error).textContent = '';
      $(error).hidden = true;
      $('repo-upload-error').textContent = '';
      $('repo-upload-error').hidden = true;
    };
    $(field).addEventListener('input', clear);
    $(field).addEventListener('change', clear);
  }

  $('repo-edit-form').addEventListener('submit', submitEdit);
  $('repo-edit-cancel').addEventListener('click', () => $('repo-edit-dialog').close());
  $('repo-edit-dialog').addEventListener('cancel', (event) => { if (repoBusy) event.preventDefault(); });
  $('repo-edit-dialog').addEventListener('close', () => { editTarget = null; flushRepoPending(); });

  $('repo-delete-cancel').addEventListener('click', () => $('repo-delete-dialog').close());
  $('repo-delete-confirm').addEventListener('click', confirmDelete);
  $('repo-delete-dialog').addEventListener('cancel', (event) => { if (repoBusy) event.preventDefault(); });
  $('repo-delete-dialog').addEventListener('close', () => { deleteTarget = null; flushRepoPending(); });

  $('repo-upload-dialog').addEventListener('cancel', (event) => { if (uploading) event.preventDefault(); });
  $('repo-upload-dialog').addEventListener('close', () => { flushRepoPending(); });
  $('repo-upstream-check').addEventListener('click', checkUpstream);
  $('repo-upstream-all').addEventListener('click', applyAllUpstream);
  $('repo-versions-close').addEventListener('click', () => $('repo-versions-dialog').close());
  $('repo-versions-dialog').addEventListener('cancel', (event) => { if (repoBusy) event.preventDefault(); });
  $('repo-versions-dialog').addEventListener('close', () => { versionsTarget = null; flushRepoPending(); });

  for (const button of acts('allow')) button.addEventListener('click', openActivityAllowDialog);
  for (const button of acts('label')) button.addEventListener('click', openActivityLabelDialog);
  $('activity-filter').addEventListener('change', () => setActivityFilter($('activity-filter').value));
  $('activity-kind').addEventListener('change', () => setActivityKind($('activity-kind').value));
  try { setActivityKind(localStorage.getItem('ps5jb.activityKind') || 'all'); } catch { /* private mode */ }
  for (const segment of $$('.segment')) segment.addEventListener('click', () => setActivityFilter(segment.dataset.filter));

  $('activity-select-all').addEventListener('change', () => {
    applySelectAll($('activity-select-all').checked);
  });
  $('activity-select-all-mobile').addEventListener('click', () => {
    const targets = collectVisibleTargets();
    const allSelected = targets.length > 0 && targets.every((target) => activitySelected.has(target));
    applySelectAll(!allSelected);
  });

  $('activity-allow-cancel').addEventListener('click', () => $('activity-allow-dialog').close());
  $('activity-allow-confirm').addEventListener('click', confirmActivityAllow);
  $('activity-allow-dialog').addEventListener('cancel', (event) => { if (activityBusy) event.preventDefault(); });
  $('activity-allow-dialog').addEventListener('close', () => { flushActivityPending(); });

  $('activity-label-form').addEventListener('submit', submitActivityLabels);
  $('activity-label-cancel').addEventListener('click', () => $('activity-label-dialog').close());
  $('activity-label-dialog').addEventListener('cancel', (event) => { if (activityBusy) event.preventDefault(); });
  $('activity-label-dialog').addEventListener('close', () => { flushActivityPending(); });

  // PS5 主机
  $('ps5-scan').addEventListener('click', () => scanPs5({ manual: true }));
  $('ps5-add-form').addEventListener('submit', submitPs5Add);
  $('ps5-add-ip').addEventListener('input', () => {
    if ($('ps5-add-ip').hasAttribute('aria-invalid')) setPs5AddError('');
  });
  $('view-ps5').addEventListener('click', onPs5ViewClick);
  bindPs5PowerEvents();
  $('ps5-tabs').addEventListener('keydown', onPs5TabsKeydown);
  $('ps5-autoload-enabled').addEventListener('change', onPs5AutoloadToggle);
  $('ps5-autoload-items').addEventListener('change', onPs5AutoloadFile);
  $('ps5-autoload-items').addEventListener('click', onPs5AutoloadClick);
  $('ps5-autoload-start-delay').addEventListener('change', onPs5AutoloadStartDelay);
  $('ps5-autoload-add').addEventListener('click', onPs5AutoloadAdd);
  $('ps5-autoload-save').addEventListener('click', requestPs5AutoloadSave);
  $('ps5-shutdown-cancel').addEventListener('click', () => $('ps5-shutdown-dialog').close());
  $('ps5-shutdown-confirm').addEventListener('click', confirmPs5Shutdown);
  $('ps5-kill-cancel').addEventListener('click', () => $('ps5-kill-dialog').close());
  $('ps5-kill-confirm').addEventListener('click', confirmPs5Kill);
  $('ps5-autoload-dialog-cancel').addEventListener('click', () => $('ps5-autoload-dialog').close());
  $('ps5-autoload-dialog-confirm').addEventListener('click', () => {
    const target = ps5AutoloadTarget;
    if (target) submitPs5Autoload(target);
  });
  for (const id of ['ps5-shutdown-dialog', 'ps5-kill-dialog', 'ps5-autoload-dialog']) {
    const dialog = $(id);
    dialog.addEventListener('cancel', (event) => { if (ps5Busy) event.preventDefault(); });
    dialog.addEventListener('close', () => {
      ps5ShutdownIp = '';
      ps5KillTarget = null;
      ps5AutoloadTarget = null;
      flushPs5Pending();
      ps5RestoreFocus();
    });
  }

  // PS5 文件
  $('view-files').addEventListener('click', onFilesViewClick);
  $('files-upload').addEventListener('click', () => $('files-upload-input').click());
  $('files-upload-input').addEventListener('change', (event) => {
    const list = event.target.files ? Array.from(event.target.files) : [];
    event.target.value = '';
    if (list.length) queueFilesUpload(list);
  });
  $('files-mkdir').addEventListener('click', () => openFilesNameDialog('mkdir', null));
  $('files-refresh').addEventListener('click', () => refreshFilesPage({ manual: true }));
  for (const button of $$('[data-files-transfers]')) button.addEventListener('click', () => toggleFilesDrawer());
  $('xfer-pill').addEventListener('click', () => toggleFilesDrawer());
  document.addEventListener('pointerdown', (event) => {
    if (!files.drawer || dialogsOpen()) return;
    if (event.target.closest('#xfer-dock, [data-files-transfers], #nas-send, #nas-extract, #files-upload, dialog')) return;
    toggleFilesDrawer(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && files.drawer && !dialogsOpen()) {
      toggleFilesDrawer(false);
      $('xfer-pill').focus({ preventScroll: true });
    }
  });
  $('files-transfers-clear').addEventListener('click', clearFilesTransfers);
  bindNasEvents();
  $('files-transfers-close').addEventListener('click', () => toggleFilesDrawer(false));
  $('files-setup-connect').addEventListener('click', connectFilesSetup);
  $('files-setup-host').addEventListener('change', () => setFilesSetupError(''));
  $('files-retry').addEventListener('click', () => {
    setFilesError('');
    loadFiles({ manual: true });
  });
  $('files-switch-host').addEventListener('click', openFilesHostDialog);
  $('files-filter').addEventListener('input', () => {
    files.filter = $('files-filter').value;
    renderFilesRows();
    renderFilesStatus();
  });
  $('files-name-form').addEventListener('submit', submitFilesName);
  $('files-name-cancel').addEventListener('click', () => $('files-name-dialog').close());
  $('files-name-input').addEventListener('input', () => {
    $('files-name-input').removeAttribute('aria-invalid');
    $('files-name-error').textContent = '';
    $('files-name-error').hidden = true;
  });
  $('files-delete-cancel').addEventListener('click', () => $('files-delete-dialog').close());
  $('files-delete-confirm').addEventListener('click', confirmFilesDelete);
  $('files-overwrite-cancel').addEventListener('click', () => settleFilesOverwrite('cancel'));
  $('files-overwrite-skip').addEventListener('click', () => settleFilesOverwrite('skip'));
  $('files-overwrite-confirm').addEventListener('click', () => settleFilesOverwrite('overwrite'));
  $('files-dest-cancel').addEventListener('click', () => $('files-dest-dialog').close());
  $('files-dest-confirm').addEventListener('click', confirmFilesDest);
  $('files-dest-root').addEventListener('click', () => loadFilesDest(''));
  $('files-dest-up').addEventListener('click', () => {
    const volume = filesVolumeOf(files.destPath);
    if (!files.destPath || !volume || files.destPath === volume.path) loadFilesDest('');
    else loadFilesDest(filesParent(files.destPath));
  });
  $('files-host-cancel').addEventListener('click', () => $('files-host-dialog').close());
  $('files-host-confirm').addEventListener('click', () => {
    const host = $('files-host-select').value;
    if (!host) {
      $('files-host-error').textContent = '请选择主机。';
      $('files-host-error').hidden = false;
      return;
    }
    configureFilesHost(host, { closeDialog: true });
  });
  $('files-menu').addEventListener('click', onFilesMenuAction);
  document.addEventListener('click', (event) => {
    if (event.target.closest && (event.target.closest('#files-menu') || event.target.closest('[data-files-menu-button]'))) return;
    closeFilesMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('files-menu').hidden) {
      event.preventDefault();
      closeFilesMenu();
    }
  });
  $('files-name-dialog').addEventListener('cancel', (event) => { if (files.busy) event.preventDefault(); });
  $('files-delete-dialog').addEventListener('cancel', (event) => { if (files.busy) event.preventDefault(); });
  $('files-dest-dialog').addEventListener('cancel', (event) => { if (files.busy) event.preventDefault(); });
  $('files-host-dialog').addEventListener('cancel', (event) => { if (files.configBusy) event.preventDefault(); });
  $('files-delete-dialog').addEventListener('close', () => { files.deleteTarget = null; });
  $('files-overwrite-dialog').addEventListener('close', () => {
    if (!filesOverwritePending) return;
    const pending = filesOverwritePending;
    filesOverwritePending = null;
    pending.onChoice('cancel');
  });
  bindFilesDrag();

  for (const dialog of $$('dialog.dialog')) {
    dialog.addEventListener('keydown', (event) => trapDialogFocus(event, dialog));
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      if (dialog === $('repo-edit-dialog') && repoBusy) return;
      if (dialog === $('repo-delete-dialog') && repoBusy) return;
      if (dialog === $('repo-upload-dialog') && uploading) return;
      if (dialog === $('activity-allow-dialog') && activityBusy) return;
      if (dialog === $('activity-label-dialog') && activityBusy) return;
      if (dialog === $('ps5-shutdown-dialog') && ps5Busy) return;
      if (dialog === $('ps5-kill-dialog') && ps5Busy) return;
      if (dialog === $('ps5-autoload-dialog') && ps5Busy) return;
      if (dialog === $('files-name-dialog') && files.busy) return;
      if (dialog === $('files-delete-dialog') && files.busy) return;
      if (dialog === $('files-dest-dialog') && files.busy) return;
      if (dialog === $('files-host-dialog') && files.configBusy) return;
      dialog.close();
    });
  }

  window.addEventListener('hashchange', () => ensureRoute({ moveFocus: true }));
  window.addEventListener('beforeunload', (event) => {
    if (!dirty && !uploading && !files.uploading) return;
    event.preventDefault();
    event.returnValue = '';
  });
}

function bindPolishEvents() {
  initHelpTips();
  bindBackupEvents();
  window.addEventListener('error', (event) => {
    noteClient('error', `${event.message || '脚本错误'}${event.filename ? ` @ ${event.filename}:${event.lineno}` : ''}`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    noteClient('error', `未处理的异常：${reason && reason.message ? reason.message : String(reason)}`);
  });
  $('logs-refresh').addEventListener('click', () => loadLogs(true));
  $('logs-export').addEventListener('click', exportLogs);
  $('logs-level').addEventListener('change', () => loadLogs(true));
  $('logs-follow').addEventListener('change', () => { if ($('logs-follow').checked) loadLogs(false); else stopLogsTimer(); });
  $('logs-search').addEventListener('input', () => {
    clearTimeout(logs.searchTimer);
    logs.searchTimer = setTimeout(() => loadLogs(true), 300);
  });
  $('ps5-upgrade-all').addEventListener('click', () => { if (ps5SelectedIp) openPs5Upgrade(ps5SelectedIp, null); });
  $('ps5-upgrade-cancel').addEventListener('click', () => $('ps5-upgrade-dialog').close());
  $('ps5-upgrade-confirm').addEventListener('click', confirmPs5Upgrade);
  $('ps5-upgrade-dialog').addEventListener('close', () => {
    ps5UpgradeTarget = null;
    flushPs5Pending();
    ps5RestoreFocus();
  });
  $('repo-ps5-hint-go').addEventListener('click', (event) => goPs5Updates(event.currentTarget.dataset.ip));
  initCategoryCombo('repo-upload');
  initCategoryCombo('repo-edit');
}


/* ── 启动 ───────────────────────────────────────────────────────────── */

bindEvents();
bindPolishEvents();
loadVersion();
renderRepo();
updateRepoControls();
renderActivity();
renderPs5();
renderFiles();
updateControls();
refreshState();
