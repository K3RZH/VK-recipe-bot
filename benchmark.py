import argparse
import http.client
import json
import sys
from collections import Counter
from pathlib import Path


STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1}}
PROPERTIES = {
    "ingredients": STRINGS,
    "excluded": STRINGS,
    "can_buy": {"anyOf": [STRINGS, {"type": "null"}]},
    "max_extra": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
}
SCHEMA = {
    "type": "object",
    "properties": PROPERTIES,
    "required": list(PROPERTIES),
    "additionalProperties": False,
}
PROMPT = """Извлеки из сообщения пользователя данные для поиска рецепта.
Используй только то, что сказано в сообщении. Указывай названия по-русски,
в именительном падеже и в единственном числе, если так говорят об этом
продукте. Например: «помидоров» → «помидор», «макароны» → «макароны».
ingredients — продукты, которые уже есть. Убери количества и единицы измерения.
excluded — продукты и группы продуктов, которые пользователь исключает.
can_buy — список продуктов, который пользователь явно разрешает докупить.
Без такого списка верни null. При полном запрете покупок верни [].
max_extra — максимум продуктов, которые можно докупить: «до двух» → 2,
«без покупок» → 0. Без числового лимита или запрета верни null.
Для ingredients и excluded используй [] при отсутствии данных.
Если пользователь передумал, учитывай последнее уточнение.
Верни JSON по схеме:
""" + json.dumps(SCHEMA, ensure_ascii=False)


def normalized(result):
    if not isinstance(result, dict) or result.keys() != PROPERTIES.keys():
        raise ValueError("Ожидался объект с четырьмя полями схемы.")
    values = result.copy()
    for field in ("ingredients", "excluded", "can_buy"):
        items = result[field]
        if field == "can_buy" and items is None:
            continue
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item.strip() for item in items
        ):
            raise ValueError(f"{field}: ожидался список непустых строк.")
        values[field] = sorted(
            {item.strip().casefold().replace("ё", "е") for item in items}
        )
    limit = result["max_extra"]
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("max_extra: ожидалось целое неотрицательное число или null.")
    return values


def load_cases():
    dataset = Path(__file__).with_name("eval.jsonl")
    cases = []
    seen = set()
    for number, line in enumerate(dataset.read_text(encoding="utf-8").splitlines(), 1):
        try:
            case = json.loads(line)
            message = case["text"]
            if not isinstance(message, str) or not message.strip():
                raise ValueError("Пустой запрос.")
            if message.strip().casefold() in seen:
                raise ValueError("Повторяющийся запрос.")
            seen.add(message.strip().casefold())
            cases.append((message, normalized(case["expected"])))
        except (TypeError, KeyError, ValueError) as error:
            raise ValueError(f"{dataset.name}, строка {number}: {error}") from error
    if not cases:
        raise ValueError(f"{dataset.name}: нет примеров.")
    return cases


def ask_model(model, message):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": message},
        ],
        "format": SCHEMA,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "seed": 42, "num_ctx": 2048, "num_predict": 256},
    })
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=180)
    try:
        connection.request("POST", "/api/chat", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        if not isinstance(data, dict):
            raise RuntimeError("Ollama вернул неожиданный ответ.")
        if response.status != 200:
            raise RuntimeError(f"Ollama HTTP {response.status}: {data.get('error', data)}")
        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise RuntimeError("В ответе Ollama нет текста сообщения.")
        return message["content"]
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Проверка разбора запросов о рецептах.")
    parser.add_argument("--model", default="gemma4:e2b-it-q4_K_M", help="Имя модели в Ollama.")
    parser.add_argument("--validate-data", action="store_true", help="Проверить файл примеров.")
    args = parser.parse_args()

    try:
        cases = load_cases()
        if args.validate_data:
            print(f"Примеров с корректной схемой: {len(cases)}")
            return 0
        matches = Counter()
        for number, (message, expected) in enumerate(cases, 1):
            answer = ask_model(args.model, message)
            try:
                actual = normalized(json.loads(answer))
            except ValueError as error:
                print(f"[{number}] Ошибка разбора: {message} ({error})", flush=True)
                continue
            matches["valid"] += 1
            wrong = [field for field in PROPERTIES if actual[field] != expected[field]]
            if wrong:
                print(f"[{number}] {message}", flush=True)
            else:
                matches["exact"] += 1
            for field in PROPERTIES:
                if field in wrong:
                    print(f"  {field}: ожидалось {expected[field]!r}, получено {actual[field]!r}")
                else:
                    matches[field] += 1
        print(f"Модель: {args.model}")
        print(f"Корректный формат: {matches['valid']}/{len(cases)}")
        print(f"Полностью верно: {matches['exact']}/{len(cases)}")
        for field in PROPERTIES:
            print(f"{field}: {matches[field]}/{len(cases)}")
        return 0
    except (OSError, http.client.HTTPException, KeyError, RuntimeError, ValueError) as error:
        print(f"Проверка остановилась: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
