"""微信通道实现 - 企业微信(WeCom) + 通用Webhook桥接

工作流程：
  用户在微信发消息 → 微信服务器推送 → 本通道接收 → 转为统一Message → Agent处理 → Reply → 发回微信

企业微信模式：
  - 接收：微信回调POST /wechat/callback → 验签+解密 → 提取消息
  - 发送：POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=TOKEN
  - 需要在企业微信后台配置回调URL

Webhook模式：
  - 接收：POST /wechat/webhook → 通用JSON消息
  - 发送：POST webhook_url → JSON消息
  - 可对接任意桥接工具（Gewechat/WeChatBot/itchat-uos等）
"""
import asyncio
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from interface.base import Channel, Message, MessageType, Reply


class WeComCrypto:
    """企业微信消息加解密（简化版，仅做验签+解密XML提取）

    生产环境建议用官方 WXBizMsgCrypt 库，
    这里用轻量实现，覆盖大部分场景。
    """

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        # AES Key: EncodingAESKey + "=" 的 base64 解码
        import base64
        aes_key_str = encoding_aes_key + "="
        self.aes_key = base64.b64decode(aes_key_str)
        self.corp_id = corp_id

    def verify_signature(self, signature: str, timestamp: str, nonce: str,
                         encrypt: str = "") -> bool:
        """验证回调签名"""
        sort_list = sorted([self.token, timestamp, nonce, encrypt])
        sha1 = hashlib.sha1("".join(sort_list).encode()).hexdigest()
        return sha1 == signature

    def decrypt_message(self, encrypt: str) -> str:
        """解密消息密文，返回明文XML"""
        try:
            from Crypto.Cipher import AES
            import base64
            import struct

            aes_key = self.aes_key
            cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
            plain = cipher.decrypt(base64.b64decode(encrypt))

            # 去除补位
            pad = plain[-1]
            plain = plain[:-pad]

            # 去除16字节随机字符串 + 4字节msg_len + msg + corp_id
            msg_len = struct.unpack(">I", plain[16:20])[0]
            msg = plain[20:20 + msg_len].decode("utf-8")
            corp_id = plain[20 + msg_len:].decode("utf-8")

            if corp_id != self.corp_id:
                logger.error(f"企业微信解密corp_id不匹配: {corp_id} != {self.corp_id}")
                return ""

            return msg
        except ImportError:
            logger.warning("pycryptodome未安装，无法解密企业微信消息，请: pip install pycryptodome")
            return ""
        except Exception as e:
            logger.error(f"企业微信消息解密失败: {e}")
            return ""


