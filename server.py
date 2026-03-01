import asyncio
import websockets
import json
import sqlite3
import os

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

connected_users = {}

async def handle_client(websocket):
    username = None
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # --- ЛОГИН ---
            if data['type'] == 'login':
                username = data['username']
                display_name = data.get('display_name', username)
                avatar_url = data.get('avatar_url', '')
                
                cursor.execute("INSERT OR IGNORE INTO users (username, display_name, avatar_url) VALUES (?, ?, ?)", 
                               (username, display_name, avatar_url))
                # Обновляем инфу на случай, если юзер зашел с новыми данными
                cursor.execute("UPDATE users SET display_name = ?, avatar_url = ? WHERE username = ?", 
                               (display_name, avatar_url, username))
                conn.commit()
                connected_users[websocket] = username
                
                # Достаем историю вместе с никами и аватарками (JOIN)
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
                
                await websocket.send(json.dumps({"type": "history", "messages": history_list}))
            
            # --- ИЗМЕНЕНИЕ ПРОФИЛЯ И ID ---
            elif data['type'] == 'update_credentials':
                new_user = data.get('new_username')
                new_nick = data.get('display_name')
                new_avatar = data.get('avatar_url')

                if new_user and new_user != username:
                    cursor.execute("SELECT username FROM users WHERE username = ?", (new_user,))
                    if cursor.fetchone():
                        await websocket.send(json.dumps({"type": "error", "message": "Этот ID уже занят!"}))
                        continue
                    
                    # Меняем ID везде
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

            # --- ПОИСК ПОЛЬЗОВАТЕЛЯ ---
            elif data['type'] == 'search_user':
                cursor.execute("SELECT username, display_name, avatar_url FROM users WHERE username = ?", (data['target'],))
                row = cursor.fetchone()
                if row:
                    await websocket.send(json.dumps({"type": "search_result", "username": row[0], "display_name": row[1], "avatar_url": row[2]}))
                else:
                    await websocket.send(json.dumps({"type": "error", "message": "Пользователь не найден!"}))

            # --- ОТПРАВКА СООБЩЕНИЯ ---
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
        if websocket in connected_users: del connected_users[websocket]

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handle_client, "0.0.0.0", port, max_size=10**7): # Увеличили лимит для картинок
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
