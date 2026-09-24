# ZeroMiss · 零漏单

> 美业门店的 AI 店长助手 —— 3 秒回客、自动预约、**不漏一单**。
> *Catch every customer. Miss none.*

基于 [Auromix/bizbot](https://github.com/Auromix/bizbot)（MIT）二次开发。

## 为什么存在

美业老板娘的微信就是生意本身，但人只有一双手：

- 晚上 10 点客人问"明天有空吗"，回晚了，客人已经在别家约好
- 做着手艺，每 5 分钟一条"还能预约吗"，忙得水都喝不上
- 两个客人约到同一时段，到店才撞单

ZeroMiss 做一件事：**把你已经花钱买来的客人接住**。它不制造客流，它决定你的客流钱漏不漏。

## 核心能力

| 能力 | 说明 |
|---|---|
| 🤖 微信自动接待 | 走企业微信「微信客服」**官方 API**（腾讯唯一认可的自动化通道，拒绝外挂封号红线），夜里有单夜里接 |
| 📅 智能预约 | 理解"明天下午三点能做头发吗"这类人话，自动登记防撞单 |
| 👥 会员管理 | 开卡、查余额、消费提醒，说人话就行 |
| 📊 店务看板 | 收入、提成、库存、流水，网页一屏看完 |
| 🔀 分级授权 | 回价格 AI 说了算；承诺折扣、改约时间必须店主确认；转人工后 AI 自动静默 |
| 🧠 行业知识库 | 门店 FAQ/SOP 可视化导入（`tools/`），AI 答案带店里自己的规矩 |

## 快速开始

### 店主（下载即用）

到 [Releases](https://github.com/ling-qian/zeromiss/releases) 下载对应系统的包：

- Windows：`BizBot-windows.zip` → 解压双击
- macOS：`BizBot-macos.dmg` → 首次打开若被拦，系统设置 → 隐私与安全性 → 仍要打开（放行一次，终身有效）

配置好企微「微信客服」API 后即上线自动接待（手册见 `deploy/部署手册.md`）。

### 开发者

```bash
pip install -r requirements.txt
python3 app.py --port 8080 --username boss --password yourpass
```

## 工程质量

- **345 项自动化测试**（功能边界/回复守卫/会话隔离/备份/历史裁剪/增值场景）
- 双平台打包流水线内置冒烟验证（产物启动后 API 就绪才算构建成功）
- 会话隔离：每个顾客独立上下文，零串扰
- 全链路消息存档：AI 替你回的每一句，店主看板可查

## 项目结构

```
app.py                    # 服务入口（Web 管理台 + 微信客服通道）
agent/                    # LLM Agent 核心
interface/
  web/                    # 管理台（FastAPI）
  wechat/kf_channel.py    # 微信客服通道（sync_msg 拉取 + 分级授权 + 轮询兜底）
config/knowledge_base.py  # 行业知识库引擎
tools/                    # 门店知识库录入三件套
deploy/                   # 双平台打包 + 云部署（可选升级）
```

## License

代码遵循上游 MIT 协议。行业知识库数据不随仓库分发。

> ZeroMiss = 按效果付费的具象化：我们收费的依据，就是那个「零」。
