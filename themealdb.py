import hashlib
import json
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.request import urlopen


CACHE_PATH = Path(__file__).with_name(".recipe-cache") / "themealdb.json"
MEAL_URL = "https://www.themealdb.com/meal/"
LOOKUP_URL = "https://www.themealdb.com/api/json/v1/1/lookup.php?i="
INGREDIENTS = {
    "all purpose flour": "пшеничная мука",
    "baking powder": "разрыхлитель",
    "black pepper": "черный перец",
    "bread": "хлеб",
    "butter": "сливочное масло",
    "chickpea flour": "нутовая мука",
    "cucumber": "огурец",
    "egg": "яйцо",
    "egg plants": "баклажан",
    "eggs": "яйцо",
    "extra virgin olive oil": "оливковое масло",
    "feta": "фета",
    "flour": "пшеничная мука",
    "garlic": "чеснок",
    "garlic clove": "чеснок",
    "green pepper": "зеленый сладкий перец",
    "melted butter": "сливочное масло",
    "milk": "молоко",
    "olive oil": "оливковое масло",
    "onions": "лук",
    "parsley": "петрушка",
    "peanut butter": "арахисовая паста",
    "pepper": "перец",
    "plain flour": "пшеничная мука",
    "potatoes": "картофель",
    "red onions": "красный лук",
    "salt": "соль",
    "sea salt": "соль",
    "spring onions": "зеленый лук",
    "sugar": "сахар",
    "thyme": "тимьян",
    "tomato": "помидор",
    "tomatoes": "помидор",
    "vegetable oil": "растительное масло",
    "vegetable stock": "овощной бульон",
    "water": "вода",
    "yeast": "дрожжи",
}

# Пересказ сверяется с версией инструкций, полученной из API.
MEALS = {
    "52914": ("Картофель буланжер", "ca09be48216c0e69", [
        "Разогрей духовку до 200 °C или 180 °C с конвекцией. Обжарь лук с тимьяном в масле около 5 минут.",
        "В смазанной форме чередуй картофель и лук так, чтобы сверху остался картофель. Влей бульон.",
        "Запекай 50–60 минут до мягкости картофеля и золотистой корочки."
    ]),
    "52958": ("Печенье из арахисовой пасты", "c00604a7fb0b113b", [
        "Разогрей духовку до 180 °C. Смешай арахисовую пасту, сахар и яйцо.",
        "Скатай тесто в шарики, выложи на противень с антипригарным покрытием и слегка приплюсни вилкой.",
        "Выпекай 8–10 минут до золотистого низа. Сними с противня и дай остыть."
    ]),
    "53022": ("Польские блины", "04f4b6e446530682", [
        "Взбей муку, яйца, молоко, воду, соль и сахар до однородности.",
        "Добавь сливочное масло в тесто или используй его для смазывания сковороды.",
        "Распредели тонкий слой теста по разогретой сковороде. Когда верх подсохнет, переверни блин и поджарь с другой стороны."
    ]),
    "53075": ("Омлет с баклажаном", "fa0f318dbd3d41f6", [
        "Запеки баклажаны под грилем до потемнения кожицы. Дай немного остыть и очисти.",
        "Расплющи баклажаны вилкой и обмакни во взбитые с солью яйца.",
        "Обжаривай в масле на среднем огне по 3–4 минуты с каждой стороны до готовности яйца."
    ]),
    "53089": ("Сирийские лепёшки", "545f7699626e84dc", [
        "Загрузи продукты в хлебопечку в порядке, указанном к ней, и включи режим «Тесто».",
        "Разогрей духовку до 245 °C. Раздели тесто на восемь частей и дай им отдохнуть под влажным полотенцем.",
        "Тонко раскатай лепёшки и выпекай на разогретом противне примерно по 5 минут, пока они не поднимутся и не подрумянятся."
    ]),
    "53139": ("Фаина из нутовой муки", "ab6b71fac77b1758", [
        "Смешай нутовую муку, воду, соль и перец. Дай тесту постоять не менее 4 часов.",
        "Разогрей духовку до 220 °C. Прогрей круглую форму с оливковым маслом.",
        "Влей тесто и выпекай 25–30 минут до золотистого цвета."
    ]),
    "53215": ("Шакшука", "fa83dda57faab391", [
        "Прогрей чеснок и чёрный перец на сухой сковороде. Влей оливковое масло и прогрей ещё минуту.",
        "Добавь помидоры и соль, накрой и туши 5 минут. Сними крышку, разомни помидоры и выпари соус до нужной густоты.",
        "Сделай углубления, разбей в них яйца и накрой сковороду. Готовь 5 минут, затем сними с огня и оставь под крышкой ещё 1–2 минуты."
    ]),
    "53401": ("Болани с картофелем", "83f7f0b7b35c7a21", [
        "Замеси тесто из муки, соли и воды. Вымешивай 5–10 минут, затем дай постоять под влажной тканью не менее получаса.",
        "Отвари картофель и разомни. Добавь зелёный лук, соль и чёрный перец.",
        "Тонко раскатай тесто, вырежи круги, разложи начинку и защипни края. Обжарь в растительном масле с обеих сторон."
    ]),
    "53417": ("Каталонские тосты с помидором", "52da0c1133cad7e8", [
        "Подсуши хлеб до хрустящей золотистой корочки.",
        "Натри тосты срезом чеснока, затем срезом помидора.",
        "Полей оливковым маслом, посоли и подавай тёплыми."
    ]),
    "53491": ("Шопский салат", "e04843bd2b550879", [
        "Смешай огурец, помидоры, красный лук и зелёный сладкий перец. Посоли и оставь на 5 минут.",
        "Заправь оливковым маслом. Добавь фету и петрушку."
    ]),
    "53516": ("Чилийские лепёшки добладитас", "da3764438dc1ba8e", [
        "Разогрей духовку до 230 °C и смажь противень. Замеси муку, чашку растопленного масла, молоко, разрыхлитель и соль.",
        "Тонко раскатай тесто, вырежи круги и смажь оставшимся растопленным маслом. Дважды сложи каждый круг пополам и прижми края.",
        "Выпекай около 15 минут до золотистого цвета."
    ]),
}
MEASURE_WORDS = {
    "finely chopped": "мелко нарезать",
    "sprigs of fresh": "свежие веточки",
    "for frying": "для жарки",
    "to taste": "по вкусу",
    "pinch": "щепотка",
    "splash": "немного",
    "teaspoon": "ч. л.",
    "tsp": "ч. л.",
    "tablespoons": "ст. л.",
    "tblsp": "ст. л.",
    "tbs": "ст. л.",
    "cups": "чаш.",
    "cup": "чаш.",
    "sliced": "нарезать ломтиками",
    "chopped": "нарезать",
    "cloves": "зубчика",
}
MEASURE_PATTERN = re.compile(r"\b(?:" + "|".join(MEASURE_WORDS) + r")\b", re.IGNORECASE)
METRIC_UNITS = {"kg": "кг", "g": "г", "ml": "мл"}


