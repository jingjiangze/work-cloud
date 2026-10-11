# 多账户改造 Stage 1–3 报告

日期：2026-09-23
仓库：https://github.com/jingjiangze/work-cloud（分支 main）

## 当前 HEAD

```text
7c13ca8 test: add multi-account local drill + explicit registry path wiring
4c370e3 feat: isolate per-account runtime data (Stage 3 / Commit 03)
7f6e19a refactor: add account execution context (Stage 2 / Commit 02)
9263d70 feat: introduce multi-account registry (Stage 1 / Commit 01)
基线：e3c7245（登录页版本）
```

## 新增文件

| 文件 | 说明 |
| --- | --- |
| `models/account.py` | Account 模型（acct_xxxxxxxx 稳定主键；不含任何凭据；写入前剔除敏感键） |
| `models/account_registry.py` | 注册中心：list/get/get_by_config/add/update/enable/disable/get_or_register；跨进程 OS 文件锁（msvcrt/fcntl）+ 线程锁 + 原子写 + JSON 损坏归档重建；account_id 与 config_file 双唯一性 |
| `core/account_context.py` | AccountContext：account_id/display_name/config/user_key + state/history/risk/logs/reports/uploads/session 目录属性 |
| `tests/test_account_registry.py` | 10 项 |
| `tests/test_account_context.py` | 5 项 |
| `tests/test_runtime_isolation.py` | 5 项 |
| `tests/test_multi_account_drill.py` | 2 项（双账户演练） |

## 修改文件

| 文件 | 改动 |
| --- | --- |
| `main.py` | 导入 AccountContext；`run(config, context=None)` 显式接收上下文（日志标签优先 account_id）；`_execute_tasks_impl` 为每个任务构建 context 并提交执行，失败降级 legacy；新增 DATA_DIR/REGISTRY_PATH 常量显式传入 from_config；有 context 时状态库指向 `accounts/{id}/state`、主键切 account_id、历史写 `accounts/{id}/history`、风险经线程路由落 `accounts/{id}/risk` |
| `models/risk_ledger.py` | `set_active_risk_dir` 线程级目录路由（目录覆盖而非身份来源；显式 risk_dir 参数优先级最高） |
| `.gitignore` / `tests/__init__.py` / `core/__init__.py` | 常规配套 |

## 账户模型

```text
Account:  account_id(acct_ 稳定主键) / display_name / config_file / enabled
          + schedule_profile / task_policy / notify_profile（Stage 5/6 预留）
Registry: data/accounts/index.json（gitignore，不存 password/token/apiKey）
Context:  AccountContext.from_config(config) 按配置文件名自动注册账户
```

## 数据目录

```text
data/
  accounts/{account_id}/
    state/ history/ risk/ logs/ reports/ uploads/ session/   ← 新数据
  {date}_{user}.json  data/history/  data/risk/              ← 旧数据继续可读
```

## 兼容策略

1. registry 不存在时自动扫描 `user/*.json`（跳过 example*/_*）建表，`python main.py` 与 `python scheduled_runner.py` 零迁移可用。
2. 环境变量配置（无 `_path`）不注册账户，走 legacy 分支，行为与旧版一致。
3. 历史数据不迁移：dashboard 等旧读取路径不动；新数据全量进账户目录。
4. `context=None` 时 `run()` 所有路径与旧版完全一致（有回归测试覆盖）。

## 测试结果

```text
22 / 22 通过（unittest discover tests）
  test_account_registry    10 项：模型校验/防泄密/引导/幂等/增改查/禁启/
                           重复拒绝/损坏恢复/8 线程并发唯一/example 跳过
  test_account_context      5 项：注册联动/目录隔离/幂等复用/ENV 降级/run 签名
  test_runtime_isolation    5 项：状态/历史/风险隔离、显式参数优先、legacy 不变
  test_multi_account_drill  2 项：双账户演练 + ENV legacy 分支
```

### 双账户本地演练（模拟响应，零真实业务请求）

走真实 `_execute_tasks_impl` 装配 + `run()` 执行链，业务层 mock（ApiClient/
登录/任务函数/preflight/推送），落盘全部真实：

- A、B 两账户均执行成功，registry 自动生成 2 个 `acct_*`
- A 状态/历史/风险只出现在 A 目录，B 只出现在 B 目录（文件集不相交）
- ApiClient 每账户独立实例、token 互不相同，A session 不进入 B
- 演练中账户任务内产生的风险事件经线程路由落入正确账户 risk/ 目录

## 失败项

无。本轮所有计划验收项均通过。

## 风险项

1. **日志隔离为部分实现**：`logs/` 目录已按账户创建，但结构化文件日志仍是
   全局文件 + `acct_id|昵称` 标签前缀；真正的账户级日志文件路由建议在
   Stage 11（Dashboard 多账户控制台）一并处理。
2. **看板暂不显示新目录数据**：dashboard 仍读旧路径（Stage 11 改造点），
   新账户历史在改版前不会出现在看板上。
3. **user_key 双轨过渡**：旧状态文件主键是手机号派生值，新数据主键是
   account_id。切换当天"今日已完成"判定对新目录重新开始一次（旧目录不受
   影响）；生产部署建议选在无任务待跑的时段。
4. **重复注册依赖文件名**：同一配置文件改名会生成新 account_id（config_file
   唯一性约束会阻止同名，但不阻止改名复制），迁移期需运营侧注意。

## 下一阶段建议

按既定顺序进入 Stage 4（Commit 04: services/session_manager.py，账户级
Session 隔离，ApiClient 显式绑定 context）。当前 ApiClient 已按账户实例化
且 token 不串线（演练验证），Session 管理器落地后可持久化会话、实现
"仅失效账户重登"。随后 Stage 5（task_policy）与 Stage 7（账户锁）优先级
高于界面类改造。
