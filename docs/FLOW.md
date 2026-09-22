# FLOW.md — 完整执行流程梳理（含异常点标注）

> Stage 0 产出。基于 commit `740de06` 实际代码逐步核对，非设计文档。
> 标注约定：⚠️ = 已确认的异常点/脆弱点（编号对应 RISK_AUDIT.md）。

## 总流程

```
启动 (main.py / scheduled_runner.py / cron / GitHub Actions)
  ↓
① 配置发现与加载
  ↓
② 线程池并发执行 run()（每用户一个线程, max 5）
  ↓
③ 登录 / Token 复用
  ↓
④ 实习计划获取（学生）
  ↓
⑤ 打卡任务 perform_clock_in
  ↓
⑥ 日报任务
  ↓
⑦ 周报任务
  ↓
⑧ 月报任务
  ↓
⑨ 消息推送
  ↓
结束
```

---

## ① 配置发现与加载

- **输入**：`user/` 目录文件列表；`--file` 参数过滤；`USER` 环境变量（JSON 数组）。
- **处理**：逐个构造 `ConfigManager`；文件型配置做经纬度末位偏移。
- **输出**：`List[ConfigManager]`。
- **异常点**：
  - 目录不存在 / 列目录 OSError → 记日志，继续（⚠️ 但可能 0 任务静默退出）；
  - JSON 解析失败 → 该用户被跳过，仅 error 日志，**无任何通知**（⚠️ 用户无感知）；
  - `USER` 环境变量非 JSON / 非数组 → 清空该来源（⚠️ 与 Unix 系统 `USER` 变量命名冲突，本地直接运行时该变量是登录名，会解析失败）；
  - 类型错误的配置值（customDays/submitTime 等）不在此时报错，**延迟到比较时静默恒 False**（⚠️ RISK-B02）。

## ② 并发执行框架

- **输入**：配置列表。
- **处理**：`ThreadPoolExecutor(max_workers=5)`，每个用户独立 `run()`。
- **异常点**：future 异常仅 log；无执行台账，进程崩溃后无从核对（⚠️ RISK-B01）。

## ③ 登录 / Token 复用

- **输入**：`config.user.phone/password`（AES-ECB 加密后提交）。
- **处理**：
  - 若 `userInfo.token` 已存在 → **直接复用，不验证有效性**（⚠️ 首个请求才暴露失效，多 1~2 次失败请求）；
  - 否则先取滑块验证码（get → 本地识别 → check），再 POST 登录；
  - 失败自动重登逻辑在 `_post_request` 内（检测 msg 含"token失效"，指数退避重试）。
- **输出**：`userInfo`（userId/roleKey/userType/token…），回写配置文件。
- **异常点**：
  - 验证码识别失败 5 次 → 通用 `Exception("通过滑块验证码失败")`，无法区分"模型退化"与"接口变更"（⚠️ RISK-C01）；
  - 密码错误 / 网络 / 服务端错误**全部归为同一个失败消息**（⚠️ RISK-C03 → Stage 3 待解决）；
  - token 回写落盘 = 配置文件泄露即账号接管（⚠️ 安全项，Stage 0 不处理，记录在案）。

## ④ 实习计划获取（仅学生）

- **输入**：userId + roleKey（sign）。
- **输出**：`planInfo`（planId 等），回写配置。
- **异常点**：`rsp.get("data",[{}])[0]` — data 为空数组时 **IndexError → 该用户整轮"系统错误"**（⚠️ RISK-B03）。教师身份跳过。

## ⑤ 打卡任务 perform_clock_in

- **输入**：当前时间、clockIn.mode（daily/holiday/custom）、specialClockIn。
- **处理**：
  1. 类型判定：`hour < 12 → START(上班卡)`，否则 `END(下班卡)`（⚠️ 模式硬编码，02:00 跑会被当成"下班卡"）；
  2. 休息日跳过 / 转节假日卡；
  3. 幂等检查：`get_checkin_info` 取**当月第一条**记录，type 与 createTime 比对（⚠️ 只看首条，时序变化即失效 → RISK-B04）；
  4. 随机选 description、上传图片（count=0 或图库不足 → **静默无图打卡** ⚠️ RISK-B05）；
  5. 提交打卡；若响应 `msg=="302"` → 点选验证码后**带 captcha 二次提交**。
- **输出**：`{status: success/skip/fail, details…}`。
- **异常点**：
  - **二次提交的响应不检查**：若验证码又失败，函数正常返回，日志显示"打卡成功"但实际未提交（⚠️ RISK-C02，Stage 1/2 必须先修）；
  - 客户端超时但服务端已受理时，重试会导致**重复提交**（⚠️ RISK-B06）；
  - 位置来自配置 + 末位偏移，无围栏校验。

## ⑥⑦⑧ 日报 / 周报 / 月报（_submit_report_common）

- **输入**：`config.reportSettings.{daily,weekly,monthly}.enabled`、submitTime、AI 配置、imageCount。
- **处理**：
  1. enabled=false → skip；
  2. 时间窗口检查：日报 `hour>=12`；周报 `weekday()+1 == submitDay 且 hour>=12`（⚠️ submitTime 类型错误时恒 False，**永远不提交且无告警** → RISK-B02）；月报月末第 min(submitDay, lastDay) 天；
  3. 幂等检查：
     - 日报：比对最近一条 createTime 日期；
     - 周报：`"第{flag+1}周"` 序号拼接比对（⚠️ **代码注释自认不准确**，flag 错位 → 重复提交或漏判 → RISK-B07）；
     - 月报：比对 yearmonth；
  4. AI 生成内容（3 次重试；内容为空时 raise → 当日窗口无补交 ⚠️ RISK-B08）；
  5. 上传图片；组装 `formFieldDtoList`（问卷全部填 "b" ⚠️）；提交。
- **输出**：`{status, message, report_content…}`。
- **异常点**：`get_weeks_date()[0]` 空数组 **IndexError**（⚠️ RISK-B03）；`get_submitted_reports_info` 依赖 flag 字段语义稳定。

## ⑨ 消息推送

- **输入**：任务结果列表 + `config.pushNotifications`。
- **处理**：全部 skip 则不推送；否则逐渠道推送（markdown/HTML 两种格式）。
- **异常点**：
  - 所有 `requests.post` **无 timeout** → 渠道挂起则线程永久阻塞（5 个 worker 全卡 = 整批挂死）（⚠️ RISK-C05，Stage 4 修复）；
  - 推送是**唯一告警通道**且自身失败仅 log → 推送失效 = 失败完全无感知（⚠️ RISK-B09，Stage 9 处理）；
  - details 中姓名未脱敏（打卡 details 直接放 `nikeName`），日志侧已脱敏，口径不一致。

## 异常点汇总索引（→ RISK_AUDIT.md）

| 流程节点 | 异常点 | 对应风险编号 |
|---|---|---|
| ① | 配置类型错误静默失效 | RISK-B02 |
| ③ | 登录失败不分类 | RISK-C03 |
| ④⑦ | data[0] 越界 | RISK-B03 |
| ⑤ | 二次提交结果未校验 | RISK-C02 |
| ⑤⑧ | 幂等判断脆弱（首条/序号） | RISK-B04/B07 |
| ⑤ | 超时重试重复提交 | RISK-B06 |
| ⑤⑧ | 失败无补交机制 | RISK-B08 |
| ⑨ | 推送无 timeout / 单通道告警 | RISK-C05/B09 |
| 全局 | 无执行台账与结构化日志 | RISK-B01/C06 |
