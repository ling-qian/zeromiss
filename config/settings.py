"""全局配置管理

所有用户可配置项均通过 .env 文件设置，运行时自动加载到此处。

使用方式：
    1. 运行 python scripts/setup_env.py 生成 .env 文件
    2. 或手动创建 .env 文件（参考 .env.example）
"""
import os
from pathlib import Path

# 先用 dotenv 把 .env 灌进 os.environ（pydantic-settings v2 的 env_file 加载有兼容问题）。
# override=True：.env 为本项目唯一配置源，优先于外部环境变量（沙箱/部署机可能注入同名空变量）。
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=True)
    except ImportError:
        pass

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置 - 所有字段均可通过 .env 或环境变量覆盖"""

    # ========== DeepSeek LLM 配置（默认，国内最便宜） ==========
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"

    # ========== MiniMax LLM 配置（备选） ==========
    minimax_api_key: str = ""
    minimax_model: str = "MiniMax-M2.5"
    minimax_base_url: str = "https://api.minimaxi.com/anthropic"

    # ========== OpenAI 配置（备选） ==========
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    # ========== LLM Provider 选择 ==========
    # 可选: deepseek / minimax / openai
    llm_provider: str = "deepseek"

    # ========== 数据库 ==========
    database_url: str = "sqlite:///data/store.db"

    # ========== Web 平台配置 ==========
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_username: str = "admin"
    web_password: str = "admin123"
    web_secret_key: str = "change-me-to-a-random-secret-key"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


# 全局配置实例
settings = Settings()
