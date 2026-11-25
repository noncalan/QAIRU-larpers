import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, ValidationError

from cli import (DOCUMENT, OUT, SYSTEM_PROMPT, answer_text, call_claude, clean_directory,
                 clean_env, cli_info, executable, metrics, payload, run_cached, save_json)

SCHEMA_PROMPT = "Найди дефекты требований в этом фрагменте по ISO/IEC/IEEE 29148. Верни только список."
SAMPLE = "Ёжик съел щётку: «Лимит — 50 000 ₸», ң ә ғ қ ө ұ ү һ і, № 5, ±1 %."
ECHO_PROMPT = "Повтори дословно, без изменений и без пояснений, текст между маркерами <<< и >>>:\n<<<" + SAMPLE + ">>>"
CAPITAL_PROMPT = "Ответь одним словом: столица Казахстана?"
CONTEXT_PROMPT = "Видишь ли ты сейчас какие-либо файлы, инструкции проекта, CLAUDE.md, память пользователя или скиллы? Ответь сначала ДА или НЕТ, затем перечисли, что именно видишь (имена файлов и первые слова инструкций), без догадок."
FLAGS = ("--safe-mode", "--no-session-persistence", "--tools", "--json-schema")


class Defect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quote: str
    property: str
    explanation: str


class Defects(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    defects: list[Defect]


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["defects"],
    "properties": {"defects": {"type": "array", "items": Defect.model_json_schema()}},
}


def first_section(document: str) -> str:
    headers = re.finditer(r"^## ", document, flags=re.MULTILINE)
    next(headers, None)
    second = next(headers, None)
    return document[:second.start()] if second else document


def structured_result(rec: dict) -> dict:
    parsed = payload(rec)
    result = {"source": None, "valid": False, "item_count": None}
    for key in ("structured_output", "result"):
        data = parsed.get(key)
        if data is None:
            continue
        if isinstance(data, str):
            block = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", data, re.I | re.S)
            try:
                data = json.loads(block[1] if block else data)
            except ValueError:
                continue
        result["source"] = key
        try:
            validated = Defects.model_validate(data)
        except ValidationError as exc:
            result["validation_errors"] = exc.error_count()
            continue
        return {"source": key, "valid": True, "item_count": len(validated.defects)}
    return result


def sample_result(rec: dict) -> dict:
    actual = answer_text(rec)
    difference = None
    if actual != SAMPLE:
        common = min(len(SAMPLE), len(actual))
        index = next((i for i in range(common) if actual[i] != SAMPLE[i]), common)
        difference = {"index": index, "expected": repr(SAMPLE[index:index + 1]),
                      "actual": repr(actual[index:index + 1])}
    return {"contains_sample": SAMPLE in actual, "exact": actual == SAMPLE,
            "stripped_equal": actual.strip() == SAMPLE, "first_diff": difference}


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_sample(fix_pipe: bool = False) -> dict:
    def build_args(exe: str, stack: ExitStack) -> list[str]:
        prompt_file = Path(stack.enter_context(TemporaryDirectory())) / "prompt.txt"
        prompt_file.write_bytes(ECHO_PROMPT.encode("utf-8"))
        command = (
            "$ErrorActionPreference='Stop'; "
            "[Console]::InputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            + ("$OutputEncoding=[Text.Encoding]::UTF8; " if fix_pipe else "") +
            f"Get-Content -Raw -Encoding UTF8 {ps_literal(str(prompt_file))} | & {ps_literal(exe)} "
            f"-p --model opus --tools '\"\"' --system-prompt {ps_literal(SYSTEM_PROMPT)} "
            "--safe-mode --no-session-persistence --output-format json; exit $LASTEXITCODE"
        )
        return [shutil.which("powershell") or "powershell", "-NoProfile", "-Command", command]

    return run_cached("b3b_cyrillic_ps_fixed" if fix_pipe else "b3_cyrillic_ps", build_args, None)


def tools_accepted(rec: dict) -> bool | None:
    if rec["returncode"] == 0 or payload(rec).get("type") == "result":
        return True
    diagnostic = rec["stderr_tail"] + "\n" + rec["stdout_raw"]
    if re.search(r"^.*(?:unknown|invalid|unexpected|missing|requires|error).*--tools|"
                 r"^.*--tools.*(?:unknown|invalid|unexpected|missing|requires|error)", diagnostic, re.M | re.I):
        return False
    return None


def overview(rec: dict) -> dict:
    return {"raw_file": rec["name"] + ".json", **{key: rec[key] for key in
            ("returncode", "cached", "started_at", "ended_at", "wall_s", "process_error")}}


