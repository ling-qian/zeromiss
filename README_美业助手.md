# 美业AI店长助手

> 基于 [BizBot](https://github.com/Auromix/bizbot) 二次开发 · MIT 协议可商用

## 这是什么

一个给美业门店老板用的AI管理助手。老板用微信跟它说话，它帮老板：
- 📋 记录每笔服务收入（谁、做了什么、多少钱、谁做的）
- 💳 管会员卡（开卡、查余额、到期提醒）
- 🛒 记产品销售、管库存
- 👨‍💼 管员工和提成
- 📊 算今日/本月收入、技师提成统计

**老板不需要学任何软件，说人话就行。**

## 快速开始

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置API Key
```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
# 获取地址: https://platform.deepseek.com/
```

### 3. 启动
```bash
python app.py
# 浏览器打开 http://localhost:8080
# 默认账号: admin / admin123
```

Windows 双击 `启动美业助手.bat` 即可。

## 接入微信

老板在微信里直接跟助手说话，不需要打开电脑。

### 方式1：企业微信（推荐，零封号风险）

1. 注册[企业微信](https://work.weixin.qq.com/)（免费）
2. 创建自建应用 → 获得 CorpID、AgentId、Secret
3. 设置接收消息 → 回调URL填 `http://你的服务器IP:8080/wechat/callback` → 获得 Token、EncodingAESKey
4. 在 `.env` 中填写：
```
WECHAT_MODE=wecom
WECOM_CORP_ID=你的企业ID
WECOM_AGENT_ID=你的应用ID
WECOM_SECRET=你的应用Secret
WECOM_TOKEN=你的回调Token
WECOM_ENCODING_AES_KEY=你的EncodingAESKey
```

### 方式2：Webhook桥接（适合测试）

可对接任意微信桥接工具（Gewechat、WeChatBot等）：
```
WECHAT_MODE=webhook
WECHAT_WEBHOOK_URL=http://localhost:9999/send
```

发消息到助手：`POST http://localhost:8080/wechat/webhook`
```json
{"user_id": "wxid_xxx", "user_name": "张老板", "content": "今天营业额多少", "type": "text"}
```

## 打包成 exe

```bash
pip install pyinstaller
pyinstaller salon_assistant.spec
# 产物在 dist/美业AI店长助手/
```

## 改造成本说明

| 改造项 | 状态 | 工作量 |
|--------|------|--------|
| HairSalonConfig 美业配置 | ✅ 已完成 | 半天 |
| DeepSeek LLM 接入 | ✅ 已完成 | 2小时 |
| .env 配置模板 | ✅ 已完成 | 半小时 |
| PyInstaller 打包配置 | ✅ 已完成 | 1小时 |
| Windows 启动脚本 | ✅ 已完成 | 半小时 |
| 微信/企微接入 | ✅ 已完成 | 1天 |
| 知识库(64条FAQ+SOP) | ✅ 已完成 | 1天 |
| Web界面汉化打磨 | ⏳ 待做 | 半天 |

## 项目结构

```
bizbot/
├── config/
│   ├── hair_salon_config.py  ← 美业业务配置（服务/价格/员工/会员卡）
│   ├── business_config.py    ← 配置切换入口（已切到HairSalon）
│   ├── business_functions.py ← 30+业务函数（记录/会员/统计...）
│   ├── register_functions.py ← 函数注册表
│   ├── settings.py           ← 全局配置（支持DeepSeek/MiniMax/OpenAI）
│   └── prompts.py            ← 提示词（从config动态生成）
├── agent/                    ← AI Agent核心（对话+函数调用）
├── database/                 ← 数据层（10张表，SQLite）
├── interface/                ← Web仪表盘+聊天界面
├── app.py                    ← 入口
├── salon_assistant.spec      ← PyInstaller打包配置
└── 启动美业助手.bat           ← Windows一键启动
```

## 美业服务清单

| 服务 | 默认价格 | 分类 |
|------|----------|------|
| 男士剪发 | ¥38 | 剪发 |
| 女士剪发 | ¥58 | 剪发 |
| 儿童剪发 | ¥28 | 剪发 |
| 烫发 | ¥198 | 烫发 |
| 染发 | ¥168 | 染发 |
| 洗发吹风 | ¥28 | 洗护 |
| 头皮护理 | ¥128 | 护理 |
| 头发护理 | ¥158 | 护理 |
| 接发 | ¥398 | 接发 |
| 基础美甲 | ¥68 | 美甲 |
| Gel甲油胶 | ¥98 | 美甲 |
| 美甲彩绘 | ¥128 | 美甲 |
| 剪+烫套餐 | ¥238 | 套餐 |
| 剪+染套餐 | ¥198 | 套餐 |

以上全部可在 `hair_salon_config.py` 中自定义修改。

## 定价建议

| 版本 | 价格 | 内容 |
|------|------|------|
| 基础版 | ¥599 买断 | exe软件+SQLite本地数据 |
| 标准版 | ¥999 买断 | +微信接入+1年技术支持 |
| 高级版 | ¥1999 买断 | +企微接入+数据看板+定制配置 |

## License

MIT — 基于原 BizBot 项目，可自由商用、修改、转售。
