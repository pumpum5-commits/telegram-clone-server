import asyncio
import websockets
import json
import sqlite3
import os
import random

# База данных
conn = sqlite3.connect('chat.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, display_name TEXT, avatar_url TEXT, password TEXT, email TEXT)''')
try: cursor.execute("ALTER TABLE users ADD COLUMN password TEXT")
except: pass
try: cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
except: pass

# В таблицу сообщений добавим колонки для файлов
cursor.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, receiver TEXT, text TEXT, file_data TEXT, file_type TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
try: cursor.execute("ALTER TABLE messages ADD COLUMN file_data TEXT")
except: pass
try: cursor.execute("ALTER TABLE messages ADD COLUMN file_type TEXT")
except: pass

cursor.execute('''CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, avatar_url TEXT, owner TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS group_members (group_id INTEGER, username TEXT, UNIQUE(group_id, username))''')
conn.commit()

connected_users = {}

async def broadcast_online_status():
    online_list = list(connected_users.values())
    msg = json.dumps({"type": "online_users", "users": online_list})
    for ws in connected_users:
        try: await ws.send(msg)
        except: pass

async def handle_client(websocket):
    username = None
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # --- ЛОГИН И РЕГИСТРАЦИЯ ---
            if data['type'] == 'login':
                auth_identifier = data['display_name'].strip()
                password = data['password']
                is_register = data.get('is_register', False)
                
                cursor.execute("SELECT username, password, avatar_url, email, display_name FROM users WHERE display_name = ? OR email = ? OR username = ?", (auth_identifier, auth_identifier, auth_identifier))
                user_row = cursor.fetchone()
                
                if is_register:
                    if user_row:
                        await websocket.send(json.dumps({"type": "auth_error", "message": "Пользователь уже существует!"}))
                        continue
                    
                    base_username = '@' + auth_identifier.lower().replace(' ', '')
                    username = base_username
                    while True:
                        cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
                        if not cursor.fetchone(): break
                        username = base_username + str(random.randint(1, 999))
                    
                    cursor.execute("INSERT INTO users (username, display_name, avatar_url, password, email) VALUES (?, ?, '', ?, '')", (username, auth_identifier, password))
                    conn.commit()
                    display_name = auth_identifier
                    avatar_url = ""
                    email = ""
                else:
                    if not user_row:
                        await websocket.send(json.dumps({"type": "auth_error", "message": "Пользователь не найден!"}))
                        continue
                    if user_row[1] != password:
                        await websocket.send(json.dumps({"type": "auth_error", "message": "Неверный пароль!"}))
                        continue
                    username, _, avatar_url, email, display_name = user_row
                
                connected_users[websocket] = username
                await broadcast_online_status()
                
                cursor.execute("SELECT g.id, g.name, g.avatar_url FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.username = ?", (username,))
                my_groups = [{"id": f"#{r[0]}", "name": r[1], "avatar_url": r[2]} for r in cursor.fetchall()]
                group_ids = [g['id'] for g in my_groups]
                
                g_placeholders = ",".join(["?"] * len(group_ids)) if group_ids else "''"
                query = f"""
                    SELECT m.sender, m.receiver, m.text, m.file_data, m.file_type, u.display_name, u.avatar_url
                    FROM messages m LEFT JOIN users u ON m.sender = u.username
                    WHERE m.receiver = ? OR m.sender = ? OR m.receiver IN ({g_placeholders})
                    ORDER BY m.id ASC
                """
                params = [username, username] + group_ids if group_ids else [username, username]
                cursor.execute(query, params)
                
                history_list = [{
                    "sender": r[0], "to": r[1], "text": r[2], 
                    "file_data": r[3], "file_type": r[4],
                    "sender_display": r[5] or r[0], "sender_avatar": r[6] or ""
                } for r in cursor.fetchall()]

                await websocket.send(json.dumps({
                    "type": "login_success", "username": username, "display_name": display_name, 
                    "avatar_url": avatar_url, "email": email, "history": history_list, "groups": my_groups
                }))

            # --- "ПЕЧАТАЕТ..." ---
            elif data['type'] == 'typing':
                out_msg = json.dumps({"type": "typing", "sender": username, "to": data['to']})
                if data['to'].startswith('#'):
                    gid = int(data['to'].replace("#", ""))
                    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (gid,))
                    for row in cursor.fetchall():
                        for ws, uname in connected_users.items():
                            if uname == row[0] and ws != websocket:
                                await ws.send(out_msg)
                else:
                    for ws, uname in connected_users.items():
                        if uname == data['to']: await ws.send(out_msg)

            # --- ГРУППЫ И ПРОФИЛЬ (Без изменений) ---
            elif data['type'] == 'create_group':
                group_name = data['name']
                cursor.execute("INSERT INTO groups (name, avatar_url, owner) VALUES (?, '', ?)", (group_name, username))
                group_id = cursor.lastrowid
                cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, username))
                conn.commit()
                await websocket.send(json.dumps({"type": "group_created", "id": f"#{group_id}", "name": group_name, "avatar_url": ""}))

            elif data['type'] == 'add_to_group':
                target = data['target']
                gid = int(data['group_id'].replace("#", ""))
                cursor.execute("SELECT username FROM users WHERE username = ?", (target,))
                if not cursor.fetchone():
                    await websocket.send(json.dumps({"type": "error", "message": "Пользователь не найден!"}))
                    continue
                cursor.execute("INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)", (gid, target))
                conn.commit()
                cursor.execute("SELECT name, avatar_url FROM groups WHERE id = ?", (gid,))
                g_info = cursor.fetchone()
                if g_info:
                    msg = json.dumps({"type": "group_added", "id": f"#{gid}", "name": g_info[0], "avatar_url": g_info[1]})
                    for ws, uname in connected_users.items():
                        if uname == target: await ws.send(msg)
                await websocket.send(json.dumps({"type": "success", "message": f"{target} добавлен в группу!"}))

            elif data['type'] == 'update_credentials':
                new_user = data.get('new_username')
                new_nick = data.get('display_name')
                new_avatar = data.get('avatar_url')
                if new_user and new_user != username:
                    cursor.execute("SELECT username FROM users WHERE username = ?", (new_user,))
                    if cursor.fetchone():
                        await websocket.send(json.dumps({"type": "error", "message": "Этот ID уже занят!"}))
                        continue
                    cursor.execute("UPDATE users SET username=?, display_name=?, avatar_url=? WHERE username=?", (new_user, new_nick, new_avatar, username))
                    cursor.execute("UPDATE messages SET sender=? WHERE sender=?", (new_user, username))
                    cursor.execute("UPDATE messages SET receiver=? WHERE receiver=?", (new_user, username))
                    cursor.execute("UPDATE group_members SET username=? WHERE username=?", (new_user, username))
                    conn.commit()
                    connected_users[websocket] = new_user
                    username = new_user
                else:
                    cursor.execute("UPDATE users SET display_name=?, avatar_url=? WHERE username=?", (new_nick, new_avatar, username))
                    conn.commit()
                await websocket.send(json.dumps({"type": "credentials_updated", "username": username, "display_name": new_nick, "avatar_url": new_avatar}))
                await broadcast_online_status()

            elif data['type'] == 'update_security':
                cursor.execute("UPDATE users SET password = ?, email = ? WHERE username = ?", (data.get('password'), data.get('email'), username))
                conn.commit()
                await websocket.send(json.dumps({"type": "security_updated"}))

            elif data['type'] == 'search_user':
                cursor.execute("SELECT username, display_name, avatar_url FROM users WHERE username = ?", (data['target'],))
                row = cursor.fetchone()
                if row: await websocket.send(json.dumps({"type": "search_result", "username": row[0], "display_name": row[1], "avatar_url": row[2]}))
                else: await websocket.send(json.dumps({"type": "search_error", "message": "Пользователь не найден!"}))

            elif data['type'] == 'get_user_info':
                cursor.execute("SELECT username, display_name, avatar_url FROM users WHERE username = ?", (data['target'],))
                row = cursor.fetchone()
                if row: await websocket.send(json.dumps({"type": "user_info", "username": row[0], "display_name": row[1], "avatar_url": row[2]}))

            # --- СООБЩЕНИЯ С ФАЙЛАМИ ---
            elif data['type'] == 'message':
                file_data = data.get('file_data', '')
                file_type = data.get('file_type', '')
                text = data.get('text', '')
                
                cursor.execute("INSERT INTO messages (sender, receiver, text, file_data, file_type) VALUES (?, ?, ?, ?, ?)", 
                               (username, data['to'], text, file_data, file_type))
                conn.commit()
                
                cursor.execute("SELECT display_name, avatar_url FROM users WHERE username = ?", (username,))
                uinfo = cursor.fetchone()
                
                out_msg = json.dumps({
                    "type": "message", "sender": username, 
                    "sender_display": uinfo[0] if uinfo else username, 
                    "sender_avatar": uinfo[1] if uinfo else "",
                    "to": data['to'], "text": text,
                    "file_data": file_data, "file_type": file_type
                })
                
                if data['to'].startswith('#'):
                    gid = int(data['to'].replace("#", ""))
                    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (gid,))
                    members = [r[0] for r in cursor.fetchall()]
                    for ws, uname in connected_users.items():
                        if uname in members: await ws.send(out_msg)
                else:
                    for ws, uname in connected_users.items():
                        if uname == data['to'] or ws == websocket: await ws.send(out_msg)

    except websockets.exceptions.ConnectionClosed: pass
    finally:
        if websocket in connected_users: 
            del connected_users[websocket]
            await broadcast_online_status()

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handle_client, "0.0.0.0", port, max_size=5*10**7): # Лимит 50МБ для файлов
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
