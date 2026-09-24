"""数据库连接与基础设施管理。

本模块负责数据库的底层基础设施，包括：
- 数据库引擎创建（自动识别 SQLite/PostgreSQL）
- 会话（Session）管理
- 数据库表创建
- 原始SQL执行

本模块不包含任何业务逻辑，仅提供数据库基础操作。
"""
import os
from typing import Optional, Union, Any
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from .models import Base
from config.settings import settings


class DatabaseConnection:
    """数据库连接管理器。

    负责数据库引擎的创建和会话管理，根据数据库URL自动选择
    同步（SQLite）或异步（PostgreSQL）引擎。

    Attributes:
        database_url: 数据库连接URL。
        engine: SQLAlchemy引擎对象（同步或异步）。
        SessionLocal: 会话工厂。
        is_async: 是否为异步引擎。

    Example:
        ```python
        # SQLite（同步，适合开发环境）
        conn = DatabaseConnection("sqlite:///data/store.db")

        # PostgreSQL（异步，适合生产环境）
        conn = DatabaseConnection("postgresql://user:pass@host/db")

        # 使用默认配置
        conn = DatabaseConnection()
        ```
    """

    def __init__(self, database_url: Optional[str] = None) -> None:
        """初始化数据库连接。

        Args:
            database_url: 数据库连接URL，如果为None则使用settings中的配置。
                        支持格式：
                        - sqlite:///path/to/db.db (同步)
                        - postgresql://user:pass@host/db (异步)
        """
        self.database_url: str = database_url or settings.database_url

        if self.database_url.startswith("sqlite"):
            # 自动创建 SQLite 数据库文件所在目录
            # sqlite:///relative/path/db  → 相对路径（3个斜杠）
            # sqlite:////absolute/path/db → 绝对路径（4个斜杠）
            db_path = self.database_url[len("sqlite:///"):]
            if db_path:
                db_dir = os.path.dirname(db_path)
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)
            self.engine = create_engine(
                self.database_url,
                echo=False,
                connect_args={"check_same_thread": False}
            )
            # SQLite 并发优化：WAL 模式（读写不互斥）+ 忙等待超时（防 database is locked）
            from sqlalchemy import event as sa_event
            @sa_event.listens_for(self.engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
            self.SessionLocal = sessionmaker(
                bind=self.engine, autocommit=False, autoflush=False
            )
            self.is_async: bool = False
        else:
            async_url = self.database_url.replace(
                "postgresql://", "postgresql+asyncpg://"
            )
            self.engine = create_async_engine(async_url, echo=False)
            self.SessionLocal = async_sessionmaker(
                self.engine, class_=AsyncSession
            )
            self.is_async: bool = True

    def create_tables(self) -> None:
        """创建所有数据库表。

        根据 models.py 中定义的所有模型创建对应的数据库表。
        如果表已存在则不会重复创建（幂等操作）。
        另外为已有旧库补建查询索引（CREATE INDEX IF NOT EXISTS 幂等）。
        """
        Base.metadata.create_all(self.engine)
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """为高频查询字段补建索引（对已存在的表，create_all 不会补索引）。

        幂等：全部使用 IF NOT EXISTS，重复执行无害。
        """
        if not self.database_url.startswith("sqlite"):
            return  # PostgreSQL 生产库建议由迁移脚本管理
        idx_sql = [
            "CREATE INDEX IF NOT EXISTS ix_customers_name ON customers (name)",
            "CREATE INDEX IF NOT EXISTS ix_customers_phone ON customers (phone)",
            "CREATE INDEX IF NOT EXISTS ix_service_records_service_date ON service_records (service_date)",
            "CREATE INDEX IF NOT EXISTS ix_service_records_customer_id ON service_records (customer_id)",
            "CREATE INDEX IF NOT EXISTS ix_service_records_employee_id ON service_records (employee_id)",
            "CREATE INDEX IF NOT EXISTS ix_product_sales_sale_date ON product_sales (sale_date)",
            "CREATE INDEX IF NOT EXISTS ix_product_sales_customer_id ON product_sales (customer_id)",
            "CREATE INDEX IF NOT EXISTS ix_memberships_customer_id ON memberships (customer_id)",
        ]
        with self.get_session() as session:
            for sql in idx_sql:
                session.execute(text(sql))
            session.commit()

    def get_session(self) -> Union[Session, AsyncSession]:
        """获取数据库会话。

        Returns:
            数据库会话对象。根据 is_async 属性返回同步或异步会话。
        """
        return self.SessionLocal()

    def execute_raw_sql(self, sql: str, params: Optional[dict] = None) -> Any:
        """执行原始SQL语句。

        注意：此方法应谨慎使用，建议优先使用ORM方法。
        如果必须使用原始SQL，请确保SQL语句安全，避免SQL注入。

        Args:
            sql: SQL语句字符串。
            params: SQL参数字典（可选）。

        Returns:
            SQL执行结果。

        Raises:
            Exception: 如果SQL执行失败。
        """
        with self.get_session() as session:
            result = session.execute(text(sql), params or {})
            session.commit()
            return result

    def close(self) -> None:
        """关闭数据库连接，释放引擎资源。

        释放连接池中的所有连接。调用后不应再使用此连接实例。
        """
        if self.engine is not None:
            self.engine.dispose()

