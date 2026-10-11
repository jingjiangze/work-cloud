# VALIDATION_PLAN.md — 7 天真实运行验证计划

> Stage 11.3 产出。定义观察期（2026-09-23 ~ 2026-09-29）的检查清单、通过标准与回退策略。
> 每日记录写入 [RUNNING_REPORT.md](RUNNING_REPORT.md)；指标来自 `data/statistics.json`。

## 1. 验证范围（不修改核心业务）

| 观察对象 | 具体检查点 | 数据来源 |
|---|---|---|
| 登录（auth_checker） | 分类是否与真实原因吻合：LOGIN_OK / TIMEOUT / NETWORK / PASSWORD / CAPTCHA / SERVER；token 复用+预检路径是否正常 | `data/history/` results 的 login 消息、`logs/` |
| 调度（scheduled_runner / Actions） | 触发时刻是否落在窗口内；每日恰好 2 次；无重复触发；Actions 排队（cancel-in-progress=false）与 cron 延迟幅度 | `data/history/` started_at |
| 签到 | 上午/下午卡判定正确（hour<12）；幂等跳过生效（本地状态 vs 服务端一致）；无假成功 | 台账 + 平台 App 人工抽查 |
| 日报/周报/月报 | 日报每日触发；周报周末；月报月底；内容校验不误杀；重复检测不误判（正常内容被判重复 → 考虑把 0.90 阈值降到 0.95? 见 §4） | 台账 + `reports/history/` |
| 图片上传 | 成功 key 落盘；无图场景有 warning 记录 | `data/uploads/` |
| 通知 | 标题带 [INFO/WARNING/ERROR]；全跳过不发推送；推送失败有日志 | 每日推送 + `logs/` |
| 台账与统计 | `data/history/`、`data/statistics.json` 与实际一致 | 每日核对 |

## 2. 每日操作（约 5 分钟）

1. 看当日推送通知（有 [ERROR] 则进入 §5 排障）；
2. `python -m models.statistics` 生成/查看汇总；
3. 对照 `data/history/当日.json` 填写 `docs/RUNNING_REPORT.md` 的 Day N 表格；
4. 抽查 1 条 `logs/YYYY/MM/DD/app.log` 的 ERROR/traceback（如有）。

## 3. 通过标准（7 天后判定）

| 指标 | 标准 |
|---|---|
| 调度成功率 | ≥ 6/7 天两次窗口均正常触发 |
| 登录成功率 | ≥ 95%（失败需分类准确） |
| 签到成功率 | ≥ 95%，且**零假成功**（推送 SUCCESS 但平台无记录=不通过） |
| 报告提交 | 按各自周期 100%（该交则交），重复检测误判 ≤ 1 次 |
| 台账一致性 | 台账与推送结果一致率 100% |
| 平均耗时 | 无逐日恶化趋势 |

**全部达标 → 进入 Stage 12（Cloud Adapter Interface）；任一不达标 → 先修复再延长观察 3 天。**

## 4. 观察期内的调参规则（允许，且不改业务逻辑）

- 重复检测误判：调 `util/report_validator.py` 的 `DUPLICATE_RATIO`（当前 0.90）；
- 调度窗口不合适：改 `WORKCLOUD_SCHEDULE` 环境变量，不动代码；
- 文件日志过大：设 `WORKCLOUD_NO_FILE_LOG=1` 或定期清理 `logs/`；
- 其余一律不动，留到观察期结束。

## 5. 失败处理与回退

- 单日失败：按 OPERATIONS.md §5 排障；`data/` 状态文件可删除以强制当日重跑；
- 连续 2 天同类失败：记录到 RUNNING_REPORT 并定位为观察期阻断项；
- 需要回退代码时：`git revert` 对应 Stage 提交（每个 Stage 独立成 commit，可单独回退）。

## 6. 观察期结束后

1. 汇总 7 天 `statistics.json` 写入 RUNNING_REPORT 结论文档；
2. 达标 → 启动 Stage 12：`adapter/`（task/result/health 三适配器，统一输出格式），
   依然不改变现有业务流程；
3. 明确不做（当前阶段）：Web 后台、数据库迁移、Docker 化——理由见评审结论
   （JSON+日志已够用；环境为 Windows + GitHub Actions）。
