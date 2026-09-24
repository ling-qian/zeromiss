"""微信通道 - 企业微信(WeCom) + 通用Webhook桥接

支持两种接入模式：
1. 企业微信(WeCom) — 官方API，零封号风险，推荐正式交付
2. 通用Webhook — 可对接任意微信桥接工具（Gewechat/WeChatBot等）

配置项（.env 或环境变量）：
  WECHAT_MODE=wecom|webhook   # 接入模式
  # 企业微信配置
  WECOM_CORP_ID=              # 企业ID
  WECOM_AGENT_ID=             # 应用AgentId
  WECOM_SECRET=               # 应用Secret
  WECOM_TOKEN=                # 回调Token
  WECOM_ENCODING_AES_KEY=     # 回调EncodingAESKey
  # Webhook配置
  WECHAT_WEBHOOK_URL=         # 消息发送Webhook URL
  WECHAT_WEBHOOK_SECRET=      # Webhook签名密钥(可选)
"""
