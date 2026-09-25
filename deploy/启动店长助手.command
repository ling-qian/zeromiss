#!/bin/bash
# BizBot AI 店长助手 — macOS 双击启动器
# 首次双击：自动创建虚拟环境并安装依赖（约2-5分钟，需联网）
# 之后双击：3秒启动，自动打开管理台
# 若 Gatekeeper 拦截：右键→打开（仅首次），或终端执行 xattr -cr 本目录

cd "$(dirname "$0")" || exit 1

CONF="launch.conf"
if [ ! -f "$CONF" ]; then
    echo "首次运行，设置管理台账号（只此一次）"
    read -r -p "用户名 [boss]: " U; U=${U:-boss}
    read -r -p "密码: " P; P=${P:-boss123}
    printf 'WEB_USERNAME=%s\nWEB_PASSWORD=%s\n' "$U" "$P" > "$CONF"
    chmod 600 "$CONF"
fi
# shellcheck disable=SC1090
source "$CONF"

if [ ! -d ".venv" ]; then
    echo "首次运行：安装依赖中（约2-5分钟，请勿关闭窗口）..."
    if ! python3 -m venv .venv; then echo "✗ 创建环境失败：需要 macOS 自带 python3"; read -r; exit 1; fi
    if ! ./.venv/bin/pip install -q -r requirements.txt; then
        echo "✗ 依赖安装失败，检查网络后重试"; read -r; exit 1
    fi
    echo "✓ 依赖就绪"
fi

# 等 API 就绪后自动打开管理台
(
    for _ in $(seq 1 30); do
        sleep 2
        if curl -sf http://127.0.0.1:8080/health >/dev/null 2>&1; then
            open "http://127.0.0.1:8080"
            break
        fi
    done
) &

echo "启动中... 管理台: http://127.0.0.1:8080 （关闭本窗口=停止服务）"
exec ./.venv/bin/python3 app.py --port 8080 --username "$WEB_USERNAME" --password "$WEB_PASSWORD"
