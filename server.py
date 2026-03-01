import asyncio
import websockets
import json
import sqlite3
import os
import random
import threading

# --- НАСТРОЙКА БАЗЫ ДАННЫХ ---
conn = sqlite3.connect('chat.db', check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

def init_db():
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, display_name TEXT, avatar_url TEXT, password TEXT, email TEXT, bio TEXT)''')
    for col in ["password", "email", "bio"]:
        try: cursor.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")
        except: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, receiver TEXT, text TEXT, file_data TEXT, file_type TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    for col in ["file_data", "file_type"]:
        try: cursor.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT DEFAULT ''")
        except: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, avatar_url TEXT, owner TEXT, public_username TEXT, is_private INTEGER, user_limit INTEGER, bio TEXT)''')
    for col in [("public_username", "TEXT"), ("is_private", "INTEGER"), ("user_limit", "INTEGER"), ("bio", "TEXT")]:
        try: cursor.execute(f"ALTER TABLE groups ADD COLUMN {col[0]} {col[1]} DEFAULT ''")
        except: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS group_members (group_id INTEGER, username TEXT, UNIQUE(group_id, username))''')
    conn.commit()

init_db()
connected_users = {} # ws: username

# Безопасные функции для работы с БД
def db_execute(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        conn.commit()
        return cursor.lastrowid

def db_fetchall(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        return cursor.fetchall()

def db_fetchone(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        return cursor.fetchone()

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
            data = await asyncio.to_thread(json.loads, message)
            
            # --- ЛОГИН И РЕГИСТРАЦИЯ ---
            if data['type'] == 'login':
                auth_identifier = data['display_name'].strip()
                password = data['password']
                is_register = data.get('is_register', False)
                
                user_row = await asyncio.to_thread(db_fetchone, "SELECT username, password, avatar_url, email, display_name, bio FROM users WHERE display_name = ? OR email = ? OR username = ?", (auth_identifier, auth_identifier, auth_identifier))
                
                if is_register:
                    username = data.get('username', '').strip()
                    if not username.startswith('@'): username = '@' + username
                    
                    exists = await asyncio.to_thread(db_fetchone, "SELECT username FROM users WHERE username = ?", (username,))
                    if exists:
                        await websocket.send(json.dumps({"type": "auth_error", "message": "Этот @ID уже занят! Придумайте другой."}))
                        continue
                    
                    await asyncio.to_thread(db_execute, "INSERT INTO users (username, display_name, avatar_url, password, email, bio) VALUES (?, ?, '', ?, '', '')", (username, auth_identifier, password))
                    display_name, avatar_url, email, bio = auth_identifier, "", "", ""
                else:
                    if not user_row:
                        await websocket.send(json.dumps({"type": "auth_error", "message": "Пользователь не найден!"}))
                        continue
                    if user_row[1] != password:
                        await websocket.send(json.dumps({"type": "auth_error", "message": "Неверный пароль!"}))
                        continue
                    username, _, avatar_url, email, display_name, bio = user_row
                
                connected_users[websocket] = username
                await broadcast_online_status()
                
                my_groups_raw = await asyncio.to_thread(db_fetchall, "SELECT g.id, g.name, g.avatar_url, g.public_username FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.username = ?", (username,))
                my_groups = [{"id": f"#{r[0]}", "name": r[1], "avatar_url": r[2], "public_username": r[3]} for r in my_groups_raw]
                group_ids = [g['id'] for g in my_groups]
                
                g_placeholders = ",".join(["?"] * len(group_ids)) if group_ids else "''"
                query = f"""
                    SELECT m.sender, m.receiver, m.text, m.file_data, m.file_type, u.display_name, u.avatar_url
                    FROM messages m LEFT JOIN users u ON m.sender = u.username
                    WHERE m.receiver = ? OR m.sender = ? OR m.receiver IN ({g_placeholders})
                    ORDER BY m.id ASC
                """
                params = [username, username] + group_ids if group_ids else [username, username]
                history_raw = await asyncio.to_thread(db_fetchall, query, params)
                
                history_list = [{
                    "sender": r[0], "to": r[1], "text": r[2], "file_data": r[3], "file_type": r[4],
                    "sender_display": r[5] or r[0], "sender_avatar": r[6] or ""
                } for r in history_raw]

                await websocket.send(json.dumps({
                    "type": "login_success", "username": username, "display_name": display_name, 
                    "avatar_url": avatar_url, "email": email, "bio": bio, "history": history_list, "groups": my_groups
                }))

            # --- "ПЕЧАТАЕТ..." ---
            elif data['type'] == 'typing':
                dname_row = await asyncio.to_thread(db_fetchone, "SELECT display_name FROM users WHERE username = ?", (username,))
                dname = dname_row[0] if dname_row else username
                out_msg = json.dumps({"type": "typing", "sender": username, "sender_display": dname, "to": data['to']})
                
                if data['to'].startswith('#'):
                    gid = int(data['to'].replace("#", ""))
                    members = await asyncio.to_thread(db_fetchall, "SELECT username FROM group_members WHERE group_id = ?", (gid,))
                    member_names = [m[0] for m in members]
                    for ws, uname in connected_users.items():
                        if uname in member_names and ws != websocket: await ws.send(out_msg)
                else:
                    for ws, uname in connected_users.items():
                        if uname == data['to']: await ws.send(out_msg)

            # --- ГРУППЫ ---
            elif data['type'] == 'create_group':
                gid = await asyncio.to_thread(db_execute, "INSERT INTO groups (name, avatar_url, owner, public_username, is_private, user_limit, bio) VALUES (?, '', ?, ?, ?, ?, ?)", 
                                              (data['name'], username, data['username'], data['is_private'], data['limit'], data['bio']))
                await asyncio.to_thread(db_execute, "INSERT INTO group_members (group_id, username) VALUES (?, ?)", (gid, username))
                await websocket.send(json.dumps({"type": "group_created", "id": f"#{gid}", "name": data['name'], "avatar_url": "", "public_username": data['username']}))

            elif data['type'] == 'add_to_group':
                target = data['target']
                gid = int(data['group_id'].replace("#", ""))
                exists = await asyncio.to_thread(db_fetchone, "SELECT username FROM users WHERE username = ?", (target,))
                if not exists:
                    await websocket.send(json.dumps({"type": "error", "message": "Пользователь не найден!"}))
                    continue
                await asyncio.to_thread(db_execute, "INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)", (gid, target))
                g_info = await asyncio.to_thread(db_fetchone, "SELECT name, avatar_url FROM groups WHERE id = ?", (gid,))
                if g_info:
                    msg = json.dumps({"type": "group_added", "id": f"#{gid}", "name": g_info[0], "avatar_url": g_info[1]})
                    for ws, uname in connected_users.items():
                        if uname == target: await ws.send(msg)
                await websocket.send(json.dumps({"type": "success", "message": f"{target} добавлен в группу!"}))

            # --- ОБНОВЛЕНИЯ ---
            elif data['type'] == 'update_my_profile':
                new_user = data.get('username')
                if new_user != username:
                    exists = await asyncio.to_thread(db_fetchone, "SELECT username FROM users WHERE username = ?", (new_user,))
                    if exists:
                        await websocket.send(json.dumps({"type": "error", "message": "Этот ID уже занят!"}))
                        continue
                    await asyncio.to_thread(db_execute, "UPDATE users SET username=?, display_name=?, avatar_url=?, bio=? WHERE username=?", (new_user, data['display_name'], data['avatar_url'], data['bio'], username))
                    await asyncio.to_thread(db_execute, "UPDATE messages SET sender=? WHERE sender=?", (new_user, username))
                    await asyncio.to_thread(db_execute, "UPDATE messages SET receiver=? WHERE receiver=?", (new_user, username))
                    await asyncio.to_thread(db_execute, "UPDATE group_members SET username=? WHERE username=?", (new_user, username))
                    connected_users[websocket] = new_user
                    username = new_user
                else:
                    await asyncio.to_thread(db_execute, "UPDATE users SET display_name=?, avatar_url=?, bio=? WHERE username=?", (data['display_name'], data['avatar_url'], data['bio'], username))
                await websocket.send(json.dumps({"type": "my_profile_updated", "username": username, "display_name": data['display_name'], "avatar_url": data['avatar_url'], "bio": data['bio']}))
                await broadcast_online_status()

            elif data['type'] == 'update_security':
                await asyncio.to_thread(db_execute, "UPDATE users SET password = ?, email = ? WHERE username = ?", (data.get('password'), data.get('email'), username))
                await websocket.send(json.dumps({"type": "security_updated"}))

            # --- ПОИСК ---
            elif data['type'] == 'search_user' or data['type'] == 'get_user_info':
                target = data['target']
                u_row = await asyncio.to_thread(db_fetchone, "SELECT username, display_name, avatar_url, bio FROM users WHERE username = ?", (target,))
                if u_row:
                    await websocket.send(json.dumps({"type": "search_result", "is_group": False, "username": u_row[0], "display_name": u_row[1], "avatar_url": u_row[2], "bio": u_row[3]}))
                else:
                    g_row = await asyncio.to_thread(db_fetchone, "SELECT id, name, avatar_url, bio, public_username FROM groups WHERE public_username = ? OR id = ?", (target, target.replace("#","") if target.startswith('#') else None))
                    if g_row:
                        await websocket.send(json.dumps({"type": "search_result", "is_group": True, "id": f"#{g_row[0]}", "display_name": g_row[1], "avatar_url": g_row[2], "bio": g_row[3], "username": g_row[4]}))
                    else:
                        if data['type'] == 'search_user': await websocket.send(json.dumps({"type": "error", "message": "Не найдено!"}))

            # --- ОТПРАВКА СООБЩЕНИЙ ---
            elif data['type'] == 'message':
                await asyncio.to_thread(db_execute, "INSERT INTO messages (sender, receiver, text, file_data, file_type) VALUES (?, ?, ?, ?, ?)", 
                                        (username, data['to'], data.get('text',''), data.get('file_data',''), data.get('file_type','')))
                
                uinfo = await asyncio.to_thread(db_fetchone, "SELECT display_name, avatar_url FROM users WHERE username = ?", (username,))
                
                out_msg = json.dumps({
                    "type": "message", "sender": username, 
                    "sender_display": uinfo[0] if uinfo else username, 
                    "sender_avatar": uinfo[1] if uinfo else "",
                    "to": data['to'], "text": data.get('text',''),
                    "file_data": data.get('file_data',''), "file_type": data.get('file_type','')
                })
                
                if data['to'].startswith('#'):
                    gid = int(data['to'].replace("#", ""))
                    members = await asyncio.to_thread(db_fetchall, "SELECT username FROM group_members WHERE group_id = ?", (gid,))
                    member_names = [m[0] for m in members]
                    for ws, uname in connected_users.items():
                        if uname in member_names: await ws.send(out_msg)
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
    async with websockets.serve(handle_client, "0.0.0.0", port, max_size=10**8):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
