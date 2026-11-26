import argparse
import json
import random
import time
from pathlib import Path

from intent import REQUEST_PROMPT, REQUEST_SCHEMA, normalized
from router_data import HOLDOUT, training_examples


BASE_MODEL = "google/gemma-4-E2B-it"


def comparable(answer):
    if not isinstance(answer, dict) or answer.keys() != REQUEST_SCHEMA["properties"].keys():
        raise ValueError("Ожидался полный ответ по схеме.")
    fields = answer.copy()
    mode, dish = fields.pop("mode"), fields.pop("dish")
    if mode not in {"help", "dish", "pantry"} or (mode == "dish") != isinstance(dish, str):
        raise ValueError("Тип запроса и название блюда не согласованы.")
    if dish is not None and not isinstance(dish, str):
        raise ValueError("Ожидалось название блюда или null.")
    dish = dish.strip().casefold().replace("ё", "е") if dish else None
    return {"mode": mode, "dish": dish, **normalized(fields)}


def validate_data():
    examples = list(training_examples())
    seen = set()
    for message, answer in [*examples, *HOLDOUT]:
        key = message.strip().casefold()
        if not key or key in seen:
            raise ValueError(f"Пустой или повторяющийся запрос: {message!r}")
        comparable(answer)
        seen.add(key)
    return examples


def conversation(tokenizer, message, answer=None):
    messages = [{"role": "system", "content": REQUEST_PROMPT}, {"role": "user", "content": message}]
    if answer is not None:
        messages.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)})
    text = tokenizer.apply_chat_template(messages, tokenize=False, enable_thinking=False,
                                         add_generation_prompt=answer is None)
    return tokenizer.encode(text, add_special_tokens=False)


def evaluate(torch, model, tokenizer, label):
    model.eval()
    exact = routed = 0
    for message, expected in HOLDOUT:
        tokens = torch.tensor([conversation(tokenizer, message)], device="cuda")
        with torch.inference_mode():
            generated = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens),
                                       max_new_tokens=180, do_sample=False, use_cache=True)
        text = tokenizer.decode(generated[0, tokens.shape[-1]:], skip_special_tokens=True).strip()
        try:
            actual = comparable(json.loads(text))
        except ValueError:
            actual = {}
        expected = comparable(expected)
        routed += actual.get("mode") == expected["mode"]
        exact += actual == expected
        if actual != expected:
            print(f"{label}: {message!r} → {text[:240]!r}", flush=True)
    print(f"{label}: mode={routed}/{len(HOLDOUT)} exact={exact}/{len(HOLDOUT)}", flush=True)


def train(args, examples):
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForMultimodalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("Для этого запуска нужна CUDA с поддержкой bfloat16.")
    args.output.mkdir(parents=True, exist_ok=False)
    torch.manual_seed(42)
    rng = random.Random(42)
    quantization = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_quant_storage=torch.bfloat16,
    )
    print(f"Загрузка {args.base_model} на {torch.cuda.get_device_name()}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModelForMultimodalLM.from_pretrained(
        args.base_model, quantization_config=quantization, device_map={"": 0}, dtype=torch.bfloat16,
    )
    model = get_peft_model(model, LoraConfig(
        r=8, lora_alpha=16, lora_dropout=0.05, task_type="CAUSAL_LM",
        target_modules=r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)",
    ))
    model.print_trainable_parameters()
    if args.compare:
        with model.disable_adapter():
            evaluate(torch, model, tokenizer, "base")

    batches = []
    for message, answer in examples:
        prompt = conversation(tokenizer, message)
        full = conversation(tokenizer, message, answer)
        if full[:len(prompt)] != prompt or len(full) <= len(prompt):
            raise RuntimeError("Шаблон Gemma не отделил ответ от запроса.")
        batches.append((full, [-100] * len(prompt) + full[len(prompt):]))

    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.train()
    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    optimizer = torch.optim.AdamW(parameters, lr=8e-5)
    started = time.monotonic()
    last_checkpoint = started
    batch_order = []
    update = 0
    while time.monotonic() - started < args.seconds or not update:
        loss_value = 0.0
        for _ in range(4):
            if not batch_order:
                batch_order = list(range(len(batches)))
                rng.shuffle(batch_order)
            tokens, labels = batches[batch_order.pop()]
            ids = torch.tensor([tokens], device="cuda")
            with torch.autocast("cuda", dtype=torch.bfloat16):
                loss = model(input_ids=ids, attention_mask=torch.ones_like(ids),
                             labels=torch.tensor([labels], device="cuda"), use_cache=False).loss / 4
            loss.backward()
            loss_value += loss.detach().item()
        torch.nn.utils.clip_grad_norm_(parameters, 1.0, error_if_nonfinite=True)
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        update += 1
        now = time.monotonic()
        if update == 1 or update % 10 == 0:
            print(f"Шаг {update}, loss={loss_value:.4f}, прошло {now - started:.0f} с", flush=True)
        if now - last_checkpoint >= 600:
            model.save_pretrained(args.output / f"step-{update}")
            last_checkpoint = now

    model.save_pretrained(args.output / "adapter")
    print(f"Адаптер сохранён после {update} шагов, {time.monotonic() - started:.0f} с.", flush=True)
    if args.compare:
        evaluate(torch, model, tokenizer, "adapter")


def main():
    parser = argparse.ArgumentParser(description="Локальное дообучение Gemma для разбора запросов.")
    parser.add_argument("--base-model", default=BASE_MODEL)
    parser.add_argument("--output", type=Path, default=Path(".training/run"))
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--compare", action="store_true", help="Замерить ответы до и после обучения.")
    parser.add_argument("--validate-data", action="store_true")
    args = parser.parse_args()
    if args.seconds < 1:
        parser.error("--seconds должен быть положительным")
    examples = validate_data()
    print(f"Обучающих запросов: {len(examples)}. Проверочных: {len(HOLDOUT)}.", flush=True)
    if not args.validate_data:
        train(args, examples)


if __name__ == "__main__":
    main()
