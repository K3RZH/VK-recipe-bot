import argparse
import http.client
import json
import os
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from bot import load_recipes, reply
from intent import DEFAULT_MODEL
from recipe_photos import find_photos


WEB_ROOT = Path(__file__).with_name("miniapp")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/vk-bridge.min.js": ("vendor/vk-bridge.min.js", "text/javascript; charset=utf-8"),
}
POLICY = (
    "default-src 'self'; img-src 'self' https://upload.wikimedia.org https://thumb.wikimedia.org; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru"
)
MAX_REQUEST_BYTES = 4096


class MiniAppServer(ThreadingHTTPServer):
    def __init__(self, port):
        super().__init__(("127.0.0.1", port), MiniAppHandler)
        self.model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
        self.recipes = load_recipes()
        self.inference = Lock()
        self.assets = {
            route: ((WEB_ROOT / path).read_bytes(), mime)
            for route, (path, mime) in ASSETS.items()
        }

    def get_request(self):
        socket, address = super().get_request()
        socket.settimeout(15)
        return socket, address


class MiniAppHandler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, ConnectionError):
            # Браузер может закрыть соединение, пока сертификат не подтверждён.
            pass

    def log_message(self, _format, *_args):
        # В параметрах запуска VK могут быть идентификаторы пользователя.
        pass

    def send_content(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", POLICY)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status, **data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_content(status, body, "application/json; charset=utf-8")

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"localhost:{port}", f"127.0.0.1:{port}"}

    def do_GET(self):
        if not self.allowed_host():
            self.send_json(HTTPStatus.FORBIDDEN, error="Открой приложение через localhost.")
            return
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            self.send_content(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        asset = self.server.assets.get(path)
        if asset is None:
            self.send_json(HTTPStatus.NOT_FOUND, error="Страница не найдена.")
            return
        self.send_content(HTTPStatus.OK, *asset)

    do_HEAD = do_GET

    def do_POST(self):
        origin = self.headers.get("Origin")
        scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        if not self.allowed_host() or (origin and origin != f"{scheme}://{self.headers['Host']}"):
            self.send_json(HTTPStatus.FORBIDDEN, error="Запрос должен идти со страницы приложения.")
            return
        if self.path != "/api/recipes":
            self.send_json(HTTPStatus.NOT_FOUND, error="Адрес запроса не найден.")
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, error="Ожидался JSON.")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise ValueError
            payload = json.loads(self.rfile.read(size))
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, str) or not 0 < len(message.strip()) <= 500:
                raise ValueError
        except (OSError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, error="Введи от 1 до 500 символов.")
            return

        if not self.server.inference.acquire(blocking=False):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, error="Подбор уже идёт. Повтори чуть позже.")
            return
        try:
            selected = []
            answer = reply(message, self.server.model, self.server.recipes, on_recipe=selected.append)
        except (OSError, http.client.HTTPException, RuntimeError, ValueError):
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, error="Подбор сейчас недоступен. Повтори запрос.")
        else:
            self.send_json(HTTPStatus.OK, answer=answer, photos=find_photos(selected))
        finally:
            self.server.inference.release()


def main():
    parser = argparse.ArgumentParser(description="Локальный сервер мини-приложения ВК.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert", help="HTTPS-сертификат для локальной разработки.")
    parser.add_argument("--key", help="Приватный ключ HTTPS-сертификата.")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("Для HTTPS укажи и --cert, и --key.")
    with MiniAppServer(args.port) as server:
        scheme = "https" if args.cert else "http"
        if args.cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.cert, args.key)
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        print(f"Приложение доступно на {scheme}://localhost:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