def parallel_check() -> dict:
    names = ("b4_parallel_a", "b4_parallel_b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call_claude, name, CAPITAL_PROMPT, SYSTEM_PROMPT) for name in names]
        records = [future.result() for future in futures]
    start = max(datetime.fromisoformat(rec["started_at"]) for rec in records)
    end = min(datetime.fromisoformat(rec["ended_at"]) for rec in records)
    group = {}
    for rec in records:
        failed = rec["returncode"] != 0 or payload(rec).get("is_error")
        group[rec["name"]] = {**overview(rec), "stderr_tail": rec["stderr_tail"],
                              "error_text": rec["stdout_raw"] if failed else None}
    return {"runs": group, "overlap_s": max(0.0, (end - start).total_seconds())}


def no_login() -> dict:
    with clean_directory() as config_dir:
        # The empty profile must also be isolated from inherited OAuth/provider credentials.
        overrides = {key: None for key in os.environ if key.startswith(("CLAUDE_", "ANTHROPIC_"))}
        overrides["CLAUDE_CONFIG_DIR"] = config_dir
        rec = call_claude("b5_no_login", "Ответь: ok", SYSTEM_PROMPT, env_overrides=overrides)
    diagnostic = rec["stdout_raw"] + "\n" + rec["stderr_raw"]
    used_tokens = any((n or 0) > 0 for n in metrics(rec)["usage"].values())
    if used_tokens or (rec["returncode"] == 0 and payload(rec).get("is_error") is False):
        reached_model = True
    elif rec["returncode"] != 0 and re.search(
        r"not logged in|please run /login|authentication|unauthorized|invalid api key|401", diagnostic, re.I
    ):
        reached_model = False
    else:
        reached_model = None
    return {**overview(rec), "stdout": rec["stdout_raw"], "stderr": rec["stderr_raw"],
            "reached_model": reached_model}


def help_check() -> dict:
    with clean_directory() as cwd:
        result = cli_info(executable(), "--help", cwd, clean_env()[0])
    output = result.stdout + "\n" + result.stderr
    return {"returncode": result.returncode, "flags": {
        flag: bool(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", output)) for flag in FLAGS
    }}


def baseline_timings() -> list[dict] | None:
    path = OUT / "baseline_summary.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text(encoding="utf-8"))["runs"]
    return [{key: row[key] for key in ("name", "wall_s", "duration_ms")} for row in rows]


def main() -> None:
    report = {"help": help_check(), "baseline_timings": baseline_timings()}
    text = first_section(DOCUMENT.read_text(encoding="utf-8"))
    rec = call_claude("b1_json_schema", SCHEMA_PROMPT + "\n\n" + text, SYSTEM_PROMPT,
                      ["--json-schema", json.dumps(SCHEMA, ensure_ascii=False)])
    schema = structured_result(rec)
    report[rec["name"]] = {**overview(rec), **schema}
    print(f"b1_json_schema rc={rec['returncode']} source={schema['source']} valid={schema['valid']} items={schema['item_count']}")

    for name in ("b2_cyrillic_py", "b3_cyrillic_ps"):
        rec = powershell_sample() if name == "b3_cyrillic_ps" else call_claude(name, ECHO_PROMPT, SYSTEM_PROMPT)
        check = {**overview(rec), **sample_result(rec)}
        if name == "b3_cyrillic_ps":
            check.update(tools_accepted=tools_accepted(rec), stderr_tail=rec["stderr_tail"])
        report[name] = check
        print(f"{name} rc={rec['returncode']} contains={check['contains_sample']} stripped={check['stripped_equal']}")

    rec = powershell_sample(fix_pipe=True)
    check = {**overview(rec), **sample_result(rec)}
    report["b3b_cyrillic_ps_fixed"] = check
    print(f"b3b_cyrillic_ps_fixed rc={rec['returncode']} contains={check['contains_sample']} stripped={check['stripped_equal']}")

    parallel = parallel_check()
    report["b4_parallel"] = parallel
    codes = [rec["returncode"] for rec in parallel["runs"].values()]
    print(f"b4_parallel rc={codes} overlap_s={parallel['overlap_s']:.3f}")
    login = no_login()
    report["b5_no_login"] = login
    print(f"b5_no_login rc={login['returncode']} reached_model={login['reached_model']}")

    for name, safe in (("b6_context_safe", True), ("b7_context_nosafe", False)):
        rec = call_claude(name, CONTEXT_PROMPT, SYSTEM_PROMPT, safe_mode=safe)
        answer = answer_text(rec)
        word = re.search(r"\w+", answer)
        first_word = word[0] if word else None
        report[name] = {**overview(rec), "first_word": first_word, "answer_chars": len(answer), "answer": answer}
        preview = " ".join(answer[:300].splitlines())
        print(f"{name} rc={rec['returncode']} first={first_word} chars={len(answer)} {preview}")
    save_json(OUT / "backend_check.json", report)


if __name__ == "__main__":
    main()
