import asyncio
import websockets
import json
import sqlite3
import os
import random

# База данных
conn = sqlite3.connect('chat.db', check_same_thread=False)
cursor = conn.cursor()

# Создаем таблицы
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        display_name TEXT,
        avatar_url TEXT
    )
''')
# Обновляем старую таблицу (добавляем колонку с паролем, если её нет)
try:
    cursor.execute("ALTER TABLE users ADD COLUMN password TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass # Колонка уже существует

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

# Словарь активных юзеров { websocket: "username" }
connected_users = {}

async def broadcast_online_status():
    """Рассылает всем список пользователей, которые сейчас онлайн"""
    online_list = list(connected_users.values())
    msg = json.dumps({"type": "online_users", "users": online_list})
    for ws in connected_users:
        try:
            await ws.send(msg)
        except:
            pass

async def handle_client(websocket):
    username = None
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # --- ЛОГИН И РЕГИСТРАЦИЯ ПО ПАРОЛЮ ---
            if data['type'] == 'login':
                display_name = data['display_name'].strip()
                password = data['password']
                
                # Ищем пользователя по нику
                cursor.execute("SELECT username, password, avatar_url FROM users WHERE display_name = ?", (display_name,))
                user_row = cursor.fetchone()
                
                if user_row:
                    # Пользователь найден, проверяем пароль
                    if user_row[1] != password:
                        await websocket.send(json.dumps({"type": "error", "message": "Неверный пароль или никнейм уже занят!"}))
                        continue
                    username = user_row[0]
                    avatar_url = user_row[2]
                else:
                    # Регистрация нового пользователя
                    # Генерируем временный username на основе ника
                    base_username = '@' + display_name.lower().replace(' ', '')
                    username = base_username
                    while True:
                        cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
                        if not cursor.fetchone(): break
                        username = base_username + str(random.randint(1, 999))
                    
                    avatar_url = ""
                    cursor.execute("INSERT INTO users (username, display_name, avatar_url, password) VALUES (?, ?, ?, ?)", 
                                   (username, display_name, avatar_url, password))
                    conn.commit()
                
                connected_users[websocket] = username
                await broadcast_online_status() # Сообщаем всем, что мы зашли
                
                # Отправляем историю и текущие данные юзера
                cursor.execute("""
                    SELECT m.sender, m.receiver, m.text, u.display_name, u.avatar_url
                    FROM messages m
                    LEFT JOIN users u ON m.sender = u.username
                    WHERE m.receiver = 'Global' OR m.receiver = ? OR m.sender = ? 
                    ORDER BY m.id ASC
                """, (username, username))
                history = cursor.fetchall()
                
                history_list = [{
                    "sender": r[0], "to": r[1], "text": r[2], 
                    "sender_display": r[3] or r[0], "sender_avatar": r[4] or ""
                } for r in history]
                
                await websocket.send(json.dumps({
                    "type": "login_success", 
                    "username": username, 
                    "display_name": display_name, 
                    "avatar_url": avatar_url,
                    "history": history_list
                }))
            
            # --- ИЗМЕНЕНИЕ ПРОФИЛЯ ---
            elif data['type'] == 'update_credentials':
                new_user = data.get('new_username')
                new_nick = data.get('display_name')
                new_avatar = data.get('avatar_url')

                if new_user and new_user != username:
                    cursor.execute("SELECT username FROM users WHERE username = ?", (new_user,))
                    if cursor.fetchone():
                        await websocket.send(json.dumps({"type": "error", "message": "Этот ID уже занят!"}))
                        continue
                    
                    cursor.execute("UPDATE users SET username = ?, display_name = ?, avatar_url = ? WHERE username = ?", (new_user, new_nick, new_avatar, username))
                    cursor.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_user, username))
                    cursor.execute("UPDATE messages SET receiver = ? WHERE receiver = ?", (new_user, username))
                    conn.commit()
                    connected_users[websocket] = new_user
                    username = new_user
                else:
                    cursor.execute("UPDATE users SET display_name = ?, avatar_url = ? WHERE username = ?", (new_nick, new_avatar, username))
                    conn.commit()
                    
                await websocket.send(json.dumps({"type": "credentials_updated", "username": username, "display_name": new_nick, "avatar_url": new_avatar}))
                await broadcast_online_status()

            # --- ПОИСК ---
            elif data['type'] == 'search_user':
                cursor.execute("SELECT username, display_name, avatar_url FROM users WHERE username = ?", (data['target'],))
                row = cursor.fetchone()
                if row:
                    await websocket.send(json.dumps({"type": "search_result", "username": row[0], "display_name": row[1], "avatar_url": row[2]}))
                else:
                    await websocket.send(json.dumps({"type": "error", "message": "Пользователь не найден!"}))

            # --- СООБЩЕНИЯ ---
            elif data['type'] == 'message':
                cursor.execute("INSERT INTO messages (sender, receiver, text) VALUES (?, ?, ?)", (username, data['to'], data['text']))
                conn.commit()
                
                cursor.execute("SELECT display_name, avatar_url FROM users WHERE username = ?", (username,))
                uinfo = cursor.fetchone()
                
                out_msg = json.dumps({
                    "type": "message", "sender": username, 
                    "sender_display": uinfo[0] if uinfo else username, 
                    "sender_avatar": uinfo[1] if uinfo else "",
                    "to": data['to'], "text": data['text']
                })
                
                if data['to'] == 'Global':
                    for ws in connected_users: await ws.send(out_msg)
                else:
                    for ws, uname in connected_users.items():
                        if uname == data['to'] or ws == websocket:
                            await ws.send(out_msg)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in connected_users: 
            del connected_users[websocket]
            await broadcast_online_status() # Обновляем статус при выходе

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handle_client, "0.0.0.0", port, max_size=10**7):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
