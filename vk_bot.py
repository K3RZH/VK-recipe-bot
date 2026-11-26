import argparse
import getpass
import hashlib
import http.client
import json
import os
import secrets
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from bot import describe_recipe, load_recipes, reply
from intent import DEFAULT_MODEL
from menu import dish_name
from recipe_photos import IMAGE_HOSTS, find_photos, photo_url


API_HOST = "api.vk.com"
API_VERSION = "5.199"
WAIT_SECONDS = 25
IO_ERRORS = (OSError, http.client.HTTPException, ValueError)
LONG_POLL_HOSTS = {"lp.vk.com", "lp.vk.ru"}
UPLOAD_DOMAINS = (".vk.com", ".vk.ru", ".userapi.com", ".vk-cdn.net")
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
PHOTO_LIMIT = 5 * 1024 * 1024
MESSAGE_LIMIT = 4000
MENU_BUTTONS = 6
PAYLOAD_LIMIT = 255


class VkError(RuntimeError):
    """VK отклонил метод API или вернул неожиданные данные."""


def request_json(host, method, path, *, fields=None, body=None, content_type=None, timeout=15):
    connection = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        if fields is not None:
            body = urlencode(fields)
            content_type = "application/x-www-form-urlencoded"
        headers = {"Content-Type": content_type} if content_type else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status >= 500 or response.status == 429:
            raise OSError(f"VK временно недоступен (HTTP {response.status}).")
        if response.status != 200:
            raise VkError(f"VK вернул HTTP {response.status}.")
        payload = response.read(4_000_001)
        if len(payload) > 4_000_000:
            raise VkError("Ответ VK превысил лимит размера.")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise VkError("Ожидался JSON-объект в ответе VK.")
        return result
    finally:
        connection.close()


def vk_address(url):
    address = urlsplit(url) if isinstance(url, str) else urlsplit("")
    if (address.scheme != "https" or not address.hostname or address.netloc != address.hostname
            or address.fragment):
        raise VkError("VK вернул некорректный HTTPS-адрес.")
    return address


def read_photo(src):
    url = photo_url(src, IMAGE_HOSTS)
    if not url:
        raise VkError("Неизвестный адрес изображения.")
    address = urlsplit(url)
    connection = http.client.HTTPSConnection(address.hostname, timeout=6)
    try:
        connection.request("GET", address.path, headers={"User-Agent": "RecipeIdea/0.1"})
        response = connection.getresponse()
        content_type = response.headers.get_content_type()
        if response.status != 200 or content_type not in PHOTO_TYPES:
            raise VkError("Изображение недоступно или используется другой формат.")
        image = response.read(PHOTO_LIMIT + 1)
        if not 0 < len(image) <= PHOTO_LIMIT:
            raise VkError("Недопустимый размер изображения.")
        return image, content_type
    finally:
        connection.close()


def message_parts(text):
    while len(text) > MESSAGE_LIMIT:
        stop = text.rfind("\n", 1, MESSAGE_LIMIT + 1)
        if stop < MESSAGE_LIMIT // 2:
            stop = MESSAGE_LIMIT
        yield text[:stop]
        text = text[stop:].lstrip("\n")
    if text:
        yield text


def menu_cards(heading, sections, excluded):
    cards = []
    lines, buttons = [heading, "Нажми кнопку с номером блюда, чтобы открыть рецепт."], []
    number = 0
    for title, dishes in sections:
        if len(buttons) + len(dishes) > MENU_BUTTONS:
            cards.append(("\n".join(lines), buttons))
            lines, buttons = [heading], []
        lines.extend(("", title))
        for meal, dish in dishes:
            number += 1
            description = f"{meal} · {dish}" if meal else dish
            lines.append(f"{number}. {description}")
            payload = json.dumps({"r": dish, "x": excluded}, ensure_ascii=False, separators=(",", ":"))
            if len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
                raise ValueError("Список исключений слишком длинный для кнопки ВК.")
            buttons.append({"color": "primary", "action": {
                "type": "text", "label": f"Покажи подробно №{number}", "payload": payload,
            }})
    cards.append(("\n".join(lines), buttons))
    return [(text, [buttons[start:start + 2] for start in range(0, len(buttons), 2)]) for text, buttons in cards]


def menu_choice(payload):
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
        return None
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or value.keys() != {"r", "x"}:
            return None
        excluded = value["x"]
        if not isinstance(excluded, list) or not all(isinstance(item, str) and item.strip() for item in excluded):
            return None
        return dish_name(value["r"]), excluded
    except (ValueError, RecursionError):
        return None


