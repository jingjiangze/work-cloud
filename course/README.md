# 知到/智慧树自动化 · 自愈链路与验证工具

本目录存放**主项目之外的叠加层**：登录自愈、课程切换、滑块验证研究、以及排查用的验证工具。
主程序本体（`fuckCourse`、`zhexam`）仍在服务器上，不在本仓库内。

## 为什么需要这层

2026-10-29 ~ 11-03，知到侧接口连续 6 天 `401 {"code":401,"message":"Forbidden"}`，
而每日定时任务静默 `exit 1`，没有任何通知。排查后确认：

1. **401 不是 cookie 过期那么简单**：用真浏览器带着现有 cookie 打开看课页，
   **官方客户端自己也收到 401**，随后主动调 `/gateway/f/v1/login/logout` 跳回登录页。
   ⇒ 会话本身已死，改请求头 / 换出口 IP / 补 AJAX 标记全部无效。
2. **真正导致「每日任务不执行」的是代码 bug**：`zhs_daily_v2.py` 里用了未定义变量 `NL`，
   每次走到「登录态异常」分支就 `NameError` 崩溃，考试/作业/章节测试/问答全都不执行。
   修复脚本见 `fix_nl.py`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `auto_login.py` | **三级登录自愈**：L1 cookies 复验（判据是 `getHikeList()` 能否拉到课程，不是页面能否打开）→ L2 账号密码（真浏览器）→ L3 扫码兜底（二维码挂公网 + PushPlus 推送 + 轮询）。任一级成功即返回，全失败推一条明确通知 |
| `pwd_login_browser.py` | 账号密码登录（真浏览器 CAS 表单）：填表 → 勾协议 → 提交 → 可选图片验证码 OCR → **再次 `getHikeList()` 复验** → 落 cookies，带退避重试 |
| `switch_courses.py` | 把课程列表在「知到 ID」与「hike 数字 ID」之间切换（带备份，旧 ID 存 `disabled_course_list`）。`fuckCourse` 按「ID 含字母⇒知到 / 纯数字⇒hike」分流 |
| `probe_with_headers.py` | 把真实客户端的请求头喂进来，验证 `/gateway/t/**` 的 401 会不会消失（不保存、不打印头的值） |
| `fix_nl.py` | 修 `zhs_daily_v2.py` 的 `NL` 未定义变量 |
| `diag_drag.py` | 滑块拖拽诊断：读 `getBoundingClientRect()` 判断事件是否真的让手柄/拼图移动 |
| `solve_closed_loop.py` | 易盾滑块**闭环**解法：按住不放、读拼图实际 x、按误差微调到缺口再松手 |
| `solve_xdotool.py` | 同上，但改用 `Xvfb` + `xdotool` 产生**操作系统级**鼠标事件 |
| `mini_proxy.py` | 最小 HTTP CONNECT 代理（仅放行单一出口 IP），用于跨机出网 |

## 易盾滑块的关键实测参数（省得重新摸索）

- 组件在主页面内（非 iframe）：`img.yidun_bg-img` 自然 480×240 / CSS 400×200 ⇒ **缩放 0.8333**；
  `img.yidun_jigsaw` 自然 91×240，透明 bbox `x:[4,88] y:[44,128]`（85×85 的块 + 4px 左 padding）。
- **手柄位移 ≠ 拼图位移**：实测拖 100px 手柄走 100、拼图只走 82；拖 200 → 拼图 182。
  即 `拼图 = 手柄 − 17px` 的**固定偏移**（不是比例）。按 1:1 拖永远差 ~17px。
- 匹配判据优先级：piece 所在**水平带的真实像素**做 `TM_CCOEFF`（0.6~0.74）> 整条灰度 Canny（0.25~0.36）
  > alpha 形状轮廓（0.11~0.22）。背景左侧**不含** piece，不存在自匹配问题。
- `Input.dispatchMouseEvent` 的 `mouseMoved` **必须带 `buttons: 1`**，否则浏览器当悬停、滑块不动。
- 触摸路线需先 `Emulation.setTouchEmulationEnabled`，否则页面 `ontouchstart` 不存在、组件只绑鼠标。
- **闭环能把拼图精确落到缺口（误差 0.0px），但 Yidun 仍拒** ⇒ 拦的是事件流行为特征，不是位置。
  已排除：`navigator.webdriver`（已抹为 false）、触摸/鼠标两种事件类型、轨迹缓动与过冲。

## 依赖

- Python 3：`opencv-python-headless`、`numpy`、`websocket-client`（滑块相关）；`requests`（其余）
- 浏览器：`chromium`（Playwright 自带的或系统包均可）
- 系统级输入（`solve_xdotool.py`）：`Xvfb`、`xdotool`
