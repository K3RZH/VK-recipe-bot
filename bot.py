import argparse
import http.client
import json
import sys
from pathlib import Path

from intent import DEFAULT_MODEL, understand_request
from menu import MENU_TITLES, generate_menu
from themealdb import CACHE_PATH
from web_recipes import cooking_priority, search, stems, summarize


HELP = (
    "Напиши, какие продукты у тебя есть, что исключить и сколько продуктов можно "
    "докупить. Или спроси про блюдо: «Хочу приготовить лосося. Что понадобится?»."
)
ALIASES = {
    "яйца": "яйцо",
    "куриное яйцо": "яйцо",
    "помидоры": "помидор",
    "томат": "помидор",
    "морковка": "морковь",
    "картошка": "картофель",
    "батон": "хлеб",
    "белый хлеб": "хлеб",
    "масло сливочное": "сливочное масло",
    "масло растительное": "растительное масло",
    "подсолнечное масло": "растительное масло",
    "молочные продукты": "молочный продукт",
    "молочка": "молочный продукт",
    "мука": "пшеничная мука",
    "мука пшеничная": "пшеничная мука",
    "масло оливковое": "оливковое масло",
    "лук репчатый": "лук",
    "баклажаны": "баклажан",
    "арахисовое масло": "арахисовая паста",
    "макаронные изделия": "макароны",
    "тушенка свиная": "свиная тушенка",
    "тушенка говяжья": "говяжья тушенка",
    "говядина тушеная": "говяжья тушенка",
    "масло сливочное несоленое": "сливочное масло",
    "поваренная соль": "соль",
    "перец черный молотый": "черный перец",
}
EXCLUDED_GROUPS = {
    "яичный продукт": {"яйцо"},
    "яичные продукты": {"яйцо"},
    "пасленовые": {"картофель", "помидор"},
    "молочный продукт": {"молоко", "сливочное масло", "фета"},
    "арахис": {"арахисовая паста"},
    "орех": {"арахисовая паста"},
    "орехи": {"арахисовая паста"},
    "перец": {"черный перец", "зеленый сладкий перец"},
    "свинина": {"свиная тушенка", "тушенка"},
    "говядина": {"говяжья тушенка", "тушенка"},
}
PANTRY = {"вода", "соль", "сахар", "черный перец", "перец", "оливковое масло",
          "растительное масло", "сливочное масло", "масло"}
SEASONINGS = {"зелень", "черный перец", "перец", "специи сухие"}
NO_MATCH = "В моём каталоге пока нет рецепта под эти условия."
DEMO_INTENT = {
    "ingredients": ["рис", "яйцо", "растительное масло", "соль", "вода"],
    "excluded": [],
    "can_buy": ["морковь"],
    "max_extra": 1,
}


