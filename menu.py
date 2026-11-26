import json
import re

from intent import structured_chat


DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
MEALS = ("Завтрак", "Обед", "Ужин")
MENU_TITLES = {"menu": "Меню", "menu_week": "Меню на неделю", "menu_restaurant": "Меню ресторана"}
DISH_LIMIT = 56
PROMPT = (
    "Подбери названия известных блюд по запросу пользователя. "
    "Учитывай кухню, продукты, исключения и повод. Выбирай названия, по которым можно найти рецепт. "
    "Используй привычные названия блюд. Для национальной кухни предложи классические варианты. "
    "Не добавляй новые ингредиенты к классическому названию и не объединяй два блюда в одно название. "
    "Пиши по-русски. Указывай только названия блюд, без рецептов, цен, ссылок и комментариев. "
    "В одном разделе давай разные блюда. Возвращай JSON. "
)
RULES = {
    "menu_week": "Нужно ровно семь разделов по дням с понедельника. В каждом три блюда: завтрак, обед, ужин. "
                 "Используй разные блюда на протяжении недели.",
    "menu_restaurant": "Собери один–четыре раздела по типам блюд, как в ресторанном меню. "
                       "По умолчанию четыре раздела по три блюда. Если задана тематика, подбери подходящие разделы.",
    "menu": "Собери меню для указанного повода или времени дня. Назови разделы по типу блюда или приёму пищи.",
}


def menu_schema(mode):
    weekly = mode == "menu_week"
    dish = {"type": "string", "minLength": 2, "maxLength": DISH_LIMIT}
    section = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 2, "maxLength": 36},
            "dishes": {"type": "array", "items": dish, "minItems": 3 if weekly else 1, "maxItems": 3},
        },
        "required": ["title", "dishes"], "additionalProperties": False,
    }
    return {
        "type": "object", "required": ["sections"], "additionalProperties": False,
        "properties": {"sections": {"type": "array", "items": section,
                                    "minItems": 7 if weekly else 1, "maxItems": 7 if weekly else 4}},
    }


def dish_name(value):
    if not isinstance(value, str):
        raise ValueError("Ожидалось название блюда.")
    name = " ".join(re.sub(r"[,«»„“”\"()–—]", " ", value).split())
    if not 2 <= len(name) <= DISH_LIMIT or not re.fullmatch(r"[А-Яа-яЁё -]+", name) or not name[0].isalpha():
        raise ValueError("Название блюда должно подходить для поиска.")
    return name


def generate_menu(model, message, mode):
    schema = menu_schema(mode)
    prompt = PROMPT + RULES[mode] + " В названиях блюд до 56 символов, только буквы, пробелы и дефисы."
    result = json.loads(structured_chat(model, prompt, message, schema, max_tokens=950))
    sections = result.get("sections") if isinstance(result, dict) else None
    limits = schema["properties"]["sections"]
    if not isinstance(sections, list) or not limits["minItems"] <= len(sections) <= limits["maxItems"]:
        raise ValueError("Не удалось собрать нужное число разделов меню.")
    menu = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict) or not isinstance(section.get("title"), str):
            raise ValueError("Ожидался заголовок раздела меню.")
        dishes = section.get("dishes")
        weekly = mode == "menu_week"
        if not isinstance(dishes, list) or not (3 if weekly else 1) <= len(dishes) <= 3:
            raise ValueError("Неверное число блюд в разделе меню.")
        title = DAYS[index] if weekly else " ".join(section["title"].split())
        if not title or len(title) > 36:
            raise ValueError("Некорректный заголовок раздела меню.")
        labels = MEALS if weekly else (None,) * len(dishes)
        menu.append((title, [(label, dish_name(dish)) for label, dish in zip(labels, dishes)]))
    return menu
