# 多账户架构与运维（Stage 1–13 汇总）

本文是多账户改造（Stage 1–13）的权威文档：架构、数据目录、调度、
看板、管理 API、安全模型与运维手册。旧文档（`MULTI_ACCOUNT_STAGE_1_3_REPORT.md`、
`OPERATIONS.md` 等）保留作历史参考，与本文冲突处以本文为准。

## 1. 总体架构

```
scheduled_runner.py（调度器，单实例锁 run.lock.scheduler）
  └─ 到点按账户触发 execute_tasks([配置文件名])
       └─ LocalRunLock（全局执行锁 data/run.lock）
            └─ run(config, context)   ← 每账户一个 AccountContext
                 ├─ 账户运行锁 AccountRunLock（A+A 拦截 / A+B 并行）
                 ├─ 任务决策链（registry enabled → task_policy → 配置旗标）
                 ├─ 打卡 / 日报 / 周报 / 月报（验证 + 台账 + 风险事件）
                 └─ 每账户独立消息推送
  └─ 当日计划全部完成 → 跨账户聚合摘要（WK_DIGEST_PUSH 开启时）
```

- 账户身份：`account_id`（`acct_` + 随机串），注册表 `data/accounts/index.json`
  首轮执行时从 `user/*.json` 自动引导建表。
- 上下文：`core/account_context.py` 的 `AccountContext` 显式传递
  （state/ledger/history/risk/reports 目录），**禁止**依赖全局变量判账号。
- 兼容：registry 为空或 `--file` 显式指定时回退旧全局窗口调度（legacy）。

## 2. 数据目录（全部只增不删，敏感正文不入库）

```
data/
├─ accounts/
│  ├─ index.json                    # 注册表（无密码等敏感字段）
│  └─ acct_xxx/
│     ├─ state/                     # 任务状态快照
│     ├─ ledger/YYYY-MM-DD.json     # 执行台账（run_id/任务粒度/验证）
│     ├─ history/                   # 每日执行历史
│     ├─ risk/                      # 风险事件
│     ├─ reports/YYYY-MM-DD/{day|week|month}/  # 报告元数据（无正文）
│     └─ session/                   # 会话隔离（内存态的持久部分）
├─ audit/YYYY-MM-DD.json            # 看板管理动作审计
├─ run.lock / run.lock.scheduler    # 全局执行锁 / 调度器锁
```

## 3. 调度

- 每账户按 `schedule_profile`（注册表）或全局窗口（env
  `WK_SCHEDULE_*` → 默认 12:30-12:40 / 17:30-17:40）生成当日随机触发时刻。
- 随机偏移仅用于错开本地任务同时启动。
- 计划任务：`WorkCloudScheduler`（pythonw scheduled_runner.py）、
  `WorkCloudDashboard`（pythonw dashboard/app.py）。
- 注意：wmic/tasklist 下 venv `pythonw.exe` 显示两条同命令行属正常
  （venv 启动器 shim + 真实解释器父子进程，用 PPID 区分）。

## 4. 本地看板（127.0.0.1:8792）

- 只读：`/api/accounts`（总览）、`/api/accounts/{id}`（详情：任务开关/
  调度窗口/7 天轮次/错误/风险/报告元数据）。
- 受控写（`POST /api/accounts/{id}/action`）：`enable` / `disable` /
  `set_task_policy` / `run`（独立进程拉起执行）。**必须登录 +
  X-CSRF-Token**（与会话绑定），全部动作落 `data/audit/`。
- `run` 与计划任务共用单实例执行锁：执行中被触发会自动退出并记风险
  事件，不会产生双重登录。

## 5. 安全模型（要点）

- 密码：`dashboard/password.txt` 明文可换，服务端只存 SHA-256；
  登录失败线性退避（封顶 5s）。
- 会话：HttpOnly Cookie + SameSite=Lax，12h TTL；CSRF 令牌绑定会话。
- 绑定 127.0.0.1，公网经 Cloudflare Tunnel；管理动作全程审计。
- 敏感信息（正文/图片/密码/token）不进任何台账/看板/摘要。

## 6. 跨账户通知聚合（Stage 13）

- 默认关闭。开启：设置环境变量 `WK_DIGEST_PUSH`（JSON 数组，结构与
  各账户配置的 `config.pushNotifications` 相同）。
- 时机：调度器发现当日计划全部完成时，聚合 `data/accounts/*/ledger/`
  当日台账推送一条总览（每天至多一次；无执行记录不推）。
- 等级：有失败账户 → ERROR；有未知/无记录 → WARNING；否则 INFO。
- 每账户每轮的独立推送行为保持不变。

## 7. 运维手册

- **改密码**：编辑 `dashboard/password.txt` 后无需重启（启动时读取，
  重启生效更稳妥）。
- **手动跑某账户**：看板账户详情 → ▶ 立即运行；或
  `python main.py --file 配置文件名(不带后缀)`。
- **禁用/启用账户**：看板操作按钮；或改注册表 `enabled` 字段。
- **排障**：pythonw 静默崩溃时用 `.venv/Scripts/python.exe` 前台跑同
  命令捕获 traceback；看板健康检查 `GET /health`。
- **测试**：`python -m unittest discover tests`（89+ 项）。
- **部署同步**：代码目录（models/ core/ coreApi/ services/ util/
  dashboard/ main.py scheduled_runner.py）**必须整包同步**，勿只拷
  单个改动文件（生产曾因缺新模块静默崩溃）；`data/` 与 `user/` 不覆盖。

## 8. 阶段索引

| Stage | 主题 | 关键交付 |
|---|---|---|
| 1–3 | 身份与隔离 | Registry / AccountContext / 目录隔离 |
| 4–5 | 会话与决策 | Session 隔离 / 任务开关三级决策链 |
| 6–7 | 调度与并发 | 账户错峰调度 / 全局+账户双运行锁 |
| 8–9 | 可观测 | run_id 台账 / 提交结果验证 |
| 10 | 报告生命周期 | 真实日期周期区间（弃 flag+1 串匹配）/ 报告元数据 |
| 11 | 多账户看板 | 账户总览 + 详情单页 |
| 12 | 管理 API | CSRF + 审计的受控写操作 |
| 13 | 收尾 | 跨账户通知聚合 / 本文档 / 回归演练 |
