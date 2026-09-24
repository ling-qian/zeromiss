#!/usr/bin/env python3
"""数据库备份模块测试（P0-2 回归）。

运行: python test_backup.py
"""
import asyncio
import os
import sys
import tempfile
import sqlite3
from pathlib import Path

DB_URL = f'sqlite:///{tempfile.mktemp(suffix=".db")}'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.backup import (backup_database, get_backup_status, list_backups,
                             _apply_retention)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


class FakeDB:
    """假 DatabaseManager：只提供 database_url。"""

    def __init__(self, db_path: str):
        self.database_url = f"sqlite:///{db_path}"


def make_src_db() -> str:
    """创建一个带数据的源库。"""
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, note TEXT)")
    conn.execute("INSERT INTO t VALUES (1, '张姐的会员卡')")
    conn.commit()
    conn.close()
    return path


def test_backup_success():
    print("\n▶ 备份成功且数据完整")
    src = make_src_db()
    db = FakeDB(src)
    bdir = tempfile.mkdtemp()
    r = backup_database(db, backup_dir=bdir)
    check("备份成功", r["success"])
    check("备份文件存在", os.path.exists(r.get("path", "")))
    # 备份内容可查
    conn = sqlite3.connect(r["path"])
    row = conn.execute("SELECT note FROM t WHERE id=1").fetchone()
    conn.close()
    check("备份数据可读", row and row[0] == "张姐的会员卡")
    status = get_backup_status(backup_dir=bdir)
    check("状态可查", status and status["path"] == r["path"])
    check("状态含大小", status and status["size_kb"] >= 0)
    return bdir


def test_backup_wal_mode():
    print("\n▶ WAL 模式源库备份（一致性快照）")
    src = make_src_db()
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL 里再写一条（主库文件尚未 checkpoint）
    conn.execute("INSERT INTO t VALUES (2, 'WAL里写入的')")
    conn.commit()
    db = FakeDB(src)
    bdir = tempfile.mkdtemp()
    r = backup_database(db, backup_dir=bdir)
    conn.close()
    check("WAL库备份成功", r["success"])
    bconn = sqlite3.connect(r["path"])
    rows = bconn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    bconn.close()
    check("WAL未checkpoint的数据也进了备份", rows == 2, f"实际 {rows} 行")


def test_retention():
    print("\n▶ 保留策略：最近7份 + 每月1号")
    bdir = Path(tempfile.mkdtemp())
    # 造 12 个文件：11 号-22 号 + 一个 1 号
    names = [f"store_20260901_020000.db"]  # 月度锚点
    for day in range(11, 23):
        names.append(f"store_202609{day:02d}_020000.db")
    for n in names:
        (bdir / n).write_bytes(b"x" * 100)
    _apply_retention(bdir)
    remaining = sorted(f.name for f in bdir.glob("store_*.db"))
    check("每月1号备份保留", "store_20260901_020000.db" in remaining)
    check("保留最近7份", len(remaining) == 8, f"实际 {len(remaining)}: {remaining}")
    check("最旧的日常备份被清理", "store_20260911_020000.db" not in remaining)
    check("最新的保留", "store_20260922_020000.db" in remaining)


def test_list_and_status_empty():
    print("\n▶ 空目录状态")
    bdir = tempfile.mkdtemp()
    check("无备份时状态为None", get_backup_status(backup_dir=bdir) is None)
    check("无备份时列表为空", list_backups(backup_dir=bdir) == [])


def test_backup_scheduler():
    print("\n▶ 定时备份调度启动")
    from database.backup import start_backup_scheduler
    src = make_src_db()
    db = FakeDB(src)

    async def run():
        task = await start_backup_scheduler(db, backup_dir=tempfile.mkdtemp(),
                                            hour=3, minute=0)
        started = not task.done() or task.cancelled()
        task.cancel()
        return started

    ok = asyncio.run(run())
    check("调度任务可启动", ok)


def test_missing_source():
    print("\n▶ 源库不存在的容错")
    db = FakeDB("/nonexistent/path/store.db")
    r = backup_database(db, backup_dir=tempfile.mkdtemp())
    check("返回失败而非抛异常", r["success"] is False)
    check("错误信息可读", "不存在" in r.get("error", ""))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print(f"\n{'='*50}")
    print(f"备份测试: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