class VkClient:
    def __init__(self, token, group_id=None):
        self.token = token
        self.group_id = group_id

    def call(self, method, **params):
        response = request_json(
            API_HOST,
            "POST",
            f"/method/{method}",
            fields={**params, "access_token": self.token, "v": API_VERSION},
        )
        if "error" in response:
            error = response["error"]
            code = error.get("error_code") if isinstance(error, dict) else None
            if code in (6, 10):
                raise OSError(f"{method}: временная ошибка VK {code}.")
            if code in (5, 7, 15, 27):
                raise VkError(f"{method}: ошибка VK {code}. Проверь ключ сообщества, ID и права доступа.")
            if method == "groups.getLongPollServer" and code == 100:
                raise VkError("VK не выдал адрес Bots Long Poll (ошибка 100). Проверь ID и настройки Long Poll.")
            if type(code) is not int:
                raise VkError(f"{method}: VK вернул ошибку без кода.")
            raise VkError(f"{method}: ошибка VK {code}.")
        if "response" not in response:
            raise VkError(f"{method}: в ответе VK отсутствуют данные.")
        return response["response"]

    def resolve_group_id(self):
        response = self.call("groups.getById")
        groups = response.get("groups") if isinstance(response, dict) else response
        if isinstance(groups, list) and len(groups) == 1 and isinstance(groups[0], dict):
            group_id = groups[0].get("id")
            if type(group_id) is int and group_id > 0:
                return group_id
        raise VkError("VK не определил сообщество по ключу. Укажи его числовой ID явно.")

    def long_poll_server(self):
        server = self.call("groups.getLongPollServer", group_id=self.group_id)
        if not isinstance(server, dict) or not server.get("key") or "ts" not in server:
            raise VkError("VK не вернул данные Bots Long Poll.")
        address = vk_address(server.get("server"))
        if address.hostname not in LONG_POLL_HOSTS:
            raise VkError("VK вернул неизвестный адрес Bots Long Poll.")
        return address, server["key"], server["ts"]

    def listen(self):
        server = None
        connected = False
        while True:
            try:
                if server is None:
                    server = self.long_poll_server()
                    connected = False
                address, key, ts = server
                query = urlencode({
                    "act": "a_check",
                    "key": key,
                    "ts": ts,
                    "wait": WAIT_SECONDS,
                })
                result = request_json(
                    address.netloc, "GET", f"{address.path or '/'}?{query}", timeout=35
                )
                failed = result.get("failed")
                if failed in (2, 3):
                    server = None
                elif failed == 1:
                    server = address, key, result["ts"]
                elif failed is None:
                    server = address, key, result["ts"]
                    if not connected:
                        print(f"Подключено сообщество {self.group_id}. Ожидаю личные сообщения.", flush=True)
                        connected = True
                    yield from result["updates"]
                else:
                    raise VkError(f"Long Poll: неизвестный код ошибки {failed}.")
            except IO_ERRORS:
                print("Связь с VK прервалась, повторяю запрос через 3 секунды.", file=sys.stderr)
                time.sleep(3)

    def upload_photo(self, peer_id, src):
        image, content_type = read_photo(src)
        server = self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        address = vk_address(server.get("upload_url") if isinstance(server, dict) else None)
        if not address.hostname.endswith(UPLOAD_DOMAINS):
            raise VkError("VK вернул неизвестный адрес загрузки фото.")
        boundary = secrets.token_hex(24)
        extension = PHOTO_TYPES[content_type]
        header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="recipe.{extension}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("ascii")
        form = header + image + f"\r\n--{boundary}--\r\n".encode("ascii")
        path = urlunsplit(("", "", address.path or "/", address.query, ""))
        uploaded = request_json(address.hostname, "POST", path,
                                body=form, content_type=f"multipart/form-data; boundary={boundary}")
        if not all(uploaded.get(key) for key in ("server", "photo", "hash")) or uploaded["photo"] == "[]":
            raise VkError("VK не принял файл изображения.")
        saved = self.call("photos.saveMessagesPhoto", server=uploaded["server"],
                          photo=uploaded["photo"], hash=uploaded["hash"])
        if not isinstance(saved, list) or not saved or not isinstance(saved[0], dict):
            raise VkError("VK не сохранил фотографию.")
        owner_id, photo_id = saved[0].get("owner_id"), saved[0].get("id")
        if type(owner_id) is not int or type(photo_id) is not int:
            raise VkError("VK не вернул идентификаторы фотографии.")
        attachment = f"photo{owner_id}_{photo_id}"
        access_key = saved[0].get("access_key")
        return f"{attachment}_{access_key}" if access_key else attachment

    def send(self, peer_id, conversation_message_id, text, attachments=(), *, buttons=(), offset=0):
        parts = list(message_parts(text))
        for index, part in enumerate(parts):
            event_id = f"{peer_id}:{conversation_message_id}"
            if offset + index:
                event_id += f":{offset + index}"
            digest = hashlib.blake2s(event_id.encode("ascii"), digest_size=4).digest()
            random_id = int.from_bytes(digest, "big") % (2**31 - 1) + 1
            params = {"peer_id": peer_id, "random_id": random_id, "message": part}
            if attachments and index == len(parts) - 1:
                params["attachment"] = ",".join(attachments)
            if buttons and index == len(parts) - 1:
                params["keyboard"] = json.dumps(
                    {"one_time": False, "inline": True, "buttons": buttons}, ensure_ascii=False
                )
            try:
                self.call("messages.send", **params)
            except IO_ERRORS:
                time.sleep(3)
                self.call("messages.send", **params)


