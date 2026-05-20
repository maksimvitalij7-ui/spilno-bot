#!/bin/bash

echo "🛑 Останавливаю бота..."
pkill -f "python3 bot.py" 2>/dev/null
sleep 2

echo "📦 Устанавливаю обновления..."
pip3 install -r requirements.txt --quiet --break-system-packages 2>/dev/null || pip3 install -r requirements.txt --quiet

echo "🚀 Запускаю бота..."
python3 bot.py
