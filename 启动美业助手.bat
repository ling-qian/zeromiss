@echo off
chcp 65001 >nul
title 美业AI店长助手
echo.
echo  ========================================
echo    美业AI店长助手 - 启动中...
echo  ========================================
echo.

:: 检查是否首次运行
if not exist "data" mkdir data
if not exist ".env" (
    echo  [首次运行] 正在生成配置文件...
    python scripts\setup_env.py
    echo.
    echo  配置文件已生成，请编辑 .env 填入以下信息：
    echo    1. DEEPSEEK_API_KEY  - DeepSeek API密钥（推荐，便宜好用）
    echo    2. 或者 OPENAI_API_KEY - OpenAI API密钥
    echo    3. WECHAT_MODE=wecom  - 启用企业微信（可选）
    echo.
    echo  获取DeepSeek API Key: https://platform.deepseek.com/
    echo  注册企业微信: https://work.weixin.qq.com/
    echo.
    pause
    notepad .env
)

:: 启动服务
python app.py --port 8080 --username admin --password admin123

echo.
echo  服务已停止
pause
