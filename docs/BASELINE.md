# BASELINE.md — 当前版本冻结基线

> Stage 0 产出。冻结基线：commit `740de06`（work-cloud `main`）。
> 本文档记录"当前真实状态"，后续所有 Stage 的改动以此为对照基准。

## 1. 项目定位

工学云（moguding.net）自动执行器：自动打卡（上班/下班/节假日卡）、自动提交日报/周报/月报（AI 生成内容）、多用户支持、消息推送。定位为**每日真实运行的定时任务工具**，非平台服务。

## 2. 代码结构（冻结时点）

| 路径 | 行数 | 职责 |
|---|---|---|
| `main.py` | 510 | 入口：多用户并发执行（ThreadPoolExecutor, max_workers=5）、打卡/三报任务逻辑、结果汇总推送 |
| `scheduled_runner.py` | 121 | 常驻调度器：每日 09:00 / 18:30 基础时间点 + 0~10 分钟随机偏移 |
| `coreApi/MainLogicApi.py` | 435 | 平台 API 客户端：登录、打卡、报告、上传 token、验证码触发处理 |
| `coreApi/AiServiceClient.py` | 135 | OpenAI 兼容接口生成日报/周报/月报内容（含重试、markdown 清洗） |
| `coreApi/FileUploadApi.py` | 128 | 七牛图片上传（重试、key 生成） |
| `util/CaptchaUtils.py` | 808 | 滑块验证码（OpenCV 模板匹配）+ 点选验证码（YOLOv5n + ONNX OCR） |
| `util/Config.py` | 169 | 配置加载/更新/落盘（login 后 token 会回写文件） |
| `util/CryptoUtils.py` | 95 | AES-ECB 加解密（协议密钥）、MD5 签名 |
| `util/FileUploader.py` | 119 | 本地图库随机选图 + 压缩（二分质量，≤1MB） |
| `util/HelperFunctions.py` | 183 | 节假日判断（gh-proxy 拉取 holiday-cn）、姓名脱敏、markdown 清洗 |
| `util/MessagePush.py` | 328 | 6 渠道推送（NotifyX/Server酱/PushPlus/AnPush/WxPusher/SMTP） |
| `models/` | — | `yolov5n.onnx`、`ocr.onnx`（点选验证码识别） |
| `user/` | — | 每用户一个 JSON 配置（凭据 + 行为配置），仅 `example.json`（占位符）入库 |

合计约 3031 行 Python。

## 3. 运行环境

- **Python**：>= 3.10（README 声明；GitHub Actions 固定 3.10）
- **关键依赖**（requirements.txt，**注意：该文件当前为 UTF-16 编码，`pip install -r` 会解析失败，见 RISK-A01**）：
  - `aes-pkcs5==1.0.3`（协议加解密）
  - `onnxruntime==1.20.1` + `opencv-python-headless==4.10.0.84` + `numpy==2.2.1`（验证码识别）
  - `pillow==11.1.0`（图片压缩）
  - `requests==2.32.3`
- **无锁文件**（无 poetry/uv/pip-tools），依赖仅靠 requirements.txt 钉版本。

## 4. 启动方式（3 种）

| 方式 | 命令 | 说明 |
|---|---|---|
| 单次执行 | `python main.py [--file name1 name2]` | 扫描 `user/*.json` + 环境变量 `USER`（JSON 数组），一轮跑完退出 |
| 常驻调度 | `python scheduled_runner.py [--file ...]` | 死循环，每日生成带随机偏移的计划表，分段 sleep（60s 粒度） |
| cron | `setup.sh` 注册 root crontab，按用户输入的小时/分钟执行 `main.py` | 仅 Linux/macOS；要求 root 运行 |

## 5. 定时方式

- **GitHub Actions**（`.github/workflows/main.yml`）：UTC 00:00/01:00/09:00/10:00（北京 08:00/09:00/17:00/18:00），凭据经 `secrets.USER` 注入，`concurrency.cancel-in-progress: true`，timeout 10 分钟，运行记录保留 2 次。
- **scheduled_runner**：09:00 / 18:30 + randint(0,10) 分钟；跨天重新生成；无单实例锁。
- **setup.sh cron**：用户自定小时/分钟，无随机偏移。

## 6. 配置加载方式

1. `main.execute_tasks()`：收集 `user/*.json` 文件名（可被 `--file` 过滤）+ `USER` 环境变量 JSON 数组。
2. 每个配置构造一个 `ConfigManager`（文件路径或内存字典）。
3. 文件加载时 `_apply_location_offset()` 对经纬度字符串末位做随机替换（配置为数字类型时静默跳过）。
4. `login()` 成功后 `update_config(user_info, "userInfo")` → 文件型配置会把 token **回写落盘**。
5. 无任何 schema/类型校验：键缺失返回 None（仅 warning 日志），类型错误静默产生恒 False 比较。

## 7. 关键外部依赖点（运行时网络调用）

| 目标 | 用途 | 失败影响 |
|---|---|---|
| `api.moguding.net:9000` | 全部业务接口 | 核心任务失败 |
| `up.qiniup.com` | 图片上传 | 无图打卡（静默降级为空附件） |
| OpenAI 兼容 API（用户自配） | 报告生成 | 三报任务失败 |
| `gh-proxy.com` → raw.githubusercontent | 节假日数据 | 静默降级为"仅周末判断" |
| 各推送服务 | 结果通知 | 仅丢通知，不影响任务 |

## 8. 当前已知"不修不行"的冻结结论（指向 RISK_AUDIT.md）

1. CI 依赖安装必然失败（RISK-A01）。
2. 执行状态零持久化：重复/漏执行完全依赖服务端查询 + 脆弱的序号推算（RISK-B01/B02）。
3. 异常分类粗糙：中文启发式判断是否重试；验证码二次提交结果未校验（RISK-C02/C03）。
4. 推送请求无 timeout，可挂死工作线程（RISK-C05）。

—— 以下状态即重构起点，Stage 1~10 的每一项改动都应能在本文档中找到"改动前"的对应描述。