class WeChatChannel(Channel):
    """微信通道 - 企业微信 + Webhook双模式

    用法：
        # 企业微信模式
        channel = WeChatChannel(
            mode="wecom",
            corp_id="ww1234",
            agent_id=1000002,
            secret="xxx",
            token="xxx",
            encoding_aes_key="xxx",
        )

        # Webhook模式
        channel = WeChatChannel(
            mode="webhook",
            webhook_url="http://localhost:8081/send",
        )
    """

    def __init__(
        self,
        mode: str = "wecom",
        message_handler=None,
        # WeCom 配置
        corp_id: str = "",
        agent_id: int = 0,
        secret: str = "",
        token: str = "",
        encoding_aes_key: str = "",
        # Webhook 配置
        webhook_url: str = "",
        webhook_secret: str = "",
        # FastAPI app（如果外部已创建）
        app=None,
    ):
        name = f"wechat-{mode}"
        super().__init__(name=name, message_handler=message_handler)

        self.mode = mode
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret

        # WeCom access_token 缓存
        self._access_token: str = ""
        self._token_expires: float = 0

        # WeCom 加解密
        self._crypto: Optional[WeComCrypto] = None

        # FastAPI app
        self._app = app
        self._own_app = False  # 是否自己创建的app

        # 会话映射：微信用户openid → BizBot session_id
        self._session_map: Dict[str, str] = {}

    # ==================== WeCom Access Token ====================

    async def _get_access_token(self) -> str:
        """获取企业微信 access_token（带缓存）"""
        if self._access_token and time.time() < self._token_expires:
            return self._access_token

        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corp_id}&corpsecret={self.secret}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            data = resp.json()

        if data.get("errcode") != 0:
            logger.error(f"获取access_token失败: {data}")
            return ""

        self._access_token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 7200) - 60
        logger.info("企业微信 access_token 已刷新")
        return self._access_token

    # ==================== WeCom 消息发送 ====================

    async def _wecom_send_text(self, user_id: str, content: str):
        """通过企业微信API发送文本消息"""
        token = await self._get_access_token()
        if not token:
            logger.error("无法发送消息：access_token获取失败")
            return

        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": content},
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

        if data.get("errcode") != 0:
            logger.error(f"企业微信发送消息失败: {data}")
        else:
            logger.debug(f"企业微信消息已发送给 {user_id}")

    # ==================== Webhook 消息发送 ====================

    async def _webhook_send(self, user_id: str, content: str):
        """通过Webhook发送消息"""
        if not self.webhook_url:
            logger.error("webhook_url未配置，无法发送消息")
            return

        payload = {
            "user_id": user_id,
            "content": content,
            "type": "text",
            "timestamp": int(time.time()),
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(self.webhook_url, json=payload, timeout=10)
            logger.debug(f"Webhook发送: {resp.status_code}")

    # ==================== Channel 接口实现 ====================

    async def startup(self):
        """启动微信通道"""
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse

        if self.mode == "wecom" and self.token and self.encoding_aes_key and self.corp_id:
            self._crypto = WeComCrypto(self.token, self.encoding_aes_key, self.corp_id)
            logger.info("企业微信加解密已初始化")

        if self._app is None:
            self._app = FastAPI(title="美业AI店长助手-微信通道")
            self._own_app = True

        self._register_routes()
        self.running = True

        mode_label = "企业微信" if self.mode == "wecom" else "Webhook"
        logger.info(f"微信通道已启动 [{mode_label}]")

    async def shutdown(self):
        """关闭微信通道"""
        self.running = False
        logger.info("微信通道已关闭")

    async def notify_owner(self, content: str) -> bool:
        """转人工/需人工介入时通知店主。

        wecom 模式发给 WECOM_OWNER_USERID 指定的店主企微账号；
        webhook 模式发到群 webhook。未配置时记日志降级。
        """
        import os
        if self.mode == "wecom":
            owner = os.getenv("WECOM_OWNER_USERID", "").strip()
            if not owner:
                logger.warning("需人工介入但未配置 WECOM_OWNER_USERID，仅记录日志")
                return False
            try:
                await self._wecom_send_text(owner, content)
                return True
            except Exception as e:
                logger.error(f"店主通知发送失败: {e}")
                return False
        else:
            try:
                await self._webhook_send("owner", content)
                return True
            except Exception as e:
                logger.error(f"店主通知（webhook）发送失败: {e}")
                return False

    async def send(self, session_id: str, reply: Reply):
        """发送回复到微信用户"""
        # 从 session_id 反查微信 user_id
        user_id = session_id.replace("wx_", "", 1) if session_id.startswith("wx_") else session_id

        if reply.type == MessageType.TEXT:
            if self.mode == "wecom":
                await self._wecom_send_text(user_id, reply.content)
            else:
                await self._webhook_send(user_id, reply.content)
        else:
            logger.warning(f"微信通道暂不支持发送 {reply.type} 类型消息")

    # ==================== 路由注册 ====================

    def _register_routes(self):
        """注册 FastAPI 路由"""
        app = self._app

        @app.get("/wechat/verify")
        async def wecom_verify(signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""):
            """企业微信回调URL验证（GET）"""
            if self.mode != "wecom":
                return PlainTextResponse("not wecom mode")

            if self._crypto and self._crypto.verify_signature(signature, timestamp, nonce):
                logger.info("企业微信回调URL验证通过")
                return PlainTextResponse(echostr)
            else:
                logger.warning("企业微信回调URL验证失败")
                return PlainTextResponse("verification failed")

        @app.post("/wechat/callback")
        async def wecom_callback(request_body: dict = None):
            """企业微信消息回调（POST）"""
            try:
                # 解析消息
                msg = await self._parse_wecom_message(request_body)
                if not msg:
                    return {"errcode": 0, "errmsg": "ok"}

                # 交给 Agent 处理
                reply = await self.handle(msg)

                # 发送回复
                if reply:
                    await self.send(msg.session_id, reply)

                return {"errcode": 0, "errmsg": "ok"}
            except Exception as e:
                logger.error(f"企业微信回调处理异常: {e}")
                return {"errcode": 0, "errmsg": "ok"}

        @app.post("/wechat/webhook")
        async def webhook_receive(request_body: dict = None):
            """通用Webhook消息接收"""
            try:
                msg = self._parse_webhook_message(request_body)
                if not msg:
                    return {"success": False, "error": "invalid message"}

                reply = await self.handle(msg)
                if reply:
                    await self.send(msg.session_id, reply)

                return {"success": True}
            except Exception as e:
                logger.error(f"Webhook消息处理异常: {e}")
                return {"success": False, "error": str(e)}

    # ==================== 消息解析 ====================

    async def _parse_wecom_message(self, body: dict) -> Optional[Message]:
        """解析企业微信回调消息"""
        if not body:
            return None

        encrypt = body.get("Encrypt", "")

        if encrypt and self._crypto:
            # 加密消息，需要解密
            xml_str = self._crypto.decrypt_message(encrypt)
            if not xml_str:
                return None
        else:
            # 明文消息
            xml_str = body.get("Content", "")
            if not xml_str and "xml" in str(body):
                # 尝试从XML格式解析
                xml_str = str(body)

        try:
            root = ET.fromstring(xml_str) if xml_str.startswith("<") else None
        except ET.ParseError:
            root = None

        if root is not None:
            msg_type = root.findtext("MsgType", "")
            content = root.findtext("Content", "")
            from_user = root.findtext("FromUserName", "")
            to_user = root.findtext("ToUserName", "")
        else:
            # JSON格式
            msg_type = body.get("MsgType", "text")
            content = body.get("Content", body.get("content", ""))
            from_user = body.get("FromUserName", body.get("from_user", ""))
            to_user = body.get("ToUserName", body.get("to_user", ""))

        if msg_type != "text" or not content:
            return None

        # 构建统一消息
        session_id = f"wx_{from_user}"
        self._session_map[from_user] = session_id

        return Message(
            type=MessageType.TEXT,
            content=content,
            sender_id=from_user,
            sender_name=from_user,  # 企微消息不含昵称，可用通讯录API查
            session_id=session_id,
            channel_name=self.name,
            extra={"msg_type": msg_type, "to_user": to_user},
        )

    def _parse_webhook_message(self, body: dict) -> Optional[Message]:
        """解析通用Webhook消息

        通用格式：
        {
            "user_id": "wxid_xxx",
            "user_name": "张三",
            "content": "你好",
            "type": "text",
            "group_id": "" (可选，群聊时)
        }
        """
        if not body:
            return None

        user_id = body.get("user_id", body.get("from_user", ""))
        user_name = body.get("user_name", body.get("nickname", user_id))
        content = body.get("content", body.get("text", ""))
        msg_type = body.get("type", "text")
        group_id = body.get("group_id", "")

        if not user_id or not content:
            return None

        session_id = f"wx_{group_id or user_id}"
        self._session_map[user_id] = session_id

        return Message(
            type=MessageType.TEXT if msg_type == "text" else MessageType.IMAGE,
            content=content,
            sender_id=user_id,
            sender_name=user_name,
            session_id=session_id,
            channel_name=self.name,
            extra={"group_id": group_id, "msg_type": msg_type},
        )


def create_wechat_channel_from_env() -> Optional[WeChatChannel]:
    """从环境变量创建微信通道"""
    import os

    mode = os.getenv("WECHAT_MODE", "webhook").lower()

    if mode == "wecom":
        corp_id = os.getenv("WECOM_CORP_ID", "")
        agent_id = int(os.getenv("WECOM_AGENT_ID", "0"))
        secret = os.getenv("WECOM_SECRET", "")
        token = os.getenv("WECOM_TOKEN", "")
        aes_key = os.getenv("WECOM_ENCODING_AES_KEY", "")

        if not corp_id or not secret:
            logger.warning("企业微信配置不完整，请设置 WECOM_CORP_ID 和 WECOM_SECRET")
            return None

        return WeChatChannel(
            mode="wecom",
            corp_id=corp_id,
            agent_id=agent_id,
            secret=secret,
            token=token,
            encoding_aes_key=aes_key,
        )
    else:
        webhook_url = os.getenv("WECHAT_WEBHOOK_URL", "")
        webhook_secret = os.getenv("WECHAT_WEBHOOK_SECRET", "")

        if not webhook_url:
            logger.warning("Webhook模式需要设置 WECHAT_WEBHOOK_URL")
            return None

        return WeChatChannel(
            mode="webhook",
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )
