import asyncio
import websockets
import json
import sqlite3
import os

# 1. Настройка Базы Данных (SQLite)
# Файл chat.db создастся автоматически рядом со скриптом
conn = sqlite3.connect('chat.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        receiver TEXT,
        text TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()

# Храним активные подключения { websocket: "Никнейм" }
connected_users = {}

async def broadcast_users():
    """Отправляет всем список пользователей, которые сейчас онлайн"""
    users_list = list(connected_users.values())
    msg = json.dumps({"type": "users", "list": users_list})
    for ws in connected_users:
        try:
            await ws.send(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

async def handle_client(websocket):
    nickname = None
    try:
        async for message in websocket:
            data = json.loads(message)
            
            if data['type'] == 'login':
                nickname = data['nickname']
                connected_users[websocket] = nickname
                await broadcast_users()
                
                # При входе достаем из БД историю сообщений:
                # Берем сообщения из Общего чата И ЛИЧНЫЕ переписки этого пользователя
                cursor.execute("""
                    SELECT sender, receiver, text 
                    FROM messages 
                    WHERE receiver = 'Global' OR receiver = ? OR sender = ? 
                    ORDER BY id ASC
                """, (nickname, nickname))
                
                history = cursor.fetchall()
                # Отправляем всю историю пользователю
                for row in history:
                    await websocket.send(json.dumps({
                        "type": "message",
                        "sender": row[0],
                        "to": row[1],
                        "text": row[2]
                    }))

            elif data['type'] == 'message':
                sender = nickname
                receiver = data['to'] # Это может быть 'Global' или имя конкретного человека
                text = data['text']
                
                # 1. Сохраняем сообщение в Базу Данных
                cursor.execute("INSERT INTO messages (sender, receiver, text) VALUES (?, ?, ?)", (sender, receiver, text))
                conn.commit()
                
                # Формируем ответный пакет
                out_msg = json.dumps({
                    "type": "message",
                    "sender": sender,
                    "to": receiver,
                    "text": text
                })
                
                # 2. Пересылаем сообщение
                if receiver == 'Global':
                    # Если общий чат — шлем всем
                    for ws in connected_users:
                        await ws.send(out_msg)
                else:
                    # Если ЛС — шлем только получателю и самому отправителю (чтобы появилось на экране)
                    for ws, uname in connected_users.items():
                        if uname == receiver or ws == websocket:
                            await ws.send(out_msg)

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"Ошибка: {e}")
    finally:
        # При отключении удаляем пользователя и обновляем список онлайн
        if websocket in connected_users:
            del connected_users[websocket]
        await broadcast_users()

async def main():
    port = int(os.environ.get("PORT", 10000))
    print(f"Сервер чата запущен на порту {port}")
    async with websockets.serve(handle_client, "0.0.0.0", port):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
