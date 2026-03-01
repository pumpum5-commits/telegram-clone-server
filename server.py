import asyncio
import websockets
import os

# Храним всех подключенных клиентов
connected_clients = set()

# Обработчик для каждого нового подключения
async def handle_client(websocket): # Убрали аргумент path
    # Регистрируем нового клиента
    connected_clients.add(websocket)
    try:
        # Бесконечный цикл ожидания сообщений от этого клиента
        async for message in websocket:
            print(f"Получено сообщение: {message}")
            # Пересылаем сообщение всем остальным клиентам (Broadcast)
            websockets_to_remove = set()
            for client in connected_clients:
                if client != websocket:
                    try:
                        await client.send(message)
                    except websockets.exceptions.ConnectionClosed:
                        websockets_to_remove.add(client)
            
            # Очищаем отключившихся клиентов
            for closed_client in websockets_to_remove:
                connected_clients.remove(closed_client)

    except websockets.exceptions.ConnectionClosed:
        print("Клиент отключился.")
    finally:
        # Убираем клиента из списка при отключении
        connected_clients.remove(websocket)

# Функция запуска сервера
async def main():
    # Render.com автоматически задает порт через переменную окружения PORT
    # Если переменной нет (запуск на домашнем ПК), используем порт 10000
    port = int(os.environ.get("PORT", 10000))
    
    print(f"Запуск WebSocket сервера на порту {port}...")
    
    # Запускаем сервер
    async with websockets.serve(handle_client, "0.0.0.0", port):
        await asyncio.Future()  # Работаем вечно

if __name__ == "__main__":
    asyncio.run(main())
