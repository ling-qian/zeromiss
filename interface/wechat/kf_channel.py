"""微信客服通道（企业微信「微信客服」官方 API）。

这是 BizBot 的合规对外接待通道：顾客在微信里通过客服链接咨询，
消息经企微微信客服服务器 → 本通道拉取 → AI 店长处理 → 回复顾客。

为什么走这个通道（2026-09 合规侦察结论）：
    个人微信自动回复 = 外挂 = 封号红线；
    「微信客服」是腾讯官方唯一认可的自动化接待通道。

协议要点（官方文档口径）：
    - access_token: GET /cgi-bin/gettoken?corpid=&corpsecret=（secret 为微信客服应用 Secret）
    - 收消息: POST /cgi-bin/kf/sync_msg  {cursor, token, limit, open_kfid} → msg_list + next_cursor
        origin: 3=顾客消息（我们只处理这个）；msgtype=event 为事件（enter_session 带欢迎语 code）
    - 发消息: POST /cgi-bin/kf/send_msg  {touser, open_kfid, msgtype, text:{content}, [welcome_code]}
    - 回调验证: GET 带 msg_signature/timestamp/nonce/echostr，解密后原样返回明文
    - 回调事件: POST 加密 XML（Encrypt 字段）——仅作为"立即拉取"触发器，消息本体以 sync_msg 为准

设计原则（借鉴 Meta Muse，B1 SOP 第五节）：
    1. 活动记录透明: 所有顾客消息与 AI 回复经由 message_handler 存档，店主看板可见
    2. 分级授权: needs_human 会话进入 human_hold（默认30分钟暂停自动回复），AI 与店主不打架
    3. 可靠性: 回调触发拉取 + 定时轮询双保险，回调丢失不影响收消息
"""
from __future__ import annotations

import asyncio
import json
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from interface.wechat.channel import WeComCrypto

QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"

# 顾客消息来源标记（sync_msg 返回的 origin 字段）
ORIGIN_CUSTOMER = 3


