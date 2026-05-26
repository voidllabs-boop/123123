import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Respond to hosting with a successful 200 OK
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Uma Enjoyers HQ Bot is running smoothly!")

    def log_message(self, format, *args):
        # Suppress request logs in console so they don't spam every 5 seconds
        return

def run():
    # Get port from hosting environment variables, default to 8000
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[Keep-Alive] Web server started on port {port} for Health Check")
    server.serve_forever()

def keep_alive():
    # Run server in a separate thread (daemon=True) so it doesn't block the bot
    t = threading.Thread(target=run, daemon=True)
    t.start()

