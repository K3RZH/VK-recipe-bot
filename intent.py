import http.client
import json
import os


DEFAULT_MODEL = "gemma4:e2b-it-q4_K_M"
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
ingredients — только продукты, которые у пользователя есть сейчас.
excluded — только продукты, которые пользователь просит не добавлять в блюдо.
«Дома нет», «могу докупить» и «покупок не будет» не означают запрет добавлять
продукт в блюдо. Названия готовых блюд и общие слова пропускай.
can_buy — только названия явно разрешённых покупок. Число продуктов, единицы
измерения и слова «покупки» или «позиции» сюда не входят. Без списка верни null.
max_extra — явно заданный максимум покупок. Без числового лимита верни null.
При полном отказе от покупок верни can_buy: [] и max_extra: 0.
При отсутствии продуктов или запретов верни пустые массивы: [].
Каждое название — по-русски, в именительном падеже и единственном числе,
где это естественно («свёкла», «макароны»). Сохраняй вид продукта, если он
указан («копчёная паприка»). Убирай количества. Учитывай последние уточнения.

Пример: «Дома перловка и две свёклы. Из магазина можно только укроп».
{"ingredients":["перловка","свёкла"],"excluded":[],"can_buy":["укроп"],"max_extra":null}

Пример: «Есть кабачок. Кинзу исключи. Сыра дома нет, за покупками не пойду».
{"ingredients":["кабачок"],"excluded":["кинза"],"can_buy":[],"max_extra":0}

Пример: «Хочу пирог. Остался кефир, могу докупить до одного продукта».
{"ingredients":["кефир"],"excluded":[],"can_buy":null,"max_extra":1}

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


def ask_model(model, message):
    return structured_chat(model, PROMPT, message, SCHEMA)


def recipe_target(model, message):
    schema = {
        "type": "object",
        "properties": {"dish": {"type": ["string", "null"], "maxLength": 80}},
        "required": ["dish"],
        "additionalProperties": False,
    }
    prompt = (
        "Выдели блюдо или основной продукт, который пользователь хочет приготовить. "
        "Верни по-русски в именительном падеже. Сохрани явный способ приготовления, "
        "если он указан. Не принимай список имеющихся продуктов за блюдо. "
        "«Хочу сделать лосося, что понадобится?» → {\"dish\":\"лосось\"}. "
        "«Какие продукты нужны для борща?» → {\"dish\":\"борщ\"}. "
        "Если конкретного блюда или продукта нет, верни {\"dish\":null}."
    )
    result = json.loads(structured_chat(model, prompt, message, schema))
    dish = result.get("dish") if isinstance(result, dict) else None
    return dish.strip() if isinstance(dish, str) and 0 < len(dish.strip()) <= 80 else None


def structured_chat(model, prompt, message, schema):
    backend = os.environ.get("RECIPE_MODEL_BACKEND", "ollama")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": message},
        ],
        "stream": False,
    }
    if backend == "ollama":
        payload.update(
            format=schema,
            think=False,
            options={"temperature": 0, "seed": 42, "num_ctx": 2048, "num_predict": 256},
        )
        data = local_chat(11434, "/api/chat", payload)
        answer = data.get("message")
    elif backend == "llama-cpp":
        payload.update(
            response_format={"type": "json_schema", "schema": schema},
            chat_template_kwargs={"enable_thinking": False},
            temperature=0,
            seed=42,
            max_tokens=256,
        )
        data = local_chat(8080, "/v1/chat/completions", payload)
        choices = data.get("choices", [])
        answer = choices[0].get("message") if choices else None
    else:
        raise ValueError("RECIPE_MODEL_BACKEND должен быть ollama или llama-cpp.")
    if not isinstance(answer, dict) or not isinstance(answer.get("content"), str):
        raise RuntimeError("В ответе локальной модели нет текста сообщения.")
    return answer["content"]


def local_chat(port, path, payload):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
    try:
        connection.request(
            "POST", path, json.dumps(payload), {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        data = json.loads(response.read())
        if not isinstance(data, dict):
            raise RuntimeError("Локальный сервер вернул неожиданный ответ.")
        if response.status != 200:
            raise RuntimeError(f"Локальный сервер HTTP {response.status}: {data.get('error', data)}")
        return data
    finally:
        connection.close()
