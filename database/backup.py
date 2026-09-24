"""数据库自动备份模块。

职责：
- SQLite 在线备份（backup API，备份期间不阻塞业务读写）
- 备份文件保留策略：最近 7 份每日备份 + 每月 1 号备份长期保留
- 后台每日定时备份（默认 02:00）
- 供看板展示的最新备份状态

用法（app.py）：
    from database.backup import backup_database, start_backup_scheduler
    backup_database(db)                  # 启动时立即备份一次
    start_backup_scheduler(db)           # 每日 02:00 自动备份
"""
import asyncio
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

# 默认保留：最近 KEEP_DAILY 份 + 历史每月 1 号的备份
KEEP_DAILY = 7
BACKUP_DIR = "data/backups"


def _extract_sqlite_path(database_url: str) -> str:
    """从 SQLAlchemy URL 提取 SQLite 文件路径。"""
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    raise ValueError(f"非 SQLite 数据库，不适用本模块: {database_url}")


def backup_database(db_manager: Any, backup_dir: str = BACKUP_DIR) -> Dict[str, Any]:
    """立即执行一次备份。

    使用 sqlite3 在线备份 API（对 WAL 模式安全），备份期间业务可继续读写。

    Args:
        db_manager: DatabaseManager 实例（需有 database_url 属性）。
        backup_dir: 备份目录。

    Returns:
        {"success": bool, "path": str, "size": int, "time": "YYYY-MM-DD HH:MM",
         "error": str(失败时)}
    """
    now = datetime.now()
    result: Dict[str, Any] = {"success": False, "time": now.strftime("%Y-%m-%d %H:%M")}

    try:
        src_path = _extract_sqlite_path(db_manager.database_url)
        if not os.path.exists(src_path):
            result["error"] = f"源数据库不存在: {src_path}"
            logger.error(f"[Backup] {result['error']}")
            return result

        dest_dir = Path(backup_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"store_{now.strftime('%Y%m%d_%H%M%S')}.db"

        # WAL 模式下数据库由多文件组成，backup API 会把一致性快照写入单文件
        src_conn = sqlite3.connect(src_path)
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            src_conn.close()

        size = dest_path.stat().st_size
        result.update({
            "success": True,
            "path": str(dest_path),
            "size": size,
        })
        logger.info(f"[Backup] 备份完成: {dest_path} ({size / 1024:.0f} KB)")

        _apply_retention(dest_dir)
        return result
    except Exception as e:
        result["error"] = str(e)
        logger.exception(f"[Backup] 备份失败: {e}")
        return result


def _apply_retention(backup_dir: Path) -> None:
    """清理旧备份：保留最近 KEEP_DAILY 份 + 所有每月 1 号的备份。

    文件名格式：store_YYYYMMDD_HHMMSS.db
    """
    files = sorted(backup_dir.glob("store_*.db"))
    if len(files) <= KEEP_DAILY:
        return

    keep = set(files[-KEEP_DAILY:])
    # 每月 1 号的备份长期保留（灾备月度锚点）
    for f in files:
        name = f.stem  # store_YYYYMMDD_HHMMSS
        try:
            day = int(name.split("_")[1][6:8])
            if day == 1:
                keep.add(f)
        except (IndexError, ValueError):
            pass

    for f in files:
        if f not in keep:
            f.unlink(missing_ok=True)
            logger.info(f"[Backup] 清理过期备份: {f.name}")


def get_backup_status(backup_dir: str = BACKUP_DIR) -> Optional[Dict[str, Any]]:
    """获取最新备份状态（供看板展示）。

    Returns:
        {"time": "YYYY-MM-DD HH:MM", "path": str, "size_kb": int} 或 None（无备份）。
    """
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return None
    files = sorted(backup_path.glob("store_*.db"))
    if not files:
        return None
    latest = files[-1]
    mtime = datetime.fromtimestamp(latest.stat().st_mtime)
    return {
        "time": mtime.strftime("%Y-%m-%d %H:%M"),
        "path": str(latest),
        "size_kb": int(latest.stat().st_size / 1024),
    }


def list_backups(backup_dir: str = BACKUP_DIR) -> List[Dict[str, Any]]:
    """列出全部备份（新→旧）。"""
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return []
    files = sorted(backup_path.glob("store_*.db"), reverse=True)
    out = []
    for f in files:
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        out.append({
            "name": f.name,
            "time": mtime.strftime("%Y-%m-%d %H:%M"),
            "size_kb": int(f.stat().st_size / 1024),
        })
    return out


async def start_backup_scheduler(
    db_manager: Any,
    backup_dir: str = BACKUP_DIR,
    hour: int = 2,
    minute: int = 0,
) -> asyncio.Task:
    """启动每日定时备份后台任务。

    Args:
        db_manager: DatabaseManager 实例。
        backup_dir: 备份目录。
        hour/minute: 每日备份时刻，默认 02:00。

    Returns:
        asyncio.Task（应用关闭时 cancel 即可）。
    """
    async def _loop():
        while True:
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            logger.debug(f"[Backup] 下次备份: {target.strftime('%Y-%m-%d %H:%M')}")
            await asyncio.sleep(wait_secs)
            # 备份在独立线程跑，避免阻塞事件循环
            await asyncio.get_event_loop().run_in_executor(
                None, backup_database, db_manager, backup_dir
            )

    task = asyncio.get_event_loop().create_task(_loop())
    logger.info(f"[Backup] 每日备份调度已启动（每日 {hour:02d}:{minute:02d}）")
    return task
