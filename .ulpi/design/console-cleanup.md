# 管理后台整理规格

Every screen must read as the same product if placed side by side.

设计语言绑定 DESIGN.md。2026-11-10 用户已批准：统一样式、重整导航和操作流程、补齐筛选与反馈，删除旧版页面。此文将已批准方向具体化，直接进入实施。

## Scope
只修改新版 web 的界面组织与前端交互，不修改 DNS、nftables、鉴权、插件执行或文件代理 API。旧页面清理仅为工作区 host-service/managed/static 与 .local/ui-backup-20261109；保留宿主资源、插件二进制、ESP32 工具链、历史验证材料。

## Journeys and navigation
- 桌面：概览单项；“主机管理”下 PS5 主机、PS5 文件；“网络管理”下访问策略、网络活动；“系统”下插件仓库、设置。保留原 href 和 data-section 兼容入口。
- 手机底部固定 5 项：概览、主机、文件、活动、更多。更多为原生 modal dialog，列出访问策略、插件仓库、设置；当前处于这三页之一时更多为当前项。活动已有直达，无须重复。
- more trigger: #mobile-more-trigger.tab-item, aria-haspopup=dialog, aria-expanded, aria-controls=mobile-more-dialog。
- more dialog: #mobile-more-dialog.dialog.mobile-more-dialog；链接 .more-link[data-section]，关闭 #mobile-more-close；选中页 aria-current=page。进入链接关闭面板；Escape 关闭、焦点回触发器；Tab 限于弹层。登录失效时面板与业务弹层一并关闭。
- 概览快捷入口延续主机、文件、策略、活动；去除重复营销说明，服务状态与操作入口优先。

## Search and data states
- 网络活动：#activity-search type=search，通过域名、IP、客户端、端口、标签、类别和事件来源搜索。与现有允许/未放行筛选组合；全选仅选当前可见可选行；筛选隐藏的已选项目须明确处理，推荐移除隐藏选择，防止意外批量授权。#activity-search-summary aria-live=polite，报告匹配数量，空结果不误报无网络活动。
- 插件仓库：#repo-search type=search，匹配名称、文件名、版本、作者、分类与描述；只影响显示，不改变原始总数、上传/编辑/删除目标。#repo-search-summary 报告匹配数。
- 主机插件：#ps5-catalog-search type=search，匹配当前已存在插件目录的名称、文件名、版本和描述；同现有过滤兼容。无主机或加载中时保持已有提示。
- 搜索使用大小写不敏感、trim 后的子串匹配，不解释为正则，不插入未经转义的 HTML。input 变化立即反馈；轮询时保留当前筛选。原始无内容、无搜索匹配、加载和请求失败必须分别显示。

## Component interface for parallel implementation
- .nav-group 为导航分组，.nav-group-label 为说明标签；平板收起标签，导航仍有 aria-label/title。
- .search-field 包裹搜索图标、可访问标签和 input；.search-toolbar 容纳筛选与搜索，窄屏堆叠。
- .search-summary 为文本计数和无结果反馈；使用 muted 文字，不能仅靠颜色。
- .mobile-more-nav 容纳 .more-link，一列分隔列表；每行触控目标 48px。
- 保留所有现有 ID、业务 data 属性、表单行为及确认弹层。不要重写业务状态机。

## Responsive and accessibility
桌面 >=1024、平板 768–1023、手机 <=767。所有 7 页面都可达；320/390px 不出现页面横向溢出；长域名、IP、文件名可换行。数据表桌面按页面滚动，必要时只横向滚动；手机文件用紧凑列表，避免每行一个悬浮卡片。
所有新 input 有显式或 visually-hidden label，搜索计数用 polite live region。当前导航 aria-current，菜单触发 aria-expanded；返回/刷新保留 hash 行为。减少动画与深色模式沿用现有媒体查询。
会话失效走现有 showLogin；写入请求保持 CSRF，禁止搜索或导航触发写入。离线或 API 失败保留现有错误提示和手动刷新。

## Validation and handoff
工程实现拆为两个互不重叠的范围：界面工程代理负责 web/index.html + web/app.js；样式工程代理负责 web/style.css。Lead 清理旧目录、文档并验证。Implement exactly this spec. Theme the existing native design system with our locked tokens; do NOT redesign or re-implement its components.
验收：手机直接切换主机、文件、活动及更多；更多关闭与焦点恢复正确；网络/仓库/主机插件搜索有命中及无结果状态，批量选择无隐藏授权；现有后端单测全部通过、JS 语法检查通过；明暗、桌面/手机截图与浏览器行为检查完成。

## Design pre-flight
身份一致、单一强调色与半径规范、无外部资源；无装饰渐变、嵌套阴影卡、营销大标题；三个导航组降低一次决策量；每页主操作突出。原生组件和本机字体为用户批准的现有栈约束。已规格化加载/空/搜索无结果/失败/登录失效与键盘路径，色彩数值在实现验证时计算。自评：辨识度3、层级3、一致性4、无障碍3、状态3、文案3、克制4、动效3，总26/32。改进：把手机缺失入口变为固定标签，将重复卡片收成分隔列表，以降低导航与阅读负担。
