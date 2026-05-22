import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Отвечаем хостингу успешным кодом 200 OK
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Haven Bot is running smoothly!")

    def log_message(self, format, *args):
        # Глушим логи запросов в консоли, чтобы они не спамили каждые 5 секунд
        return

def run():
    # Берем порт из переменных окружения хостинга, либо ставим 8000 по умолчанию
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[Keep-Alive] Веб-сервер запущен на порту {port} для обхода Health Check")
    server.serve_forever()

def keep_alive():
    # Запускаем сервер в отдельном потоке (daemon=True), чтобы он не блокировал бота
    t = threading.Thread(target=run, daemon=True)
    t.start()