def load_recipes():
    local = Path(__file__).with_name("recipes.json")
    recipes = json.loads(local.read_text(encoding="utf-8"))
    try:
        recipes.extend(json.loads(CACHE_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    return recipes


def ingredient_names(items):
    names = (" ".join(item.casefold().replace("ё", "е").split()) for item in items)
    return {ALIASES.get(name, name) for name in names}


def format_recipe(recipe, missing, assumed, optional, model, on_recipe=None):
    if on_recipe is not None:
        on_recipe(recipe)
    lines = [recipe["title"]]
    if recipe.get("rating"):
        score, maximum, votes, reviews = recipe["rating"]
        detail = f", оценок: {votes}" if votes else f", отзывов: {reviews}" if reviews else ""
        lines.append(f"Рейтинг: {score:g}/{maximum:g}{detail}")
    lines.extend(("", "Ингредиенты"))
    for name, amount in recipe["ingredients"].items():
        amount = (amount or "").strip()
        if amount.casefold() in {"количество не указано", "не указано"}:
            amount = ""
        lines.append(f"• {name} — {amount}" if amount else f"• {name}")
    if missing:
        lines.extend(("", "Докупить: " + ", ".join(sorted(missing)) + "."))
    if assumed:
        lines.append("Из базового понадобится: " + ", ".join(sorted(assumed)) + ".")
    if optional:
        lines.append("Можно добавить по вкусу: " + ", ".join(sorted(optional)) + ".")
    steps = summarize(model, recipe) if model and recipe.get("web_steps") else recipe["steps"]
    if steps:
        lines.extend(("", "Приготовление"))
        lines.extend(f"{number}. {step}" for number, step in enumerate(steps, 1))
    elif recipe.get("web_steps"):
        lines.extend(("", "Шаги приготовления доступны в оригинале рецепта."))
    if recipe.get("source"):
        lines.extend(("", "Источник: " + recipe.get("source_name", "TheMealDB"), recipe["source"]))
    return "\n".join(lines)


def rating_quality(recipe):
    rating = recipe.get("rating")
    if not rating:
        return 0
    score, maximum, votes, reviews = rating
    count = votes or reviews
    return (score / maximum * count + 0.8 * 8) / (count + 8)


def rank_match(match):
    recipe, missing, _assumed, _optional = match
    return len(missing), -rating_quality(recipe), -len(recipe["ingredients"]), recipe["title"]


def excluded_ingredients(intent):
    excluded = ingredient_names(intent["excluded"])
    for name in tuple(excluded):
        excluded.update(EXCLUDED_GROUPS.get(name, ()))
    return excluded


def purchase_rules(intent):
    have = ingredient_names(intent["ingredients"])
    allowed = intent["can_buy"]
    if allowed is not None:
        allowed = ingredient_names(allowed)
        # При противоречии в разборе считаем продукт возможной покупкой.
        have -= allowed
    limit = intent["max_extra"]
    return have, allowed, 2 if limit is None else min(limit, 2)


def acceptable_rating(recipe):
    rating = recipe.get("rating")
    return not rating or (rating[2] or rating[3]) < 3 or rating[0] / rating[1] >= 0.6


def match_recipe(recipe, required, have, allowed, limit):
    absent = required - have
    assumed = set()
    optional = set()
    if recipe.get("web_steps"):
        assumed = absent & {"вода", "соль"}
        optional = ingredient_names(
            name for name, amount in recipe["ingredients"].items() if amount.casefold() == "по вкусу"
        ) & SEASONINGS
    missing = absent - assumed - optional
    if len(missing) <= limit and (allowed is None or missing <= allowed):
        return recipe, missing, assumed, optional
    return None


def suggest_recipes(intent, recipes, model=None, on_recipe=None):
    have, allowed, limit = purchase_rules(intent)
    if not have:
        return HELP
    excluded = excluded_ingredients(intent)

    matches = []
    for recipe in recipes:
        required = ingredient_names(recipe["ingredients"])
        if required & excluded or not (required - PANTRY) & have:
            continue
        if not acceptable_rating(recipe):
            continue
        match = match_recipe(recipe, required, have, allowed, limit)
        if match:
            matches.append(match)

    matches.sort(key=rank_match)
    ready = next((match for match in matches if not match[1]), None)
    shopping = next((match for match in matches if match[1]), None)
    if not ready and not shopping:
        return NO_MATCH

    if ready:
        result = ["Можно приготовить по списку продуктов:\n" + format_recipe(*ready, model, on_recipe)]
    else:
        result = ["Среди проверенных рецептов есть вариант с покупкой."]
    if shopping:
        result.append("Вариант с покупкой:\n" + format_recipe(*shopping, model, on_recipe))
    return "\n\n".join(result)


def describe_recipe(intent, target, model, local_recipes, on_recipe, *, from_menu=False):
    candidates, reachable = search({target}, prefer_cooked=len(target.split()) == 1)
    terms = [word for group in stems({target}) for word in group]
    excluded = excluded_ingredients(intent)
    have, allowed, limit = purchase_rules(intent)
    restricted = allowed is not None or intent["max_extra"] is not None
    for pool in (candidates, local_recipes):
        ranked = []
        for recipe in pool:
            title_words = [word for group in stems({recipe["title"]}) for word in group]
            score = sum(any(word.startswith(term) for word in title_words) for term in terms)
            required = ingredient_names(recipe["ingredients"])
            if not score or required & excluded or not acceptable_rating(recipe):
                continue
            if from_menu and score < len(terms):
                ingredient_words = [word for group in stems(required) for word in group]
                if any(not any(word.startswith(term) for word in [*title_words, *ingredient_words]) for term in terms):
                    continue
            match = (match_recipe(recipe, required, have, allowed, limit) if restricted
                     else (recipe, set(), set(), set()))
            if match is None:
                continue
            technique = cooking_priority(recipe.get("source", "")) if len(target.split()) == 1 else 0
            order = (-score, len(match[1]), -technique, -rating_quality(recipe), len(required), recipe["title"])
            ranked.append((order, match))
        if ranked:
            match = min(ranked, key=lambda item: item[0])[1]
            return format_recipe(*match, model, on_recipe)
    return (
        "На проверенных сайтах не нашёл подходящего рецепта для этого блюда."
        if reachable else "Сайты рецептов сейчас не отвечают."
    )


def reply(message, model, recipes, on_recipe=None, *, on_menu=None):
    message = message.strip()
    if not message:
        return HELP
    if len(message) > 500:
        return "Сократи запрос до 500 символов и отправь его ещё раз."
    mode, target, intent = understand_request(model, message, allow_menu=on_menu is not None)
    if on_menu and mode in MENU_TITLES:
        on_menu(generate_menu(model, message, mode), intent["excluded"])
        return MENU_TITLES[mode]
    if mode == "help":
        return HELP + " Можно также попросить меню на неделю или для ресторана." if on_menu else HELP
    if mode == "dish":
        return describe_recipe(intent, target.strip(), model, recipes, on_recipe)
    keywords = ingredient_names(intent["ingredients"]) - PANTRY
    if keywords:
        found, reachable = search(keywords, pantry=True)
        answer = suggest_recipes(intent, found, model, on_recipe)
        if answer != NO_MATCH:
            return answer
        local = suggest_recipes(intent, recipes, on_recipe=on_recipe)
        status = (
            "На проверенных сайтах не нашёл рецепта под эти условия."
            if reachable else "Сайты рецептов сейчас не отвечают."
        )
        return status if local == NO_MATCH else status + "\n\nИз локальной подборки:\n" + local
    return suggest_recipes(intent, recipes, on_recipe=on_recipe)


def main():
    parser = argparse.ArgumentParser(description="Локальная проверка бота рецептов.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Имя локальной модели.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--demo", action="store_true", help="Показать подбор на готовом примере.")
    modes.add_argument("--message", help="Передать запрос локальной Gemma.")
    args = parser.parse_args()
    try:
        recipes = load_recipes()
        response = suggest_recipes(DEMO_INTENT, recipes) if args.demo else reply(
            args.message, args.model, recipes
        )
    except (OSError, http.client.HTTPException, ValueError, RuntimeError) as error:
        print(f"Не удалось обработать запрос: {error}", file=sys.stderr)
        return 1
    print(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
