> 如果我看得更远，那是因为我站在巨人的肩膀上。 —— 艾萨克·牛顿

# AutoMoGuDingCheckIn

AutoMoGuDingCheckIn 工学云自动打卡工具，采用最新接口，安全稳定。支持多用户管理、自定义位置信息、登录状态保持、每日打卡检查、位置浮动、消息推送，免服务器运行。

本项目站在了众多开源前辈与社区贡献者的肩膀之上，在已有思路、接口探索与实践经验的基础上优化迭代，谨向所有相关开源项目与开发者致以敬意与感谢。

## 项目概述

AutoMoGuDingCheckIn 旨在：

- 自动化工学云应用中的签到过程。
- 自动提交月报、周报、日报。
- ~~适配云函数~~

## 功能列表

- [x] 自动签到
- [x] 消息推送功能
- [x] 多用户支持
- [x] 打卡位置浮动
- [x] 自动提交日报
- [x] 自动提交周报
- [x] 自动提交月报
- [x] AI 生成周、日、月报
- [x] 打卡备注以及带图打卡
- [x] Github 工作流支持
- [ ] ~~适配云函数~~

## 使用方法

### Github 工作流（免服务器部署）

参见 [Wiki](https://github.com/Rensr0/AutoMoGuDingCheckIn/wiki/Github-%E5%B7%A5%E4%BD%9C%E6%B5%81%E9%83%A8%E7%BD%B2)

**切记不要将配置文件上传到公开仓库，否则会造成信息泄露。请使用环境变量！！！，已经泄露请立刻修改工学云密码！！！**

### 本地运行

#### 环境

- Python 3.10+
- pip（Python 包管理器）

#### 安装

1. 克隆代码库：

   ```bash
   git clone https://github.com/Rensr0/AutoMoGuDingCheckIn.git
   cd AutoMoGuDingCheckIn
   ```

2. 按照下面要求添加配置文件
3. 执行（linux 系统，windows 需要自行配置计划任务程序）

   ```bash
   chmod +x setup.sh
   bash setup.sh
   ```

   按照脚本提示设置定时任务并执行

#### 配置

1. 打开 user 目录，根据下表修改 json 文件中的配置（每个文件就是一个用户）

<!-- markdownlint-disable MD033 -->
<table>
    <tr>
        <th>配置项</th>
        <th>字段</th>
        <th>说明</th>
        <th>示例</th>
    </tr>
    <tr>
        <td rowspan="2">用户信息</td>
        <td>phone</td>
        <td>工学云手机号，确保号码正确无误。</td>
        <td>13800138000</td>
    </tr>
    <tr>
        <td>password</td>
        <td>工学云密码，注意区分大小写。</td>
        <td>your_password</td>
    </tr>
    <tr>
        <td rowspan="11">打卡设置</td>
        <td>mode</td>
        <td>打卡地点的模式，`daily`：每天打上/下班卡，`holiday`：自动识别节假日并根据`specialClockIn`决定打卡类型，`custom`：自定义打卡（需配置`customDays`表示周几打卡）</td>
        <td>daily</td>
    </tr>
    <tr>
        <td>address</td>
        <td>打卡地点的详细地址，确保信息准确。</td>
        <td>四川省 · 成都市 · 高新区 · 在科创十一街附近</td>
    </tr>
    <tr>
        <td>latitude</td>
        <td>打卡地点的纬度。</td>
        <td>34.059922</td>
    </tr>
    <tr>
        <td>longitude</td>
        <td>打卡地点的经度。</td>
        <td>118.277435</td>
    </tr>
    <tr>
        <td>province</td>
        <td>所在省份。</td>
        <td>四川省</td>
    </tr>
    <tr>
        <td>city</td>
        <td>所在城市。</td>
        <td>成都市</td>
    </tr>
    <tr>
        <td>area</td>
        <td>所在区域。</td>
        <td>高新区</td>
    </tr>
    <tr>
        <td>imageCount</td>
        <td>打卡时需要上传的图片数量，默认为0。</td>
        <td>0</td>
    </tr>
    <tr>
        <td>description</td>
        <td>打卡备注</td>
        <td>列表，一个元素为一条，打卡时候随机抽取，若为空就不带备注</td>
    </tr>
    <tr>
        <td>specialClockIn</td>
        <td>打卡类型，当打卡模式为`holiday`或者`custom`有效，值设置为`true`时候将会在节假日或者不在`customDays`配置中的时候打`休息/节假日`卡</td>
        <td>false</td>
    </tr>
    <tr>
        <td>customDays</td>
        <td>自定义周几打卡（例子就是周一到周四打卡）</td>
        <td>1,2,3,4</td>
    </tr>
    <tr>
        <td rowspan="8">报告设置</td>
        <td>daily.enabled</td>
        <td>是否启用日报提交（true 或 false）。</td>
        <td>false</td>
    </tr>
    <tr>
        <td>daily.imageCount</td>
        <td>日报中需要上传的图片数量。</td>
        <td>0</td>
    </tr>
    <tr>
        <td>weekly.enabled</td>
        <td>是否启用周报提交（true 或 false）。</td>
        <td>true</td>
    </tr>
    <tr>
        <td>weekly.imageCount</td>
        <td>周报中需要上传的图片数量。</td>
        <td>0</td>
    </tr>
    <tr>
        <td>weekly.submitTime</td>
        <td>提交时间</td>
        <td>4（表示周四提交周报）</td>
    </tr>
    <tr>
        <td>monthly.enabled</td>
        <td>是否启用月报提交（true 或 false）。</td>
        <td>false</td>
    </tr>
    <tr>
        <td>monthly.imageCount</td>
        <td>月报中需要上传的图片数量。</td>
        <td>0</td>
    </tr>
    <tr>
        <td>monthly.submitTime</td>
        <td>提交时间</td>
        <td>29（每月29号提交月报如果没有29天则会在该月最后一天提交）</td>
    </tr>
    <tr>
        <td rowspan="3">AI 设置</td>
        <td>model</td>
        <td>AI 模型名称（可根据需求修改，需要支持OpenAI格式的api，目前国产模型都提供有类似API）</td>
        <td>gpt-4o-mini</td>
    </tr>
    <tr>
        <td>apikey</td>
        <td>API 密钥，确保无误。</td>
        <td>sk-osdhgosdipghpsdgjiosfvinoips</td>
    </tr>
    <tr>
        <td>apiUrl</td>
        <td>API 地址，通常为 `https://api.openai.com/`。</td>
        <td>https://api.openai.com/</td>
    </tr>
    <tr>
        <td rowspan="12">推送通知设置</td>
        <td>type</td>
        <td>推送通知的类型（如 NotifyX、Server、PushPlus 等）。</td>
        <td>Server</td>
    </tr>
    <tr>
        <td>enabled</td>
        <td>是否启用该推送通知（true 或 false）。</td>
        <td>true</td>
    </tr>
    <tr>
        <td>apiKey/sendKey/token</td>
        <td>相应的密钥或令牌。</td>
        <td>your_key</td>
    </tr>
    <tr>
        <td>description</td>
        <td>NotifyX 消息简介（可选）。</td>
        <td>简短的消息描述</td>
    </tr>
    <tr>
        <td>team</td>
        <td>NotifyX 群组 ID，用于将消息推送到指定群组（可选）。</td>
        <td>1234567890</td>
    </tr>
    <tr>
        <td>channel</td>
        <td>对于 AnPush，填写通道 ID，多个用英文逗号隔开。</td>
        <td>通道ID1,通道ID2</td>
    </tr>
    <tr>
        <td>to</td>
        <td>根据官方文档获取的接收者信息。</td>
        <td>recipient@example.com</td>
    </tr>
    <tr>
        <td>host</td>
        <td>SMTP 服务地址。</td>
        <td>smtp.example.com</td>
    </tr>
    <tr>
        <td>port</td>
        <td>SMTP 服务端口，通常为 465。</td>
        <td>465</td>
    </tr>
    <tr>
        <td>username</td>
        <td>发件人邮箱地址。</td>
        <td>sender@example.com</td>
    </tr>
    <tr>
        <td>password</td>
        <td>SMTP 密码。</td>
        <td>smtp_password</td>
    </tr>
    <tr>
        <td>from</td>
        <td>发件人名称。</td>
        <td>发件人名称</td>
    </tr>
    <tr>
        <td rowspan="5">设备信息</td>
        <td>设备信息</td>
        <td>设备信息，<a href="https://www.123pan.com/s/rlqcVv-bQOPH.html" rel="nofollow">用这个小工具获取</a></td>
        <td>{brand: TA J20, systemVersion: 17, Platform: Android, isPhysicalDevice: true, incremental: K23V10A}</td>
    </tr>
</table>

##### 示例 JSON 配置

```json
{
  "config": {
    "user": {
      "phone": "工学云手机号",
      "password": "工学云密码"
    },
    "clockIn": {
      "mode": "daily",
      "location": {
        "address": "四川省 · 成都市 · 高新区 · 在科创十一街附近",
        "latitude": "30.559922",
        "longitude": "104.093023",
        "province": "四川省",
        "city": "成都市",
        "area": "高新区"
      },
      "imageCount": 0,
      "description": ["今天天气不错", "今天天气很好", "今天天气不太好"],
      "specialClockIn": false,
      "customDays": [1, 3, 5]
    },
    "reportSettings": {
      "daily": {
        "enabled": false,
        "imageCount": 0
      },
      "weekly": {
        "enabled": true,
        "imageCount": 0,
        "submitTime": 4
      },
      "monthly": {
        "enabled": false,
        "imageCount": 0,
        "submitTime": 29
      }
    },
    "ai": {
      "model": "gpt-4o-mini",
      "apikey": "sk-osdhgosdipghpsdgjiosfvinoips",
      "apiUrl": "https://api.openai.com/"
    },
    "pushNotifications": [
      {
        "type": "NotifyX",
        "enabled": true,
        "apiKey": "your_api_key",
        "description": "简短的消息描述",
        "team": "1234567890"
      },
      {
        "type": "Server",
        "enabled": true,
        "sendKey": "your_key"
      },
      {
        "type": "PushPlus",
        "enabled": true,
        "token": "your_token"
      },
      {
        "type": "AnPush",
        "enabled": true,
        "token": "your_token",
        "channel": "通道ID,多个用英文逗号隔开",
        "to": "根据官方文档获取"
      },
      {
        "type": "WxPusher",
        "enabled": true,
        "spt": "your_spt"
      },
      {
        "type": "SMTP",
        "enabled": true,
        "host": "smtp服务地址",
        "port": 465,
        "username": "发件人邮箱",
        "password": "smtp密码",
        "from": "发件人名称",
        "to": "收件人邮箱"
      }
    ],
    "device": "{brand: TA J20, systemVersion: 17, Platform: Android, isPhysicalDevice: true, incremental: K23V10A}"
  }
}
```

##### 图片提交说明

配置对应的配置项目，将图片放到 images 目录，运行时会自动随机抽取指定数量图片提交

##### 消息推送

支持：

- [NotifyX](https://www.notifyx.cn/)
- [Server 酱](https://sct.ftqq.com/r/13600)
- [PushPlus](https://www.pushplus.plus/)
- [AnPush](https://anpush.com/)
- [WxPusher](https://wxpusher.zjiecode.com/)
- SMTP

#### 手动运行

```bash
python main.py
```

## 🆕 新功能使用指南

项目新增了两个重要功能，让您的使用体验更加便捷和智能。

### 1. 智能日志管理 📝

现在程序可以更好地追踪每个用户的操作记录，让您清楚地知道每个账号的执行情况。

#### 💡 这对您意味着什么？

- **清晰标识**：每个用户的日志都有专属标签，再也不用担心多个账号混淆
- **问题追踪**：出现问题时，可以快速定位是哪个账号出了状况
- **执行状态**：实时了解每个账号的打卡、报告提交状态

#### 📋 日志示例

当您运行程序时，会看到类似这样的日志：

```
[2025-09-03 09:15:23] main INFO [张三]: 开始执行打卡任务
[2025-09-03 09:15:24] main INFO [李四]: 开始执行打卡任务
[2025-09-03 09:15:25] main INFO [调度器]: 下一次执行时间: 2025-09-03 18:35:00
```

每行日志前面的 `[张三]`、`[李四]` 就是用户标签，让您一眼就知道是哪个账号的操作。

### 2. 自动定时执行 ⏰

**重磅功能！** 再也不用每天手动运行打卡程序了！

#### 🎯 功能亮点

- **全自动运行**：设置好后完全无需人工干预
- **智能时间**：每天自动在上班和下班时间执行打卡
- **防检测设计**：每次执行时间都有随机偏移，避免被系统识别为机器操作
- **稳定可靠**：支持长期运行，自动处理日期变更

#### 🚀 开始使用

##### 第一步：启动自动定时器

打开命令行，进入项目目录，输入：

```powershell
python scheduled_runner.py
```

这样就启动了自动定时器，程序会：
- 每天上午 9:00 左右自动打卡（会随机偏移 0-10 分钟）
- 每天下午 18:30 左右自动打卡（同样有随机偏移）

##### 第二步：指定特定用户（可选）

如果您只想为部分用户启用自动打卡，可以这样：

```powershell
# 只为 user1 和 user2 两个配置文件启用自动打卡
python scheduled_runner.py --file user1 user2
```

#### 📊 运行效果

启动后，您会看到这样的提示：

```
[2025-09-03 08:30:15] 调度器启动。基础时间点: 09:00, 18:30 (均加 0~10 分钟随机偏移)
[2025-09-03 08:30:15] 生成当日计划执行时间: 2025-09-03 09:07, 2025-09-03 18:33
[2025-09-03 08:30:15] 下一次执行时间: 2025-09-03 09:07:00 (等待 2212 秒)
```

这表示：
- 今天上午会在 9:07 执行打卡（原定 9:00 + 7分钟随机偏移）
- 今天下午会在 18:33 执行打卡（原定 18:30 + 3分钟随机偏移）

### 3. 自定义执行时间 ⚙️

默认的执行时间是上午9:00和下午18:30，但您可以根据自己的需求轻松修改。

#### 🛠️ 快速修改教程

**步骤1：打开配置文件**

使用任意文本编辑器（如记事本、VS Code）打开 `scheduled_runner.py` 文件。

**步骤2：找到配置区域**

在文件开头找到这几行：
```python
BASE_TIMES = ["09:00", "18:30"]  # 触发打卡的基础时间点
MAX_OFFSET_MINUTES = 10  # 去机器化，随机让时间点偏移 0~10 分钟
```

**步骤3：修改为您需要的时间**

根据您的上下班时间修改，例如：

```python
# 实例1：朝九晚六
BASE_TIMES = ["09:00", "18:00"]

# 实例2：朝八晚五
BASE_TIMES = ["08:00", "17:00"] 

# 实例3：上午9:30，下午5:45
BASE_TIMES = ["09:30", "17:45"]
```

**步骤4：调整随机偏移（可选）**

```python
# 减少随机偏移（更精准的时间）
MAX_OFFSET_MINUTES = 3  # 0-3分钟偏移

# 增加随机偏移（更随机的时间）
MAX_OFFSET_MINUTES = 15  # 0-15分钟偏移

# 完全固定时间（不推荐，容易被检测）
MAX_OFFSET_MINUTES = 0  # 无偏移
```

**步骤5：保存并重启**

1. 保存文件（Ctrl + S）
2. 关闭当前运行的定时器（按 Ctrl + C）
3. 重新启动：`python scheduled_runner.py`

#### 📋 配置示例

**场景1：实习生标准时间**
```python
BASE_TIMES = ["08:30", "17:30"]  # 8:30上班，17:30下班
MAX_OFFSET_MINUTES = 8  # 随机偏移8分钟内
```

**场景2：弹性工作时间**
```python
BASE_TIMES = ["09:30", "18:30"]  # 9:30上班，18:30下班
MAX_OFFSET_MINUTES = 15  # 较大随机偏移
```

#### ⚠️ 重要提醒

- **时间格式**：必须使用 24小时制，格式为 "HH:MM"
- **合理时间**：选择符合常理的上下班时间
- **适度偏移**：建议保持 3-10 分钟的随机偏移
- **测试验证**：修改后先观察一天，确认时间合适

### 4. 实用操作技巧 💡

#### 🖥️ Windows 用户：后台运行

想让程序在后台悄悄运行，不影响您使用电脑？

```powershell
# 隐藏窗口运行，完全后台执行
Start-Process python -ArgumentList "scheduled_runner.py" -WindowStyle Hidden
```

#### ⏹️ 停止自动执行

需要暂停自动打卡时，只需按 `Ctrl + C` 即可安全退出。

#### 📱 配合消息推送

建议开启消息推送功能（如Server酱、微信推送等），这样即使程序在后台运行，您也能及时收到打卡成功的通知。

### 5. 常见使用问题 ❓

#### 问：自动定时器启动失败怎么办？

**答：** 请先确认：
1. 手动运行 `python main.py` 是否正常
2. 配置文件是否正确设置
3. Python 环境是否安装完整

#### 问：时间不准确怎么办？

**答：** 检查您的电脑系统时间是否正确，程序会基于系统时间执行。

#### 问：程序会一直运行吗？

**答：** 是的，一旦启动定时器，程序会持续运行，每天自动执行打卡任务，直到您手动停止。

### 6. 升级使用建议 ⬆️

#### 🔄 从手动到自动

如果您之前一直手动运行 `python main.py`：

1. **先测试**：运行一次 `python scheduled_runner.py` 确认正常
2. **观察一天**：让程序自动运行一天，确认执行时间合适
3. **放心使用**：确认无误后就可以完全依赖自动执行了

#### ✅ 兼容性保证

- 所有现有配置继续有效，无需修改
- 仍然可以随时手动运行 `python main.py`
- 自动定时功能是额外增加的，不影响原有使用方式

---

💡 **温馨提示**：建议初次使用时先观察几天，确认执行时间和效果符合预期后再长期使用。有任何问题都可以随时停止并手动执行。

## 许可证

本项目采用 Apache 2.0 许可。详细信息请参阅 [LICENSE](https://github.com/Rensr0/AutoMoGuDingCheckIn/blob/main/LICENSE)
文件。

## 联系方式

如有任何疑问或需要支持，请通过提交 [issue](https://github.com/Rensr0/AutoMoGuDingCheckIn/issues) 与我们联系。

## 致谢

本项目基于 [Rockytkg/AutoMoGuDingCheckIn](https://github.com/Rockytkg/AutoMoGuDingCheckIn) 进行维护和更新。原作者因实习结束已停止维护，后续我会持续更新与维护，修复问题、完善功能。原作者实现的功能已非常完善，若后续接口或平台规则发生变动，我会尽可能及时跟进更新，有需求或问题欢迎提交 Issue。

