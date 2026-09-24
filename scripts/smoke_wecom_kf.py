"""微信客服通道冒烟测试（纯本地，零网络依赖）。

跑法: python3 scripts/smoke_wecom_kf.py
覆盖: 加解密往返+验签 / MsgId排重LRU / 人工接管计时 / 游标持久化 / 缺配置降级
"""
import os
import sys
import time
import tempfile
import base64
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 全部环境变量指向测试值，确保 create_kf_channel_from_env 走通且不发任何真实请求
os.environ.update({
    "WECOM_CORP_ID": "ww_test_corp",
    "WECOM_KF_SECRET": "test_secret",
    "WECOM_KF_OPEN_KFID": "wkTEST123",
    "WECOM_KF_TOKEN": "test_token",
    "WECOM_KF_AES_KEY": "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopq",  # 43位
})

from interface.wechat.kf_channel import WeChatKfChannel, create_kf_channel_from_env  # noqa: E402
from interface.wechat.channel import WeComCrypto  # noqa: E402

TOKEN = "test_token"
AES_KEY_STR = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopq"
CORP_ID = "ww_test_corp"

PASS = 0


def ok(name: str):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def encrypt_for_test(crypto: WeComCrypto, plaintext: str) -> str:
    """按企微协议构造密文（反向验证 decrypt_message 的正确性）。"""
    from Crypto.Cipher import AES
    msg_bytes = plaintext.encode("utf-8")
    rand = b"1234567890abcdef"
    content = rand + struct.pack(">I", len(msg_bytes)) + msg_bytes + CORP_ID.encode("utf-8")
    pad = 32 - (len(content) % 32)
    content += bytes([pad]) * pad
    cipher = AES.new(crypto.aes_key, AES.MODE_CBC, crypto.aes_key[:16])
    return base64.b64encode(cipher.encrypt(content)).decode()


def test_crypto_roundtrip():
    crypto = WeComCrypto(TOKEN, AES_KEY_STR, CORP_ID)
    plaintext = '<xml><ToUserName><![CDATA[ww_test_corp]]></ToUserName></xml>'
    encrypt = encrypt_for_test(crypto, plaintext)

    # 验签
    import hashlib
    sig = hashlib.sha1("".join(sorted([TOKEN, "1700000000", "nonce1", encrypt])).encode()).hexdigest()
    assert crypto.verify_signature(sig, "1700000000", "nonce1", encrypt), "验签应通过"
    assert not crypto.verify_signature("badsig", "1700000000", "nonce1", encrypt), "坏签名应拒绝"
    ok("签名验证（好/坏签名）")

    # 解密往返
    assert crypto.decrypt_message(encrypt) == plaintext, "解密应还原明文"
    # corp_id 不匹配应返回空串（防跨企业投递）
    other = WeComCrypto(TOKEN, AES_KEY_STR, "ww_other_corp")
    assert other.decrypt_message(encrypt) == "", "corp_id不匹配应拒绝"
    ok("AES解密往返 + corp_id防投递校验")


def make_channel(tmpdir: str) -> WeChatKfChannel:
    return WeChatKfChannel(
        message_handler=None,
        corp_id=CORP_ID,
        secret="test_secret",
        token=TOKEN,
        encoding_aes_key=AES_KEY_STR,
        open_kfid="wkTEST123",
        app=None,
        poll_interval=5,
        cursor_path=str(Path(tmpdir) / "cursor.txt"),
    )


def test_dedup_lru():
    ch = make_channel(tempfile.mkdtemp())
    assert ch._dedup("m1") is True
    assert ch._dedup("m1") is False, "重复msgid应跳过"
    for i in range(2100):
        ch._dedup(f"m{i}")
    assert ch._dedup("m1") is True, "LRU淘汰后m1应可再次处理"
    assert len(ch._seen) <= 2000, "LRU上限应生效"
    ok("MsgId排重 + LRU上限")


def test_human_hold():
    ch = make_channel(tempfile.mkdtemp())
    assert ch._on_hold("s1") is False
    ch.set_human_hold("s1", minutes=30)
    assert ch._on_hold("s1") is True, "接管期内应静默"
    ch._human_hold["s1"] = time.time() - 1  # 手动过期
    assert ch._on_hold("s1") is False, "超时应恢复AI"
    ok("人工接管：静默→超时恢复")


def test_cursor_persistence():
    tmpdir = tempfile.mkdtemp()
    ch = make_channel(tmpdir)
    ch._save_cursor("CURSOR_ABC_123")
    ch2 = make_channel(tmpdir)  # 模拟进程重启
    ch2._load_cursor()
    assert ch2._cursor == "CURSOR_ABC_123", "游标应跨进程恢复"
    ok("游标持久化 + 重启恢复")


def test_env_factory():
    # 环境已配置 → 应成功创建
    ch = create_kf_channel_from_env(None, app=None)
    assert ch is not None and ch.open_kfid == "wkTEST123"
    # 缺配置 → 返回 None 降级
    saved = os.environ.pop("WECOM_KF_SECRET")
    try:
        assert create_kf_channel_from_env(None, app=None) is None
    finally:
        os.environ["WECOM_KF_SECRET"] = saved
    # AES key 长度校验
    os.environ["WECOM_KF_AES_KEY"] = "short"
    try:
        assert create_kf_channel_from_env(None, app=None) is None
    finally:
        os.environ["WECOM_KF_AES_KEY"] = AES_KEY_STR
    ok("环境变量工厂：完整创建/缺失降级/AES长度校验")


if __name__ == "__main__":
    print("微信客服通道冒烟测试")
    print("=" * 40)
    test_crypto_roundtrip()
    test_dedup_lru()
    test_human_hold()
    test_cursor_persistence()
    test_env_factory()
    print("=" * 40)
    print(f"全部通过: {PASS}/5 组")
