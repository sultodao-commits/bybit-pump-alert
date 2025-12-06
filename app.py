#!/usr/bin/env python3
import os
import requests
import sys

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def test_telegram():
    print("🔍 Тестирование Telegram бота...")
    print(f"Token: {'указан' if TELEGRAM_BOT_TOKEN else 'НЕ указан!'}")
    print(f"Chat ID: {'указан' if TELEGRAM_CHAT_ID else 'НЕ указан!'}")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        sys.exit(1)
    
    # Тест 1: Проверка токена
    print("\n1. Проверка токена бота...")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                bot_info = data['result']
                print(f"✅ Бот найден: @{bot_info.get('username')} - {bot_info.get('first_name')}")
            else:
                print(f"❌ Ошибка: {data.get('description')}")
                sys.exit(1)
        else:
            print(f"❌ HTTP ошибка: {response.status_code}")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        sys.exit(1)
    
    # Тест 2: Проверка chat_id
    print("\n2. Проверка chat_id...")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat"
    params = {"chat_id": TELEGRAM_CHAT_ID}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                chat_info = data['result']
                print(f"✅ Чат найден: {chat_info.get('title', 'Private Chat')} (тип: {chat_info.get('type')})")
            else:
                print(f"❌ Ошибка: {data.get('description')}")
                print("ℹ️ Убедись, что бот добавлен в этот чат")
                sys.exit(1)
        else:
            print(f"❌ HTTP ошибка: {response.status_code}")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")
        sys.exit(1)
    
    # Тест 3: Отправка тестового сообщения
    print("\n3. Отправка тестового сообщения...")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "✅ Тестовое сообщение от бота!\nБот работает корректно!",
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                print("✅ Сообщение успешно отправлено!")
                message_id = data['result']['message_id']
                print(f"📨 ID сообщения: {message_id}")
                
                # Тест 4: Удаление сообщения (опционально)
                print("\n4. Тест удаления сообщения (опционально)...")
                delete_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
                delete_payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "message_id": message_id
                }
                try:
                    del_response = requests.post(delete_url, json=delete_payload, timeout=5)
                    if del_response.status_code == 200:
                        print("✅ Сообщение удалено (тест завершен)")
                    else:
                        print("ℹ️ Не удалось удалить сообщение (но это не критично)")
                except:
                    print("ℹ️ Пропущено удаление сообщения")
                
                return True
            else:
                print(f"❌ Ошибка API: {data.get('description')}")
                return False
        else:
            print(f"❌ HTTP ошибка: {response.status_code}")
            print(f"❌ Ответ: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

if __name__ == "__main__":
    test_telegram()