class WeChatKfChannel:
    """微信客服通道：拉取式收消息 + API 发消息。

    与 WeChatChannel（应用消息/webhook，面向企业成员）互补：
    本通道面向外部顾客，走「微信客服」账号。
    """

    def __init__(
        self,
        message_handler,
        corp_id: str,
        secret: str,
        token: str,
        encoding_aes_key: str,
        open_kfid: str,
        app=None,
        poll_interval: int = 20,
        human_hold_minutes: int = 30,
        cursor_path: str = "data/wecom_kf_cursor.txt",
    ):
        self.name = "wechat-kf"
        self.message_handler = message_handler
        self.corp_id = corp_id
        self.secret = secret
        self.open_kfid = open_kfid
        self.poll_interval = max(5, poll_interval)
        self.human_hold_seconds = human_hold_minutes * 60

        self._crypto = WeComCrypto(token, encoding_aes_key, corp_id)
        self._app = app
        self._own_app = False

        # access_token 缓存
        self._access_token: str = ""
        self._token_expires: float = 0

        # sync_msg 游标（断电续传：持久化到 data/）
        self._cursor: str = ""
        self._cursor_path = Path(cursor_path)

        # MsgId 排重（LRU，防止企微重试导致重复回复）
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_max = 2000

        # 分级授权：session → 恢复自动回复的时间戳
        self._human_hold: Dict[str, float] = {}

        self._poll_task: Optional[asyncio.Task] = None
        self._client = httpx.AsyncClient(timeout=15)

    # ==================== access_token ====================

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires:
            return self._access_token
        url = f"{QYAPI}/gettoken?corpid={self.corp_id}&corpsecret={self.secret}"
        resp = await self._client.get(url)
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"gettoken失败: {data}")
        self._access_token = data["access_token"]
        # 官方有效期7200s，提前5分钟刷新
        self._token_expires = time.time() + int(data.get("expires_in", 7200)) - 300
        return self._access_token

    # ==================== 收消息（sync_msg 拉取） ====================

    def _load_cursor(self) -> None:
        try:
            if self._cursor_path.exists():
                self._cursor = self._cursor_path.read_text(encoding="utf-8").strip()
                if self._cursor:
                    logger.info(f"微信客服: 已恢复拉取游标 ({len(self._cursor)}字符)")
        except Exception as e:
            logger.warning(f"微信客服: 读取游标失败（从零开始拉取）: {e}")

    def _save_cursor(self, cursor: str) -> None:
        self._cursor = cursor
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(cursor, encoding="utf-8")
        except Exception as e:
            logger.warning(f"微信客服: 持久化游标失败: {e}")

    def _dedup(self, msgid: str) -> bool:
        """True=首次出现（应处理），False=重复（跳过）。"""
        if msgid in self._seen:
            self._seen.move_to_end(msgid)
            return False
        self._seen[msgid] = None
        if len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)
        return True

    def set_human_hold(self, session_id: str, minutes: Optional[int] = None) -> None:
        """转人工后暂停该会话的自动回复（分级授权）。

        店主在企微里亲自回复顾客期间，AI 静默，避免两边同时说话。
        超时后自动恢复 AI 接待。
        """
        hold_seconds = (minutes or 30) * 60
        self._human_hold[session_id] = time.time() + hold_seconds
        logger.info(f"微信客服: 会话 {session_id} 进入人工接管，{minutes or 30} 分钟后恢复 AI")

    def _on_hold(self, session_id: str) -> bool:
        until = self._human_hold.get(session_id)
        if until is None:
            return False
        if time.time() >= until:
            self._human_hold.pop(session_id, None)
            logger.info(f"微信客服: 会话 {session_id} 人工接管结束，恢复 AI 接待")
            return False
        return True

    async def _sync_and_reply(self) -> int:
        """拉取一轮消息并处理。返回本轮处理的顾客消息数。"""
        token = await self._get_access_token()
        url = f"{QYAPI}/kf/sync_msg?access_token={token}"
        payload: Dict[str, Any] = {
            "cursor": self._cursor,
            "limit": 1000,
            "open_kfid": self.open_kfid,
            "voice_format": 0,
        }
        resp = await self._client.post(url, json=payload)
        data = resp.json()
        if data.get("errcode"):
            # token 过期等错误：清缓存，下轮重试
            if data.get("errcode") in (40014, 42001):
                self._access_token = ""
                self._token_expires = 0
            raise RuntimeError(f"sync_msg失败: {data}")

        msg_list = data.get("msg_list") or []
        handled = 0
        for msg in msg_list:
            msgid = msg.get("msgid", "")
            if not self._dedup(msgid):
                continue

            origin = msg.get("origin")
            msgtype = msg.get("msgtype")

            # 顾客文本消息 → AI 接待
            if origin == ORIGIN_CUSTOMER and msgtype == "text":
                await self._handle_customer_text(msg)
                handled += 1

            # 事件消息：进会话 → 发欢迎语
            elif msgtype == "event":
                await self._handle_event(msg)

            # 其他消息类型（图片/语音等）：第一版提示顾客转文字
            elif origin == ORIGIN_CUSTOMER:
                await self._send_text(
                    msg.get("external_userid", ""),
                    "收到啦～图片语音我还在学，麻烦用文字跟我说，看得更明白 🙏",
                )

        self._save_cursor(data.get("next_cursor", ""))
        return handled

    async def _handle_customer_text(self, msg: dict) -> None:
        external_userid = msg.get("external_userid", "")
        content = (msg.get("text") or {}).get("content", "").strip()
        if not external_userid or not content:
            return

        # 顾客唯一ID即会话ID：天然会话隔离，与网页/其他顾客互不污染
        session_id = f"wxkf_{external_userid}"

        if self._on_hold(session_id):
            logger.info(f"微信客服: 会话 {session_id} 人工接管中，AI 静默")
            return

        from interface.base import Message, MessageType, Reply

        message = Message(
            session_id=session_id,
            content=content,
            sender_id=external_userid,
            sender_name=f"微信顾客_{external_userid[-6:]}",
            channel_name="wechat_kf",
        )
        try:
            reply: Reply = await self.message_handler(message)
        except Exception as e:
            logger.exception(f"微信客服: message_handler异常 [session={session_id}]: {e}")
            reply = Reply(type=MessageType.TEXT, content="哎呀，我这边出了点小状况，请稍后再试或联系店主处理 🙏")

        content_out = (reply.content or "").strip()
        if content_out:
            await self._send_text(external_userid, content_out)

    async def _handle_event(self, msg: dict) -> None:
        event = msg.get("event") or {}
        event_type = event.get("event_type", "")
        if event_type == "enter_session" and event.get("welcome_code"):
            # 顾客点开客服：48小时内有效的欢迎语 code，立即发送
            store = self._store_name()
            welcome = (
                f"你好呀，我是{store}的AI店长助理 🙋\n"
                "项目价格、营业时间、预约安排都可以直接问我；"
                "复杂的事我会马上转给店主本人回复～"
            )
            ok = await self._send_welcome(event["welcome_code"], welcome)
            if not ok:
                logger.warning("微信客服: 欢迎语发送失败（code可能已过期，忽略）")

    def _store_name(self) -> str:
        try:
            from config.business_config import business_config
            return business_config.get_business_name()
        except Exception:
            return "本店"

    # ==================== 发消息（kf/send_msg） ====================

    async def _send_text(self, touser: str, content: str) -> bool:
        if not touser:
            return False
        token = await self._get_access_token()
        url = f"{QYAPI}/kf/send_msg?access_token={token}"
        payload = {
            "touser": touser,
            "open_kfid": self.open_kfid,
            "msgtype": "text",
            "text": {"content": content[:2048]},
        }
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            if data.get("errcode"):
                logger.error(f"微信客服: 发送失败 touser={touser[-6:]}: {data}")
                return False
            return True
        except Exception as e:
            logger.error(f"微信客服: 发送异常 touser={touser[-6:]}: {e}")
            return False

    async def _send_welcome(self, welcome_code: str, content: str) -> bool:
        token = await self._get_access_token()
        url = f"{QYAPI}/kf/send_msg?access_token={token}"
        payload = {
            "touser": "",  # 欢迎语模式由 welcome_code 定位顾客
            "open_kfid": self.open_kfid,
            "msgtype": "text",
            "text": {"content": content[:2048]},
            "welcome_code": welcome_code,
        }
        try:
            resp = await self._client.post(url, json=payload)
            data = resp.json()
            return not data.get("errcode")
        except Exception as e:
            logger.error(f"微信客服: 欢迎语发送异常: {e}")
            return False

    # ==================== 回调端点 ====================

    def _register_routes(self) -> None:
        if self._app is None:
            return

        @self._app.get("/wecom/kf/callback", include_in_schema=False)
        async def kf_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
            """URL有效性验证（开启API时企微调用一次）。解密 echostr 原样返回明文。"""
            if not self._crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
                logger.warning("微信客服: 回调验证签名不通过")
                return ""
            plain = self._crypto.decrypt_message(echostr)
            if not plain:
                return ""
            logger.info("微信客服: 回调URL验证通过")
            # 官方要求返回明文（无引号无包裹）
            from fastapi import Response
            return Response(content=plain, media_type="text/plain")

        @self._app.post("/wecom/kf/callback", include_in_schema=False)
        async def kf_event(request):
            """事件通知：解密XML，立即触发一次拉取（fire-and-forget），固定回 success。"""
            body = (await request.body()).decode("utf-8", errors="replace")
            try:
                root = ET.fromstring(body)
                encrypt = root.findtext("Encrypt", "")
                timestamp = request.query_params.get("timestamp", "")
                nonce = request.query_params.get("nonce", "")
                signature = request.query_params.get("msg_signature", "")
                if encrypt and self._crypto.verify_signature(signature, timestamp, nonce, encrypt):
                    # 通知里不含消息本体，只当"拉取触发器"
                    asyncio.create_task(self._safe_sync())
            except Exception as e:
                logger.warning(f"微信客服: 回调事件处理异常: {e}")
            from fastapi import Response
            return Response(content="success", media_type="text/plain")

    async def _safe_sync(self) -> None:
        try:
            await self._sync_and_reply()
        except Exception as e:
            logger.warning(f"微信客服: 回调触发拉取失败（轮询兜底）: {e}")

    async def _poll_loop(self) -> None:
        """定时轮询兜底：回调丢失/失败也能收到消息。"""
        logger.info(f"微信客服: 轮询已启动（每 {self.poll_interval}s）")
        while True:
            try:
                n = await self._sync_and_reply()
                if n:
                    logger.info(f"微信客服: 本轮处理 {n} 条顾客消息")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"微信客服: 轮询异常（下轮重试）: {e}")
            await asyncio.sleep(self.poll_interval)

    # ==================== 生命周期 ====================

    async def startup(self) -> None:
        self._load_cursor()
        self._register_routes()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"微信客服通道已启动: open_kfid={self.open_kfid[:8]}...")

    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()
        logger.info("微信客服通道已停止")


