#!/usr/bin/env bash
# BizBot 一键部署/更新脚本。用法: bash deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== BizBot 部署 =="
[ -f .env ] || { echo "✗ 缺 .env（cp .env.example .env 后填写）"; exit 1; }
mkdir -p certs

# SSL 证书检查（nginx 挂载只读，缺了 nginx 起不来）
if [ ! -f certs/fullchain.pem ] || [ ! -f certs/privkey.pem ]; then
  echo "✗ 缺 SSL 证书: certs/fullchain.pem + certs/privkey.pem（备案域名的证书）"
  echo "  腾讯云/阿里云控制台可免费签发，下载 nginx 格式放进来"
  exit 1
fi

echo "-- 构建 + 启动"
docker compose up -d --build

echo "-- 健康检查（最多等 30s）"
for i in $(seq 1 15); do
  if curl -sf http://127.0.0.1:8080/health >/dev/null 2>&1; then
    echo "✓ 应用已启动"
    echo "✓ 对外入口: https://你的域名  |  企微回调地址: https://你的域名/wecom/kf/callback"
    echo ""
    echo "上线验收三测（deploy/部署手册 第六节）:"
    echo "  1. 企微后台回调验证通过"
    echo "  2. 手机微信扫码进客服 → 收到欢迎语"
    echo "  3. 发'多少钱' → 秒回价格"
    exit 0
  fi
  sleep 2
done
echo "✗ 应用未就绪，查日志: docker compose logs bizbot --tail 50"
exit 1
