import argparse
import http.client
import json
import sys
from collections import Counter
from pathlib import Path

from intent import DEFAULT_MODEL, PROPERTIES, ask_model, normalized


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


def main():
    parser = argparse.ArgumentParser(description="Проверка разбора запросов о рецептах.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Имя локальной модели.")
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
