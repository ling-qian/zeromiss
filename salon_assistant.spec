# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 美业AI店长助手

使用方式：
  pyinstaller salon_assistant.spec

生成：
  dist/美业AI店长助手/美业AI店长助手.exe  （单目录模式，含SQLite数据库）
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# 收集所有隐式依赖
hiddenimports = [
    'greenlet',  # SQLAlchemy 2.x 异步模式必需，动态加载 PyInstaller 扫不到
    'appdirs',  # pkg_resources 运行时钩子需要
    'sqlalchemy.dialects.sqlite',
    'sqlalchemy.orm',
    'sqlalchemy.sql',
    'sqlalchemy.pool',
    'sqlalchemy.engine',
    'sqlalchemy.event',
    'sqlalchemy.ext.declarative',
    'loguru',
    'pydantic',
    'pydantic_settings',
    'dotenv',
    'httpx',
    'dateutil',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 如果有前端静态文件，需要一起打包
        # ('interface/web/static', 'interface/web/static'),
        # ('interface/web/templates', 'interface/web/templates'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='美业AI店长助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 改为False则无黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # 可以加 .ico 图标
)