def create_kf_channel_from_env(message_handler, app=None) -> Optional[WeChatKfChannel]:
    """从环境变量创建微信客服通道；配置不全时返回 None（不影响 Web 服务启动）。

    必需环境变量:
        WECOM_CORP_ID       企业ID（企微后台-我的企业）
        WECOM_KF_SECRET     微信客服应用Secret（开启API后查看）
        WECOM_KF_OPEN_KFID  客服账号ID（wk开头）
        WECOM_KF_TOKEN      回调配置Token
        WECOM_KF_AES_KEY    回调配置EncodingAESKey（43位）
    可选:
        WECOM_KF_POLL_INTERVAL  轮询间隔秒数（默认20）
        WECOM_KF_HOLD_MINUTES   转人工后AI静默分钟数（默认30）
    """
    import os

    corp_id = os.getenv("WECOM_CORP_ID", "").strip()
    secret = os.getenv("WECOM_KF_SECRET", "").strip()
    open_kfid = os.getenv("WECOM_KF_OPEN_KFID", "").strip()
    token = os.getenv("WECOM_KF_TOKEN", "").strip()
    aes_key = os.getenv("WECOM_KF_AES_KEY", "").strip()

    missing = [k for k, v in {
        "WECOM_CORP_ID": corp_id,
        "WECOM_KF_SECRET": secret,
        "WECOM_KF_OPEN_KFID": open_kfid,
        "WECOM_KF_TOKEN": token,
        "WECOM_KF_AES_KEY": aes_key,
    }.items() if not v]
    if missing:
        logger.info(f"微信客服通道未配置（缺 {','.join(missing)}），跳过启动")
        return None
    if len(aes_key) != 43:
        logger.error("微信客服通道: WECOM_KF_AES_KEY 必须43位（企微后台原样复制）")
        return None

    return WeChatKfChannel(
        message_handler=message_handler,
        corp_id=corp_id,
        secret=secret,
        token=token,
        encoding_aes_key=aes_key,
        open_kfid=open_kfid,
        app=app,
        poll_interval=int(os.getenv("WECOM_KF_POLL_INTERVAL", "20")),
        human_hold_minutes=int(os.getenv("WECOM_KF_HOLD_MINUTES", "30")),
    )
