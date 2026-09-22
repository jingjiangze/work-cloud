# OPERATIONS.md — 生产运维指南

> Stage 0~10 完成后的运行手册。对应代码基线：`main` 分支 Stage 10 提交之后。

## 1. 运行方式

| 方式 | 命令 | 说明 |
|---|---|---|
| 单次执行 | `python main.py [--file 用户文件名...]` | 一轮跑完退出，适合 cron / Actions |
| 常驻调度 | `python scheduled_runner.py [--file ...]` | 每日窗口内随机触发（默认 09:00-09:10 / 18:30-18:40） |
| GitHub Actions | 自动（4 个 cron） | 凭据放 `secrets.USER`（JSON 数组） |

## 2. 环境变量（全部可选）

| 变量 | 作用 | 默认 |
|---|---|---|
| `USER` | 用户配置 JSON 数组（Actions 场景） | 无 |
| `WORKCLOUD_SCHEDULE` | 覆盖调度窗口，如 `{"windows":[["08:00","09:30"],["17:30","19:00"]]}` | 09:00-09:10 / 18:30-18:40 |
| `WORKCLOUD_NO_FILE_LOG` | 设为 1 关闭文件日志 | 未设置（开启） |

**用户配置文件格式未做任何变更**（`user/*.json` 各字段含义同 README）。

## 3. 运行时产物（全部已 gitignore）

| 目录 | 内容 | 说明 |
|---|---|---|
| `data/{日期}_{用户}.json` | 当日各任务状态（SUCCESS/FAILED/RUNNING/SKIPPED） | **幂等依据**：当日已成功的任务直接跳过；删除该文件即强制重跑当日任务 |
| `data/history/日期.json` | 每日执行台账（用户/开始时间/耗时/各任务结果） | 事后核对"到底执行了没有"的唯一可信来源 |
| `data/uploads/` | 图片上传结果（成功 key / 失败原因） | 无图打卡时先查这里 |
| `reports/history/{用户}/` | 报告内容指纹（SHA1 + 归一化预览，无全文） | 重复内容检测依据；删除后重复检测重新学习 |
| `logs/YYYY/MM/DD/app.log` | JSON Lines 结构化日志（含完整堆栈） | 排障第一步 |

## 4. 通知分级（Stage 9）

推送标题格式：`🎉 [INFO] 工学云报告 (4/4)`

- **INFO**（✅）：全部任务成功；
- **WARNING**（⚠️）：有跳过/未知状态但无失败（如"未到提交时间"之外的部分成功）；
- **ERROR**（❌）：任一任务失败——收到即需人工介入。

**注意**：所有任务均为跳过时**不发推送**；推送渠道自身故障不会产生通知，需定期抽查日志。

## 5. 常见故障排查

| 现象 | 排查步骤 |
|---|---|
| 通知显示"登录失败：手机号或密码错误" | Stage 3 分类（LOGIN_PASSWORD_ERROR）：核对 `user/*.json` 的 phone/password |
| 通知显示"登录失败：网络不可达" | 检查本机网络/代理/DNS；Actions 场景多为 runner 出口被平台限流 |
| 通知显示"登录失败：验证码获取/识别失败" | 查 `logs/` 中 CaptchaUtils warning；连续出现说明接口或模型需更新 |
| 通知显示"报告内容校验未通过" | AI 返回内容过短/含模板占位符；检查 `config.ai.*` 配置与账户余额 |
| 打卡失败：验证码验证未通过 | 平台风控触发点选验证且识别失败，等下一窗口重试 |
| 周报/月报从未提交 | 检查 `submitTime` 必须是**整数**（字符串会导致恒不触发，Stage 1 前的老配置重点检查） |
| 怀疑重复执行 | 查 `data/history/日期.json` 台账 + `data/{日期}_{用户}.json` 状态 |

## 6. 与 autotask-platform 对接（预留）

执行历史 `data/history/*.json` 即标准化输出：日期为键、每用户一条记录、
results 数组结构稳定（task_type/status/message）。平台侧可直接：
1. 定时读取当日 JSON 渲染看板；
2. 监听 ERROR 级别条目做告警；
3. 以 duration_sec 做健康度基线。

后续可在此目录上挂文件 watch 或以只读 API 暴露，无需改动本执行器。

## 7. 已知边界（如实声明）

- `data/`、`logs/`、`reports/` 删除安全：最多丢失当日幂等记忆/历史，服务端去重仍兜底；
- 用户配置中 `customDays`/`submitTime` 等仍无 schema 强校验（类型错误静默不触发的
  问题已在 FLOW/RISK_AUDIT 记录，启动预检列入后续 Stage 1 增强）；
- 协议层加密（固定密钥 AES-ECB / MD5 签名）为平台逆向事实，本项目不可单方更改；
  `user/*.json` 必须当作高敏感物对待，禁止提交到任何仓库（.gitignore 已兜底）；
- 本工具用于考勤自动化存在平台协议与校纪层面的合规风险，使用前请阅读 README 声明。
