from statistics import mean

from cli import DOCUMENT, OUT, SYSTEM_PROMPT, TOKEN_FIELDS, answer_text, call_claude, metrics, save_json

PROMPT = "Проверь документ требований по ISO/IEC/IEEE 29148:2018 (п. 5.2.5 и 5.2.6) и IEEE 830-1998 (п. 4.3). Выпиши все дефекты. Для каждого: точная цитата из документа, какое свойство нарушено, почему, насколько критично."


def averages(rows: list[dict], keys: tuple[str, ...]) -> dict:
    result = {}
    for key in keys:
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        result[key] = mean(values) if values else None
    return result


def main() -> None:
    stdin_text = PROMPT + "\n\n---\nДокумент:\n\n" + DOCUMENT.read_text(encoding="utf-8")
    runs = []
    for number in range(1, 4):
        name = f"baseline_run{number}"
        rec = call_claude(name, stdin_text, SYSTEM_PROMPT)
        (OUT / f"{name}.md").write_text(answer_text(rec), encoding="utf-8")
        row = {key: rec[key] for key in ("name", "returncode", "wall_s", "cached", "claude_version")}
        row.update(metrics(rec))
        runs.append(row)
        usage = row["usage"]
        print(row["returncode"], f"{row['wall_s']:.3f}", usage["input_tokens"], usage["output_tokens"],
              row["total_cost_usd"], ",".join(row["models"]) or "-", row["answer_chars"],
              "yes" if row["cached"] else "no", sep="\t", flush=True)

    avg = averages(runs, ("wall_s", "duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "answer_chars"))
    avg["usage"] = averages([row["usage"] for row in runs], TOKEN_FIELDS)
    save_json(OUT / "baseline_summary.json", {
        "prompt": PROMPT, "system_prompt": SYSTEM_PROMPT, "claude_version": runs[0]["claude_version"],
        "runs": runs, "averages": avg,
    })


if __name__ == "__main__":
    main()
