# RUNNING_REPORT.md — 真实运行日报（观察期）

> Stage 11.1 产出。观察周期：**7 天**（Stage 11 Real Operation Validation）。
> 目的：验证 Stage 0~10 的加固在**真实平台行为**下是否成立——代码结构测试已通过，
> 但登录状态变化、接口返回变化、任务窗口、报告限制等只有真实运行能暴露。
>
> 记录来源：`data/history/*.json`（台账）+ `logs/YYYY/MM/DD/app.log`（明细）
> + 每日推送通知。统计汇总见 `data/statistics.json`（Stage 11.2）。

## 记录规范

每天收盘后（当日最后一次调度完成）追加一节，格式如下：

```markdown
## Day N — YYYY-MM-DD

| 项目 | 结果 | 备注 |
|---|---|---|
| 调度触发 | SUCCESS | 实际触发时刻 09:03 / 18:37（窗口内） |
| 登录 | SUCCESS | 分类: LOGIN_OK / 或失败分类 |
| 签到 | SUCCESS / SKIP / FAIL | 上午卡+下午卡 |
| 日报 | — / SUCCESS / FAIL | enabled=false 记 "—" |
| 周报 | — / SUCCESS / FAIL | 周末重点观察 |
| 月报 | — / SUCCESS / FAIL | 月底重点观察 |
| 通知 | SUCCESS / FAIL | 推送标题是否带 [LEVEL] |
| 异常 | 无 / 描述 | 摘自 app.log 的 ERROR/traceback |
| 耗时 | xx 秒 | history 的 duration_sec |
```

状态取值口径：以 `data/history/当日.json` 中各 task 的 status 为准（SUCCESS/SKIP/FAIL）；
"异常"取当日 app.log 中 `level=ERROR` 的条目摘要。

---

## 观察记录

## Day 0 — 2026-09-22（部署日，非运行日）

| 项目 | 结果 | 备注 |
|---|---|---|
| 版本冻结 | SUCCESS | `20038fa`，Stage 0~10 全部完成并推送 |
| 模块测试 | SUCCESS | 8 个模块 60 项断言全部通过（详见各 commit） |
| 真实运行 | 未开始 | 观察期 Day 1 从下一个调度窗口起算 |
| 异常 | 无 | — |

### Day 0 备注（观察期重点，摘自 Stage 11 计划）

1. **登录分类**：统计 `LOGIN_OK / LOGIN_TIMEOUT / LOGIN_NETWORK_ERROR / LOGIN_PASSWORD_ERROR`
   出现频率，确认分类与真实原因吻合（尤其"token 复用 + 预检"路径）；
2. **调度**：确认触发时刻落在窗口内、每日恰好 2 次、无重复触发；Actions 场景确认
   `cancel-in-progress: false` 后排队正常、cron 延迟幅度；
3. **三类报告**：日报每日、周报周末、月报月底——观察生成质量（长度/占位符）、
   提交成功率、**重复检测是否误判**（正常内容被判重复 → 需调低 0.90 阈值）；
4. **幂等与台账**：核对 `data/history/` 与推送结果一致；检查有无假成功（推送 SUCCESS
   但平台侧无记录）。

（Day 1 起按上方模板追加）
