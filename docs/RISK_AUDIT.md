# RISK_AUDIT.md — 风险审计（稳定性导向）

> Stage 0 产出。本文档是 2026-09-22 全量代码审计的**稳定性子集**，按本阶段目标重新聚焦：
> 提高自动化任务可靠性、异常处理与状态一致性，**降低程序错误 / 异常频率 / 状态不同步导致的问题**。
> 不包含、也不作为"绕过平台检测"的依据；协议层面的既有事实（固定密钥、MD5 签名）仅作为已知约束记录。

## 等级定义

- **A（阻断级）**：导致功能完全不可用或必然出错。
- **B（状态/数据级）**：导致重复执行、漏执行、状态不同步——每日真实运行的最大风险面。
- **C（健壮性级）**：异常处理粗糙、可观测性不足，放大故障恢复时间。

## 风险清单

### A 组：阻断级

| 编号 | 风险 | 位置 | 现状与影响 | 修复阶段 |
|---|---|---|---|---|
| RISK-A01 | requirements.txt 为 UTF-16 编码 | `requirements.txt` | 文件头 `FF FE`，pip 按 UTF-8 解析直接报错 → **GitHub Actions 与 setup.sh 的依赖安装全部失败**。所有 CI 定时运行实际从未工作过 | Stage 0 已随本提交修复（Commit 01 一并重写为 UTF-8） |
| RISK-A02 | GitHub Actions `cancel-in-progress: true` | `.github/workflows/main.yml` | 相邻 cron（间隔仅 1h）若前次运行未结束会被强杀，产生"已登录未提交"的中断状态 | Stage 5 |

### B 组：状态与数据一致性

| 编号 | 风险 | 位置 | 现状与影响 | 修复阶段 |
|---|---|---|---|---|
| RISK-B01 | 执行状态零持久化 | 全局 | 无任何"今天执行到哪了"的本地记录；进程崩溃/日志丢失后无法事后核对；与计划中 Stage 1/2/10 直接对应 | Stage 1/2/10 |
| RISK-B02 | 配置类型错误静默失效 | `main.py` 周报/月报时间检查 | `weekday()+1 == submitDay` 在 submitTime 为字符串时恒 False → **周报/月报永远不提交且无任何告警** | Stage 1（配置预检） |
| RISK-B03 | 响应取值越界 | `MainLogicApi.fetch_internship_plan` / `get_weeks_date()[0]`（main.py 两处） | data 为空数组时 IndexError → 整轮"系统错误" | Stage 1 |
| RISK-B04 | 打卡幂等只看当月首条 | `get_checkin_info` + `perform_clock_in` | 记录时序变化时重复/漏判失效 | Stage 2 |
| RISK-B05 | 图片不足静默降级 | `FileUploader.upload_img` | 图库不足/目录缺失返回 "" 继续打卡，行为与配置预期不符且无告警 | Stage 7 |
| RISK-B06 | 超时重试导致重复提交 | `_post_request` | 请求已达服务端但客户端超时时，重试逻辑再次提交，无请求指纹去重 | Stage 4 |
| RISK-B07 | 周报序号推算不可靠 | `_submit_report_common` | `"第{flag+1}周"` 拼接比对（代码注释自认不准确）；flag 错位 → 重复提交 | Stage 1/2 |
| RISK-B08 | 失败无补交机制 | 三报任务 | 日报每日仅 12 点后一个窗口，失败即丢；月报仅月末一天 | Stage 5（窗口）+ Stage 10（记录后人工补） |
| RISK-B09 | 告警单通道 | `MessagePusher` | 推送是唯一失败感知渠道，且推送自身失败仅 log；配置 JSON 解析失败的用户甚至永远收不到通知 | Stage 9 |
| RISK-B10 | 调度器无单实例锁 | `scheduled_runner.py` | 双开会双倍请求；宿主重启后需人工拉起 | Stage 5 |

### C 组：健壮性与可观测性

| 编号 | 风险 | 位置 | 现状与影响 | 修复阶段 |
|---|---|---|---|---|
| RISK-C01 | 验证码失败不可归因 | `CaptchaUtils` / `MainLogicApi` | 失败仅 warning，5 次后抛通用 Exception；无法区分模型退化/接口变更/网络问题 | Stage 3/4 |
| RISK-C02 | 验证码二次提交结果未校验 | `submit_clock_in` | 带 captcha 重提交的响应被丢弃，若再次 302 则**静默假成功** | Stage 1（状态机直接解决） |
| RISK-C03 | 登录失败不分类 | `login` | 密码错误/超时/网络/服务端错误统一文案，排障靠猜 | Stage 3 |
| RISK-C04 | 重试分类靠"含中文"启发式 | `_post_request` | `re.search(r"[\u4e00-\u9fff]")` 决定是否重试；接口文案变化即错乱（业务失败被重试 5 次） | Stage 4 |
| RISK-C05 | 推送请求无 timeout | `MessagePush` 全部渠道 | TCP 挂起 → 工作线程永久阻塞 | Stage 4 |
| RISK-C06 | 日志非结构化、异常无堆栈 | 全局 | 兜底 except 只打 `str(e)`；无文件落盘；多用户并发日志仅靠 userTag 区分 | Stage 8 |
| RISK-C07 | CV 模型路径依赖 CWD + 会话重复加载 | `CaptchaUtils` | `./models/*.onnx` 相对路径，crontab 场景直接 FileNotFoundError；每次识别重建 InferenceSession | Stage 7 一并处理 |
| RISK-C08 | 依赖 CV 模块导入失败面 | `requirements`/`models` | onnx/opencv 体积大、平台敏感（pyreadline3 仅 Windows），本地与 CI 环境差异是最常见安装失败来源 | Stage 0 记录，暂不处理 |

## 已知约束（记录，不在本项目范围内修改）

1. 协议加解密为固定密钥 AES-ECB、请求签名为 MD5+公开盐——逆向自平台客户端，属协议事实，本项目单方面无法更改；其含义是凭据在传输层以下无额外保护，**配置文件必须当作高敏感物对待**。
2. `user/*.json` 明文凭据 + token 回写落盘——需要用户文档层面的强烈警示与 .gitignore 加固（`user/*.json` + example 白名单），属 Stage 10 运维文档内容。
3. `.gitignore` 当前仅排除 `user/test.json`，其余命名的真实配置会被跟踪——随 Commit 01 一并修复。

## Stage 1~10 优先级映射（结论）

1. **先修 A 组**（A01 随本次提交，A02 在 Stage 5）。
2. **Stage 1（状态机）+ Stage 2（幂等锁）是根**：B01/B02/B03/B04/B07/C02 六项同时收敛。
3. Stage 3/4（登录分类 + 网络包装）解决 C01/C03/C04/C05/B06。
4. Stage 7（图片/CV 路径）解决 B05/C07。
5. Stage 8/9（结构化日志 + 通知分级）解决 C06/B09，并为 Stage 10 执行历史提供数据底座。
6. Stage 5/10 收尾调度窗口、补交与每日台账。

## Commit 01 附带修复说明（超出纯文档的两处最小改动）

按"冻结审计"原则，本次提交仅做两处**无行为争议**的修复，其余全部留待对应 Stage：

1. `requirements.txt` 重写为 UTF-8（内容逐字节不变）——不修则一切 CI 验证无从谈起（RISK-A01）。
2. `.gitignore` 增加 `user/*.json` + `!user/example.json` 白名单——防止后续真实凭据被误提交（安全红线，不属于功能性改动）。