def read_meal(meal_id):
    with urlopen(LOOKUP_URL + meal_id, timeout=12) as response:
        items = json.loads(response.read(250_000)).get("meals")
    if not items or items[0].get("idMeal") != meal_id:
        raise ValueError(f"TheMealDB не вернул рецепт {meal_id}.")
    return items[0]


def translate_measure(amount):
    amount = " ".join(amount.split()).replace(" and ", " ")
    amount = re.sub(
        r"(?<=\d)\s*(kg|ml|g)\b",
        lambda match: " " + METRIC_UNITS[match[1].lower()],
        amount,
        flags=re.IGNORECASE,
    )
    return MEASURE_PATTERN.sub(lambda match: MEASURE_WORDS[match[0].lower()], amount)


def convert_meal(meal, title, revision, steps):
    instructions = " ".join((meal.get("strInstructions") or "").split())
    if hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:16] != revision:
        raise ValueError("В источнике изменились шаги; нужно сверить русский текст.")
    ingredients = {}
    for number in range(1, 21):
        name = (meal.get(f"strIngredient{number}") or "").strip()
        if not name:
            continue
        key = INGREDIENTS.get(name.casefold())
        if key is None:
            raise ValueError(f"Не сопоставлен ингредиент {name}.")
        amount = translate_measure(meal.get(f"strMeasure{number}") or "") or "количество не указано"
        ingredients[key] = f"{ingredients[key]} + {amount}" if key in ingredients else amount
    if not ingredients:
        raise ValueError("В источнике пустой список продуктов.")
    return {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "source": MEAL_URL + meal["idMeal"],
    }


def main():
    recipes = []
    for meal_id, locale in MEALS.items():
        try:
            recipe = convert_meal(read_meal(meal_id), *locale)
        except (OSError, URLError, TypeError, ValueError) as error:
            print(f"{meal_id}: {error} Импорт остановлен, прежняя подборка сохранена.", file=sys.stderr)
            return 1
        recipes.append(recipe)

    CACHE_PATH.parent.mkdir(exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_PATH.parent, delete=False) as output:
        json.dump(recipes, output, ensure_ascii=False, indent=2)
        output.write("\n")
    Path(output.name).replace(CACHE_PATH)
    print(f"Импортировано рецептов из TheMealDB: {len(recipes)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