def handle_update(vk, update, model, recipes):
    if not isinstance(update, dict) or update.get("type") != "message_new" or update.get("group_id") != vk.group_id:
        return
    event = update.get("object")
    message = event.get("message") if isinstance(event, dict) else None
    if not isinstance(message, dict):
        return
    peer_id = message.get("peer_id")
    message_id = message.get("conversation_message_id")
    text = message.get("text", "")
    if (
        type(peer_id) is not int or peer_id <= 0 or peer_id != message.get("from_id")
        or type(message_id) is not int or message_id <= 0 or message.get("out")
        or not isinstance(text, str)
    ):
        return
    selected = []
    menu = []

    def accept_menu(sections, excluded):
        menu.append((sections, excluded))

    try:
        choice = menu_choice(message.get("payload"))
        if choice:
            dish, excluded = choice
            intent = {"excluded": excluded, "ingredients": [], "can_buy": None, "max_extra": None}
            answer = describe_recipe(intent, dish, model, recipes, selected.append, from_menu=True)
        else:
            answer = reply(text, model, recipes, on_recipe=selected.append, on_menu=accept_menu)
    except (OSError, http.client.HTTPException, ValueError, RuntimeError):
        print("Локальная модель не обработала сообщение.", file=sys.stderr)
        answer = "Не удалось разобрать запрос. Попробуй отправить его ещё раз чуть позже."
        selected.clear()
    if menu:
        try:
            cards = menu_cards(answer, *menu[0])
        except ValueError:
            answer = "Список исключений слишком длинный для кнопок. Сократи его и повтори запрос."
        else:
            try:
                for offset, (page, buttons) in enumerate(cards):
                    vk.send(peer_id, message_id, page, buttons=buttons, offset=offset)
            except IO_ERRORS:
                print("Не удалось отправить меню в VK.", file=sys.stderr)
            except VkError as error:
                print(f"Не удалось отправить меню в VK. {error}", file=sys.stderr)
            return

    attachments = []
    photo_pages = []
    for photo in find_photos(selected):
        try:
            attachments.append(vk.upload_photo(peer_id, photo["src"]))
            photo_pages.append(photo["page"])
        except (*IO_ERRORS, VkError):
            print("Фото недоступно для отправки в VK.", file=sys.stderr)
    photo_buttons = [
        [{"action": {"type": "open_link", "label": "Автор и лицензия фото" if len(photo_pages) == 1
                     else f"Фото {number} · автор и лицензия", "link": page}}]
        for number, page in enumerate(photo_pages, 1)
    ]
    try:
        vk.send(peer_id, message_id, answer, attachments, buttons=photo_buttons)
    except IO_ERRORS:
        print("Не удалось отправить ответ в VK после двух попыток.", file=sys.stderr)
    except VkError as error:
        print(error, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Бот рецептов в личных сообщениях сообщества ВК.")
    parser.add_argument("--interactive", action="store_true", help="Запросить ID и ключ в терминале.")
    parser.add_argument("--check", action="store_true", help="Проверить доступ к Bots Long Poll и выйти.")
    args = parser.parse_args()
    try:
        group = os.environ.get("VK_GROUP_ID", "").strip()
        token = os.environ.get("VK_GROUP_TOKEN", "").strip()
        if args.interactive and not group:
            group = input("ID сообщества ВК (Enter для определения по ключу): ").strip()
        if group and not (group.isascii() and group.isdecimal() and len(group) <= 19 and int(group) > 0):
            raise VkError("Укажи положительный числовой ID сообщества через --interactive или VK_GROUP_ID.")
        if args.interactive and not token:
            if not sys.stdin.isatty():
                raise VkError("Для скрытого ввода ключа открой терминал.")
            token = getpass.getpass("Ключ сообщества ВК: ").strip()
        if not token:
            raise VkError("Введи ключ сообщества через --interactive или VK_GROUP_TOKEN.")
        vk = VkClient(token, int(group) if group else None)
        if vk.group_id is None:
            vk.group_id = vk.resolve_group_id()
        if args.check:
            vk.long_poll_server()
            print("VK принял ключ и вернул адрес Bots Long Poll.")
            return 0
        model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
        recipes = load_recipes()
        for update in vk.listen():
            handle_update(vk, update, model, recipes)
    except (KeyboardInterrupt, EOFError):
        return 0
    except VkError as error:
        print(f"Бот остановлен: {error}", file=sys.stderr)
        return 1
    except (*IO_ERRORS, KeyError):
        print("Бот остановлен. Проверь связь с VK и доступность каталога рецептов.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
