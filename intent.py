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


REQUEST_SCHEMA = {
    **SCHEMA,
    "properties": {
        **PROPERTIES,
        "mode": {"enum": ["dish", "pantry", "help"]},
        "dish": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 80}, {"type": "null"}]},
    },
    "required": [*PROPERTIES, "mode", "dish"],
}
MENU_MODES = ("menu", "menu_week", "menu_restaurant")
VK_REQUEST_SCHEMA = {
    **REQUEST_SCHEMA,
    "properties": {**REQUEST_SCHEMA["properties"], "mode": {"enum": ["dish", "pantry", "help", *MENU_MODES]}},
}
REQUEST_RULES = """Разбери кулинарный запрос по смыслу. Исправляй опечатки в названиях.
dish: человек хочет приготовить конкретное блюдо или продукт. В поле dish дай
только название в именительном падеже и явно заданный способ приготовления.
pantry: человек перечисляет продукты, чтобы подобрать рецепт. dish=null.
help: приветствие, просьба без блюда и продуктов или другая тема. dish=null.
ingredients: продукты из списка запасов. Желаемое блюдо и покупки не являются запасами.
excluded: продукты, которые нельзя добавлять. «У меня нет» не означает запрет.
Если запасов или запретов не назвали, укажи []. Не пиши в списке «нет» или «не указано».
can_buy: названия разрешённых покупок или null. max_extra: явный лимит покупок
или null. При отказе от покупок укажи can_buy=[] и max_extra=0.
Названия продуктов пиши в именительном падеже, без количества.

«Камбалу как пожарить?» → {"mode":"dish","dish":"жареная камбала","ingredients":[],"excluded":[],"can_buy":null,"max_extra":null}
«малина кефир, докуплю только мёд» → {"mode":"pantry","dish":null,"ingredients":["малина","кефир"],"excluded":[],"can_buy":["мёд"],"max_extra":null}
«Испеку хлеб без тмина, мука уже есть» → {"mode":"dish","dish":"хлеб в духовке","ingredients":["мука"],"excluded":["тмин"],"can_buy":null,"max_extra":null}

"""
JSON_INSTRUCTION = "Верни только JSON по схеме: "
REQUEST_PROMPT = REQUEST_RULES + JSON_INSTRUCTION + json.dumps(REQUEST_SCHEMA, ensure_ascii=False)
VK_REQUEST_PROMPT = REQUEST_RULES + """Если нужен набор блюд, используй один из режимов:
menu_week — меню на неделю или на семь дней. menu_restaurant — ассортимент ресторана, кафе или бистро.
menu — меню для события, приёма пищи или дня, если не подходит режим выше.
Во всех трёх режимах dish=null. Не предполагай продукты и запреты для меню.
«Покажи подробно борщ» — dish. «Меню на ужин» — menu.
«Сделай меню для кафе» → {"mode":"menu_restaurant","dish":null,"ingredients":[],"excluded":[],"can_buy":null,"max_extra":null}
Учитывай формулировки вроде «распиши завтраки и обеды с понедельника по воскресенье» — menu_week.

""" + JSON_INSTRUCTION + json.dumps(VK_REQUEST_SCHEMA, ensure_ascii=False)


PURCHASE_FLAGS = ("purchase_limit", "purchase_list")
PURCHASE_SCHEMA = {
    "type": "object", "properties": {key: {"type": "boolean"} for key in PURCHASE_FLAGS},
    "required": list(PURCHASE_FLAGS), "additionalProperties": False,
}
PURCHASE_PROMPT = """Отметь только ограничения на покупки, которые пользователь прямо написал.
purchase_limit=true, если он отказался от покупок или ограничил число покупок.
purchase_list=true, если он перечислил продукты, которые готов купить.
Просьба приготовить блюдо, вопрос «что купить», запрет на ингредиент и описание запасов сами по себе дают false.
«Хочу оладьи» → {"purchase_limit":false,"purchase_list":false}
«Есть рис, за продуктами не пойду» → {"purchase_limit":true,"purchase_list":false}
«Оладьи без сахара. Купить могу молоко» → {"purchase_limit":false,"purchase_list":true}
«Докуплю до двух продуктов» → {"purchase_limit":true,"purchase_list":false}
Верни только JSON."""
EMPTY_EXCLUSIONS = {"[]", "нет", "ничего", "никаких", "ничего не указано", "не указано"}


def confirm_purchases(model, message, intent):
    if intent["can_buy"] is None and intent["max_extra"] is None:
        return intent

    flags = json.loads(structured_chat(model, PURCHASE_PROMPT, message, PURCHASE_SCHEMA, max_tokens=64))
    if (not isinstance(flags, dict) or flags.keys() != PURCHASE_SCHEMA["properties"].keys()
            or not all(type(value) is bool for value in flags.values())):
        raise ValueError("Не удалось подтвердить условия покупки из запроса.")
    if not flags["purchase_limit"]:
        intent["max_extra"] = None
    elif intent["max_extra"] is None and intent["can_buy"] == []:
        intent["max_extra"] = 0
    if not flags["purchase_list"]:
        intent["can_buy"] = None
    if intent["max_extra"] == 0:
        intent["can_buy"] = []
    return intent


def understand_request(model, message, *, allow_menu=False):
    schema, prompt = (VK_REQUEST_SCHEMA, VK_REQUEST_PROMPT) if allow_menu else (REQUEST_SCHEMA, REQUEST_PROMPT)
    result = json.loads(structured_chat(model, prompt, message, schema))
    if not isinstance(result, dict) or result.keys() != schema["properties"].keys():
        raise ValueError("Не удалось разобрать запрос по схеме.")
    mode, dish = result.pop("mode"), result.pop("dish")
    if mode not in schema["properties"]["mode"]["enum"]:
        raise ValueError("Неизвестный тип запроса.")
    if mode == "dish" and (not isinstance(dish, str) or not 0 < len(dish.strip()) <= 80):
        raise ValueError("В запросе не определилось название блюда.")
    intent = normalized(result)
    intent["excluded"] = [item for item in intent["excluded"] if item not in EMPTY_EXCLUSIONS]
    if mode in {"dish", "pantry"}:
        intent = confirm_purchases(model, message, intent)
    return mode, dish, intent


def structured_chat(model, prompt, message, schema, *, max_tokens=256):
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
            options={"temperature": 0, "seed": 42, "num_ctx": 2048, "num_predict": max_tokens},
        )
        data = local_chat(11434, "/api/chat", payload)
        answer = data.get("message")
    elif backend == "llama-cpp":
        payload.update(
            response_format={"type": "json_schema", "schema": schema},
            chat_template_kwargs={"enable_thinking": False},
            temperature=0,
            seed=42,
            max_tokens=max_tokens,
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
