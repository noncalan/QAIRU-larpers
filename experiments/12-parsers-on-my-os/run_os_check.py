"""Run the parsing lab locally and compare guard diagnostics with section 5."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import closing, suppress
from dataclasses import asdict
from importlib import metadata
from pathlib import Path

import psutil

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[2]
LAB = ROOT / "experiments/04-parsing-lab"
OUT = ROOT / "experiments/out"
FX = OUT / "fx"
PATH_TEST = OUT / "fx_path/папка с пробелом/требования.pdf"
REPORT = OUT / "12"
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
             "PYTHONDONTWRITEBYTECODE": "1"}
PACKAGES = ("pymupdf pymupdf4llm pdfplumber pypdfium2 pikepdf python-docx pypdf "
            "markitdown charset-normalizer msoffcrypto-tool olefile psutil").split()
WORDS = re.compile(r"[а-яё]{3,}")
EXPECTED = {
    name: codes for codes, names in {
        "IN-002": "zero.pdf zero.docx zero.md", "IN-070": "whitespace.md",
        "IN-050": "blank_page.pdf scan.pdf", "IN-051": "mixed_text_scan.pdf",
        "IN-020": "enc_user.pdf", "IN-022": "enc_owner_only.pdf",
        "IN-041": "bad_xref.pdf zeroed_xref.pdf", "IN-040/IN-041/IN-043": "truncated_60.pdf",
        "IN-040/IN-042": "truncated_5.pdf header_only.pdf",
        "IN-010": "garbage.pdf image_named.pdf image.png",
        "IN-011": "pdf_named.docx docx_named.pdf md_named.pdf docx_not_zip.docx",
        "IN-032": "docx_truncated.docx", "IN-031": "docx_no_document_xml.docx",
        "IN-030": "docx_bomb.docx", "IN-021": "docx_password.docx",
        "IN-061": "req_cp1251.md req_koi8r.txt req_utf16.md req_utf16le_nobom.md",
        "none": "req_utf8_bom.md novel.md template_empty.md mixed_lang.md req.md req.docx "
                "req.pdf req.html req.rtf req.odt",
    }.items() for name in names.split()
}


def environment():
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return {"platform": platform.platform(), "python": platform.python_version(), **versions}


def memory_bytes(process):
    info = process.memory_info()
    return getattr(info, "peak_wset", info.rss)


def make_fixtures():
    if FX.resolve() != FX:
        raise ValueError(f"Refusing to reset redirected directory: {FX}")
    if FX.exists():
        shutil.rmtree(FX)
    FX.mkdir(parents=True)
    for ext in ("md", "docx", "pdf"):
        shutil.copyfile(ROOT / f"data/input/requirements.{ext}", FX / f"req.{ext}")
    start = time.perf_counter()
    peak = 0
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        [sys.executable, LAB / "make_fixtures.py", FX], cwd=ROOT, env=CHILD_ENV,
        stdout=subprocess.DEVNULL, stderr=errors,
    ) as child:
        with suppress(psutil.NoSuchProcess):
            process = psutil.Process(child.pid)
            while child.poll() is None:
                total = 0
                for member in [process, *process.children(recursive=True)]:
                    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        total += memory_bytes(member)
                peak = max(peak, total)
                time.sleep(0.05)
        child.wait()
        elapsed = time.perf_counter() - start
        errors.seek(max(0, errors.seek(0, os.SEEK_END) - 2048))
        tail = errors.read().decode("utf-8", errors="replace")
    PATH_TEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FX / "req.pdf", PATH_TEST)
    return {"returncode": child.returncode, "elapsed_s": elapsed, "peak_mb": peak / 2**20,
            "stderr_tail": tail}


def guard_one(path):
    sys.path.insert(0, str(LAB))
    from guard import check

    start = time.perf_counter()
    probe = check(str(path))
    return {"kind": probe.kind, "diags": [asdict(d) for d in probe.diags],
            "elapsed_s": time.perf_counter() - start, "peak_mb": memory_bytes(psutil.Process()) / 2**20}


def extract(path, parser):
    if parser == "pymupdf":
        import pymupdf
        with pymupdf.open(path, filetype="pdf") as doc:
            return "".join(page.get_text() for page in doc)
    if parser == "pdfplumber":
        import pdfplumber
        with pdfplumber.open(path) as doc:
            return "\n".join(page.extract_text() or "" for page in doc.pages)
    if parser == "pypdfium2":
        import pypdfium2
        pages = []
        with closing(pypdfium2.PdfDocument(path)) as doc:
            for index in range(len(doc)):
                with closing(doc[index]) as page, closing(page.get_textpage()) as text:
                    pages.append(text.get_text_range())
        return "\n".join(pages)
    if parser == "pymupdf4llm":
        import pymupdf4llm
        return pymupdf4llm.to_markdown(str(path))
    if parser == "python-docx":
        from docx import Document
        doc = Document(path)
        parts = [paragraph.text for paragraph in doc.paragraphs]
        parts.extend(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        return "\n".join(parts)
    from charset_normalizer import from_path
    best = from_path(path).best()
    if best is None:
        raise ValueError("Encoding could not be detected")
    return str(best)


def parse_one(path, kind):
    if path.name.casefold() == "docx_bomb.docx":
        raise ValueError("Only guard.check may inspect docx_bomb.docx")
    parsers = {"pdf": ["pymupdf", "pdfplumber", "pypdfium2"], "docx": ["python-docx"],
               **dict.fromkeys(("text", "html", "rtf"), ["charset-normalizer"])}.get(kind, [])
    if path == FX / "req.pdf" and kind == "pdf":
        parsers.append("pymupdf4llm")
    reference = set(WORDS.findall((FX / "req.md").read_text(encoding="utf-8").lower()))
    for parser in parsers:
        row = {"parser": parser, "ok": False, "chars": None, "recall": None}
        start = time.perf_counter()
        try:
            text = extract(path, parser)
        except Exception as exc:
            row["elapsed_s"] = time.perf_counter() - start
            row.update(error_type=type(exc).__name__, message=str(exc)[:300])
        else:
            row["elapsed_s"] = time.perf_counter() - start  # холодный: с первым import
            warm = time.perf_counter()
            extract(path, parser)
            row["warm_s"] = time.perf_counter() - warm  # тёплый: повтор без import
            found = set(WORDS.findall(text.lower()))
            row.update(ok=True, chars=len(text), recall=len(reference & found) / len(reference)
                       if reference else None)
        yield row


def worker(mode, path, *args):
    # Keep the JSON channel separate from Python and native parser messages.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as channel:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        if mode == "--guard-one":
            result = guard_one(path)
        else:
            for row in parse_one(path, *args):
                print(json.dumps(row, ensure_ascii=False), file=channel, flush=True)
            result = {"peak_mb": memory_bytes(psutil.Process()) / 2**20}
        print(json.dumps(result, ensure_ascii=False), file=channel)


def run_worker(mode, path, *args):
    start = time.perf_counter()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, subprocess.Popen(
        [sys.executable, SCRIPT, mode, path, *args], cwd=ROOT, env=CHILD_ENV,
        stdout=stdout, stderr=stderr,
    ) as child:
        try:
            code = child.wait(timeout=60)
            status = "OK" if code == 0 else f"EXIT {code}"
        except subprocess.TimeoutExpired:
            # A Windows venv launcher can have a separate interpreter process.
            with suppress(psutil.NoSuchProcess):
                for member in reversed(psutil.Process(child.pid).children(recursive=True)):
                    with suppress(psutil.NoSuchProcess):
                        member.kill()
            child.kill()
            child.wait()
            status = "TIMEOUT"
        stdout.seek(0)
        output = stdout.read()
        stderr.seek(max(0, stderr.seek(0, os.SEEK_END) - 2048))
        errors = stderr.read()
    result = {"status": status, "wall_s": time.perf_counter() - start,
              "stderr_tail": errors.decode("utf-8", errors="replace")}
    if mode == "--parse-one":
        result["parsers"] = []
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if status == "OK":
                result["status"] = "INVALID OUTPUT"
            continue
        if "parser" in record:
            result["parsers"].append(record)
        else:
            result.update(record)
    if result["status"] == "OK" and "peak_mb" not in result:
        result["status"] = "INCOMPLETE"
    return result


def check_fixture(path):
    guard = run_worker("--guard-one", path)
    diags = guard.get("diags", [])
    allowed = guard["status"] == "OK" and all(d["level"] != "ERROR" for d in diags)
    expected = "none" if path == PATH_TEST else EXPECTED.get(path.name, "n/a")
    bomb = path.name.casefold() == "docx_bomb.docx"
    verdict = "N/A"
    if expected != "n/a":
        codes = {d["code"] for d in diags}
        matched = allowed if expected == "none" else bool(codes.intersection(expected.split("/")))
        if bomb:
            expected += " (< 1 s)"
            matched = matched and guard["elapsed_s"] < 1
        verdict = "MATCH" if guard["status"] == "OK" and matched else "MISMATCH"
    row = {"file": path.relative_to(OUT).as_posix(), "size_kb": path.stat().st_size / 1024,
           "guard": guard, "expected": expected, "verdict": verdict}
    if allowed and not bomb:
        row["parse"] = run_worker("--parse-one", path, guard["kind"])
    if path.name == "docx_bad_xml.docx":
        results = row.get("parse", {}).get("parsers", [])
        raised = any(p["parser"] == "python-docx" and not p["ok"]
                     and p["error_type"] not in ("ImportError", "ModuleNotFoundError") for p in results)
        row["parser_expectation"] = {"expected": "IN-034 (python-docx raises)",
                                     "verdict": "MATCH" if raised else "MISMATCH"}
    return row


def table(headers, rows):
    def cell(value):
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value if value is not None else "—").replace("|", r"\|").replace("\n", " ").replace("\r", " ")
    return ["| " + " | ".join(map(cell, row)) + " |"
            for row in (headers, ["---"] * len(headers), *rows)]


def markdown(report):
    gen = report["generation"]
    lines = ["# Проверка парсеров", "", "## Окружение", "",
             *table(["Параметр", "Значение"], report["environment"].items()), "",
             "## Генерация файлов", "",
             f"Код возврата {gen['returncode']}, время {gen['elapsed_s']:.3f} с, "
             f"пик памяти {gen['peak_mb']:.1f} МБ.", "", "## Проверка guard", ""]
    guards, parsers = [], []
    for row in report["files"]:
        name, guard = row["file"], row["guard"]
        codes = ", ".join(d["code"] + " " + d["level"] for d in guard.get("diags", []))
        guards.append([name, row["size_kb"], guard.get("kind"), codes or guard["status"],
                       row["expected"], row["verdict"], guard.get("elapsed_s", guard["wall_s"]),
                       guard.get("peak_mb")])
        if "parse" in row:
            stage = row["parse"]
            for p in stage["parsers"]:
                status = "ok" if p["ok"] else f"{p['error_type']} — {p['message']}"
                parsers.append([name, p["parser"], status, p["chars"], p["recall"], p["elapsed_s"], p.get("warm_s")])
            if stage["status"] != "OK":
                parsers.append([name, "процесс", stage["status"], None, None, stage["wall_s"]])
    lines += table(["Файл", "Размер, КБ", "Тип", "Коды", "Ожидание", "Итог", "Время, с", "Пик, МБ"], guards)
    lines += ["", "## Парсеры", "", *table(["Файл", "Парсер", "Результат", "Символы",
                                            "Полнота", "Холодный, с", "Тёплый, с"], parsers)]
    for row in report["files"]:
        if "parser_expectation" in row:
            lines += ["", f"`{row['file']}` — исключение python-docx (IN-034): "
                      + row["parser_expectation"]["verdict"]]
    return "\n".join([*lines, "", "## Итоги guard", "", summary(report), ""])


def summary(report):
    counts = ", ".join(f"{key} {value}" for key, value in report["counts"].items())
    return f"{counts} · {report['total_s']:.2f} с · генератор {report['generation']['returncode']}"


def main():
    start = time.perf_counter()
    env = environment()
    generation = make_fixtures()
    files = []
    if generation["returncode"] == 0:
        paths = sorted(p for p in FX.iterdir() if p.is_file())
        files = [check_fixture(path) for path in [*paths, PATH_TEST]]
    counts = Counter(row["verdict"] for row in files)
    report = {"environment": env, "generation": generation, "files": files,
              "counts": {key: counts[key] for key in ("MATCH", "MISMATCH", "N/A")},
              "total_s": time.perf_counter() - start}
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (REPORT / "results.md").write_text(markdown(report), encoding="utf-8")
    print(summary(report))
    return generation["returncode"]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:2] in (["--guard-one"], ["--parse-one"]):
        worker(sys.argv[1], Path(sys.argv[2]).resolve(), *sys.argv[3:])
    else:
        sys.exit(main())
