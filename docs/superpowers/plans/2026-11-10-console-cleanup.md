# PS5 Console Cleanup Implementation Plan

> **For agentic workers:** Use subagent-driven-development. Tasks have disjoint write scopes; Lead reviews the final integration. Execute the user-approved direction without another approval round.

**Goal:** 整理现有后台的样式、导航、筛选和反馈，并删除旧版页面。

**Architecture:** 保留原生静态三文件页面与原 API。HTML/JS 代理负责导航、搜索和反馈；CSS 代理负责设计变量、布局、组件与响应式。Lead 负责旧文件清理、文档和浏览器验证。

**Tech Stack:** 原生 HTML/CSS/JavaScript，Python unittest，Playwright 浏览器验证。

## Task 1: Lock design
- [x] Inspect web/index.html, web/app.js, web/style.css and README.
- [x] Obtain user approval for compact teal device console and legacy page cleanup.
- [x] Write .ulpi/design/DESIGN.md and console-cleanup.md with exact component IDs, flow and state contracts.

## Task 2: Navigation and filtering (HTML/JS agent)
- [x] Add meaningful browser behavioral regressions for mobile navigation / more dialog, search combinations and visible selection; observe missing behavior before implementing where tooling permits.
- [x] Reorganize existing sidebar links into groups, retain href/data-section contracts.
- [x] Add mobile-more dialog; five bottom tabs overview/ps5/files/activity/more. Match aria-current and aria-expanded; restore focus on cancel.
- [x] Add search fields activity-search, repo-search and ps5-catalog-search; filter original data without mutating it, preserve queries through polling, label no-match states.
- [x] Preserve all CSRF and destructive confirmation flows; change stale activity description to include DNS and local-service events.
- [x] Run `node --check web/app.js` (expect exit 0).

## Task 3: Visual language (CSS agent)
- [x] Apply locked tokens; flatten excessive card shadows, improve title hierarchy, desktop max width and grouped sidebar.
- [x] Style shared search fields and responsive toolbars; style more dialog and current navigation.
- [x] Make mobile file rows compact separated lists; preserve keyboard focus, >=44px touch targets, dark mode and reduced motion.
- [x] Check stylesheet syntax and existing responsive/media rules for conflicts.

## Task 4: Legacy cleanup (Lead)
- [x] Verify new repository does not reference host-service/managed/static or .local/ui-backup-20261109.
- [x] Verify resolved absolute paths, remove those exact legacy page directories, update host-service/README.md and workspace PROJECT.md to name the maintained new source.
- [x] Keep host resources, payloads, certificates, secrets, firmware and historical documentation.

## Task 5: Integration verification (Lead)
- [x] Run `.venv/bin/python -m unittest discover -s ps5-jb-server/tests -v` from workspace (expect no failures).
- [x] Run `node --check ps5-jb-server/web/app.js` and `git -C ps5-jb-server diff --check` (expect exit 0).
- [x] Serve web on loopback via managed background job; mock API responses in Playwright to avoid real device writes.
- [x] Exercise all seven routes, mobile navigation/more keyboard/selection, activity search + select all, repository search and current-host catalog search, empty/error/session states.
- [x] Capture light desktop, mobile and dark mobile views, check 320/390/768/1440 widths for overflow and all new input labels.
- [x] Request independent review, address material findings, collect jobs and stop fixture server.
- [x] Write docs/ui-cleanup-verification.md with exact evidence and remaining real-device/deployment limits; report final result without claiming deployment.
