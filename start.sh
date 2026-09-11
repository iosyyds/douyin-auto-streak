#!/bin/bash
set -e

# 安装 Playwright Chromium 浏览器
python -m playwright install chromium

# 启动应用
exec python app.py
