import asyncio
import websockets
import json
import sqlite3
import os

# База данных для пользователей и сообщений
conn = sqlite3.connect('chat.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        display_name TEXT,
        avatar_url TEXT
    )
''')
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

connected_users = {} # websocket: "@username"

async def handle_client(websocket):
    username = None
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # --- ЛОГИН И РЕГИСТРАЦИЯ ---
            if data['type'] == 'login':
                username = data['username']
                display_name = data.get('display_name', username)
                avatar_url = data.get('avatar_url', '')
                
                # Сохраняем или обновляем пользователя в БД
                cursor.execute("INSERT OR IGNORE INTO users (username, display_name, avatar_url) VALUES (?, ?, ?)", 
                               (username, display_name, avatar_url))
                conn.commit()
                
                connected_users[websocket] = username
                
                # Отправляем историю сообщений пакетом
                cursor.execute("""
                    SELECT sender, receiver, text FROM messages 
                    WHERE receiver = 'Global' OR receiver = ? OR sender = ? 
                    ORDER BY id ASC
                """, (username, username))
                history = cursor.fetchall()
                
                history_list = [{"sender": r[0], "to": r[1], "text": r[2]} for r in history]
                await websocket.send(json.dumps({"type": "history", "messages": history_list}))
            
            # --- ОБНОВЛЕНИЕ ПРОФИЛЯ ---
            elif data['type'] == 'update_profile':
                cursor.execute("UPDATE users SET display_name = ?, avatar_url = ? WHERE username = ?", 
                               (data['display_name'], data['avatar_url'], username))
                conn.commit()
            
            # --- ПОЛУЧЕНИЕ ИНФОРМАЦИИ О ПОЛЬЗОВАТЕЛЕ ---
            elif data['type'] == 'get_user':
                cursor.execute("SELECT username, display_name, avatar_url FROM users WHERE username = ?", (data['target'],))
                row = cursor.fetchone()
                if row:
                    await websocket.send(json.dumps({
                        "type": "user_info",
                        "username": row[0],
                        "display_name": row[1],
                        "avatar_url": row[2]
                    }))
            
            # --- ОТПРАВКА СООБЩЕНИЯ ---
            elif data['type'] == 'message':
                cursor.execute("INSERT INTO messages (sender, receiver, text) VALUES (?, ?, ?)", 
                               (username, data['to'], data['text']))
                conn.commit()
                
                out_msg = json.dumps({
                    "type": "message",
                    "sender": username,
                    "to": data['to'],
                    "text": data['text']
                })
                
                if data['to'] == 'Global':
                    for ws in connected_users:
                        await ws.send(out_msg)
                else:
                    # Отправляем получателю и самому себе
                    for ws, uname in connected_users.items():
                        if uname == data['to'] or ws == websocket:
                            await ws.send(out_msg)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in connected_users:
            del connected_users[websocket]

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handle_client, "0.0.0.0", port):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
