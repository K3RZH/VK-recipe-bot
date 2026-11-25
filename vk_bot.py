import hashlib
import http.client
import json
import os
import sys
import time
from urllib.parse import urlencode, urlsplit

from bot import load_recipes, reply
from intent import DEFAULT_MODEL


API_HOST = "api.vk.com"
API_VERSION = "5.199"
WAIT_SECONDS = 25
IO_ERRORS = (OSError, http.client.HTTPException, ValueError)


class VkError(RuntimeError):
    """VK отклонил метод API или вернул неожиданные данные."""


def request_json(host, method, path, *, fields=None, timeout=15):
    connection = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        body = urlencode(fields) if fields is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status >= 500 or response.status == 429:
            raise OSError(f"VK временно недоступен (HTTP {response.status}).")
        if response.status != 200:
            raise VkError(f"VK вернул HTTP {response.status}.")
        result = json.loads(response.read())
        if not isinstance(result, dict):
            raise VkError("Ожидался JSON-объект в ответе VK.")
        return result
    finally:
        connection.close()


class VkClient:
    def __init__(self, token, group_id):
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
            code = response["error"]["error_code"]
            if code == 6:
                raise OSError("Слишком много запросов к VK.")
            raise VkError(f"{method}: ошибка VK {code}.")
        return response["response"]

    def listen(self):
        server = None
        while True:
            try:
                if server is None:
                    server = self.call("groups.getLongPollServer", group_id=self.group_id)
                    address = urlsplit(server["server"])
                    if address.scheme != "https" or address.netloc not in {
                        "lp.vk.com", "lp.vk.ru",
                    }:
                        raise VkError("VK вернул неизвестный адрес Long Poll.")
                    ts = server["ts"]
                query = urlencode({
                    "act": "a_check",
                    "key": server["key"],
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
                    ts = result["ts"]
                elif failed is None:
                    yield from result["updates"]
                    ts = result["ts"]
                else:
                    raise VkError(f"Long Poll: неизвестный код ошибки {failed}.")
            except IO_ERRORS:
                print("Связь с VK прервалась, повторяю запрос через 3 секунды.", file=sys.stderr)
                time.sleep(3)

    def send(self, peer_id, conversation_message_id, text):
        event_id = f"{peer_id}:{conversation_message_id}".encode("ascii")
        digest = hashlib.blake2s(event_id, digest_size=4).digest()
        random_id = int.from_bytes(digest, "big") % (2**31 - 1) + 1
        self.call("messages.send", peer_id=peer_id, random_id=random_id, message=text)


def handle_update(vk, update, model, recipes):
    if update.get("type") != "message_new" or update.get("group_id") != vk.group_id:
        return
    message = update.get("object", {}).get("message", {})
    peer_id = message.get("peer_id")
    message_id = message.get("conversation_message_id")
    if (
        type(peer_id) is not int or peer_id <= 0 or peer_id != message.get("from_id")
        or type(message_id) is not int or message_id <= 0
    ):
        return
    try:
        answer = reply(message.get("text", ""), model, recipes)
    except (OSError, http.client.HTTPException, ValueError, RuntimeError):
        print("Локальная модель не обработала сообщение.", file=sys.stderr)
        answer = "Не удалось разобрать запрос. Попробуй отправить его ещё раз чуть позже."

    for attempt in range(2):
        try:
            vk.send(peer_id, message_id, answer)
            return
        except IO_ERRORS:
            if attempt == 0:
                time.sleep(3)
        except VkError as error:
            print(error, file=sys.stderr)
            return
    print("Не удалось отправить ответ в VK после двух попыток.", file=sys.stderr)


def main():
    token = os.environ.get("VK_GROUP_TOKEN", "").strip()
    group = os.environ.get("VK_GROUP_ID", "").strip()
    if not token or not group.isascii() or not group.isdecimal() or int(group) == 0:
        print("Задай VK_GROUP_TOKEN и положительный VK_GROUP_ID в окружении.", file=sys.stderr)
        return 1
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    vk = VkClient(token, int(group))
    try:
        recipes = load_recipes()
        print("Ожидаю сообщения сообщества VK.", flush=True)
        for update in vk.listen():
            handle_update(vk, update, model, recipes)
    except KeyboardInterrupt:
        return 0
    except (VkError, OSError, ValueError, KeyError) as error:
        print(f"Бот остановлен: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
