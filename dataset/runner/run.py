#!/usr/bin/env python3
"""
Раннер одного прогона: одна задача, один режим памяти, один повтор.

    python dataset/runner/run.py --task a3 --mode memory-off --seed 1

Что делает по шагам:
  1. валидирует задачу (пути скрытых тестов реально есть в эталонном коммите);
  2. материализует base_commit в свежий однокоммитный git repo без будущей истории;
  3. убирает чужие агентские файлы (AGENTS.md / CLAUDE.md) — в ОБОИХ режимах,
     иначе в memory-off приезжает чужая память, а в memory-on — две сразу;
  4. в memory-on кладёт .claude/settings.json с хуками и проверяет, что память
     реально уехала в контекст (пустой снапшот = прогон невалиден, не тихий ноль);
  5. запускает агента;
  6. снимает дифф, гоняет регрессию (скрытые тесты из неё исключены);
  7. накладывает скрытые тесты из эталонного коммита и гоняет их;
  8. пишет metrics.json + артефакты и убирает за собой workspace.

Скрытые тесты не «прячутся» — на base_commit их в новой редакции ещё нет.
Мы накладываем их поверх дерева агента после прогона.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("нужен pyyaml: pip install pyyaml")

if sys.version_info < (3, 11):
    sys.exit("нужен python >= 3.11 (tomllib)")

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "dataset" / "hooks"

RC_TIMEOUT = -9


# --------------------------------------------------------------------------- утилиты


def sh(cmd, cwd=None, env=None, timeout=None, check=False):
    """Запуск команды. При таймауте убивает всю process group, но сохраняет частичный вывод."""
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=isinstance(cmd, str),
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = p.communicate()
        return RC_TIMEOUT, out, err + f"\n[kata] таймаут {timeout}s: {cmd}"
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd} -> rc={p.returncode}\n{err[-2000:]}")
    return p.returncode, out, err


EMPTY_TESTS = {"rc": None, "parsed": False, "tests": 0, "passed": 0, "failed": 0,
               "errors": 0, "skipped": 0, "green": False, "ratio": 0.0, "failing": []}


def parse_junit(xml_path: Path, rc: int) -> dict:
    """
    Результат считаем по junit-xml, а не по хвосту вывода: uv дописывает свои
    предупреждения после pytest, и парсинг строки «N passed» врёт. XML заодно
    даёт поимённый список упавших проверок — без него attribution не собрать.
    """
    if not xml_path.exists():
        return {**EMPTY_TESTS, "rc": rc}

    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    tests = failures = errors = skipped = 0
    failing = []
    for s in suites:
        tests += int(s.get("tests", 0))
        failures += int(s.get("failures", 0))
        errors += int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
        for case in s.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failing.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    passed = tests - failures - errors - skipped
    ran = tests - skipped
    return {
        "rc": rc,
        "parsed": True,
        "tests": tests,
        "passed": passed,
        "failed": failures,
        "errors": errors,
        "skipped": skipped,
        # passed > 0 обязательно: иначе прогон, где всё скипнулось (нет LDAP-сервиса),
        # отчитается как зелёный при нуле пройденных проверок
        "green": rc == 0 and failures == 0 and errors == 0 and passed > 0,
        "ratio": round(passed / ran, 3) if ran else 0.0,
        "failing": failing[:40],
    }


# --------------------------------------------------------------------------- модели


@dataclass
class Task:
    id: str
    solution_commit: str
    base_commit: str
    title: str
    prompt: str
    hidden_tests: list[str]
    slices: list[str] = field(default_factory=list)
    contract: str | None = None


def load_tasks(path: Path) -> tuple[dict, dict[str, Task]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    tasks = {
        t["id"]: Task(
            id=t["id"],
            solution_commit=t["solution_commit"],
            base_commit=t["base_commit"],
            title=t["title"],
            prompt=" ".join(t["prompt"].split()),
            hidden_tests=t["hidden_tests"],
            slices=t.get("slices", []),
            contract=(" ".join(t["contract"].split()) if t.get("contract") else None),
        )
        for t in doc.get("tasks", [])
    }
    return doc.get("meta", {}) or {}, tasks


def validate_task(clone: Path, task: Task) -> list[str]:
    """Дешёвые проверки до того, как потрачены токены."""
    problems = []
    for sha in (task.base_commit, task.solution_commit):
        rc, _, _ = sh(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=clone)
        if rc != 0:
            problems.append(f"нет коммита {sha} в клоне")
    rc_base, base, _ = sh(["git", "rev-parse", task.base_commit], cwd=clone)
    rc_parent, parent, _ = sh(["git", "rev-parse", f"{task.solution_commit}^"], cwd=clone)
    if rc_base == 0 and rc_parent == 0 and base.strip() != parent.strip():
        problems.append(
            f"base_commit {task.base_commit} не является родителем {task.solution_commit}"
        )
    rc, out, _ = sh(["git", "ls-tree", "-r", "--name-only", task.solution_commit], cwd=clone)
    present = set(out.splitlines())
    for p in task.hidden_tests:
        if p not in present:
            problems.append(f"скрытый тест {p} отсутствует в {task.solution_commit}")
    return problems


# --------------------------------------------------------------------------- рабочее дерево


def ensure_clone(cfg) -> Path:
    clone = (ROOT / cfg["repo"]["clone"]).resolve()
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        print(f"[workspace] клонирую {cfg['repo']['url']} -> {clone}")
        sh(["git", "clone", cfg["repo"]["url"], str(clone)], check=True)
    else:
        sh(["git", "fetch", "--quiet", "--all"], cwd=clone, check=True)
    return clone


def extract_from_reference(clone: Path, commit: str, dest: Path,
                           paths: list[str] | None = None) -> None:
    """Материализует дерево/пути из reference clone, не связывая его историю с workspace."""
    archive = dest.parent / f".{dest.name}-{commit[:9]}.tar"
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = ["git", "archive", "--format=tar", f"--output={archive}", commit]
        if paths:
            cmd.extend(paths)
        sh(cmd, cwd=clone, check=True)
        dest.mkdir(parents=True, exist_ok=True)
        sh(["tar", "-xf", str(archive), "-C", str(dest)], check=True)
    finally:
        archive.unlink(missing_ok=True)


def make_workspace(clone: Path, base_commit: str, dest: Path,
                   strip_files: list[str]) -> tuple[Path, list[str]]:
    """Свежий однокоммитный repo: будущих коммитов Mealie агент физически не видит."""
    if dest.exists():
        shutil.rmtree(dest)
    extract_from_reference(clone, base_commit, dest)
    removed = strip_foreign_memory(dest, strip_files)
    sh(["git", "init", "-q"], cwd=dest, check=True)
    sh(["git", "config", "user.name", "kata-eval"], cwd=dest, check=True)
    sh(["git", "config", "user.email", "kata-eval@localhost"], cwd=dest, check=True)
    sh(["git", "add", "-A"], cwd=dest, check=True)
    sh(["git", "commit", "-q", "-m", f"eval base {base_commit}"], cwd=dest, check=True)
    return dest, removed


def drop_workspace(dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)


def strip_foreign_memory(wt: Path, names: list[str]) -> list[str]:
    """
    Убираем чужие агентские файлы. Делаем это в ОБОИХ режимах: в memory-off иначе
    сравниваем не с пустотой, а с чужой памятью; в memory-on — получили бы две
    памяти сразу и не поняли, чья заслуга.
    """
    removed = []
    for name in names:
        p = wt / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return removed


def install_agent_settings(wt: Path, memory_on: bool, write_back: bool) -> None:
    """
    В обоих режимах ставит одинаковую OS-песочницу. В memory-on дополнительно
    ставит SessionStart, который читает локальную копию снапшота.

    Stop-хук (шаг актуализации памяти) ставим ТОЛЬКО когда памяти реально есть
    куда писать. Иначе он гарантированно добавляет memory-on лишний ход и лишние
    токены — то есть портит ровно ту метрику, ради которой всё затевалось.
    """
    settings = {}
    if memory_on:
        settings = json.loads((HOOKS_DIR / "settings.memory-on.json").read_text(encoding="utf-8"))
        if not write_back:
            settings["hooks"].pop("Stop", None)
    settings["sandbox"] = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        # Reference clone, dataset and credentials live under home; subprocesses
        # may read only the temporary project workspace.
        "filesystem": {"denyRead": ["~/"], "allowRead": ["."]},
    }
    claude_dir = wt / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def install_agent_runtime(wt: Path, cfg, memory_on: bool) -> dict[str, str]:
    """Копирует минимальный runtime внутрь sandbox и возвращает его env."""
    bin_dir = wt / ".kata-bin"
    bin_dir.mkdir(exist_ok=True)
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv не найден в PATH")
    shutil.copy2(uv, bin_dir / "uv")

    env = {
        **cfg["repo"].get("env", {}),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "UV_CACHE_DIR": str(wt / ".uv-cache"),
    }
    if memory_on:
        local_hooks = wt / ".kata-hooks"
        shutil.copytree(HOOKS_DIR, local_hooks)
        env["KATA_HOOKS_DIR"] = str(local_hooks)
        env["KATA_RUN_DIR"] = str(wt / ".kata-run")
        env["KATA_MEMORY_MODE"] = cfg["memory"]["mode"]
        if cfg["memory"]["mode"] == "snapshot":
            snapshot = (ROOT / cfg["memory"]["snapshot"]).resolve()
            local_snapshot = local_hooks / "snapshot-c0.md"
            shutil.copy2(snapshot, local_snapshot)
            env["KATA_FACTS_SNAPSHOT"] = str(local_snapshot)
        env["KATA_XMEM_INSTANCE"] = cfg["memory"].get("xmem_instance", "")
    return env


def collect_hook_artifacts(wt: Path, run_dir: Path) -> None:
    source = wt / ".kata-run"
    if not source.exists():
        return
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, run_dir / path.name)



EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
READ_TOOLS = {"Read", "Glob", "Grep"}
PATH_KEYS = ("file_path", "path", "notebook_path", "pattern")


def parse_stream_events(out: str, run_dir: Path) -> dict:
    """
    Разбор --output-format stream-json в метрики разведки.

    Зачем. Гипотеза звучит как «память заменяет разведку»: агент не ищет по репозиторию
    то, что уже записано в фактах. Тогда правильная метрика — не общие токены, а
    **сколько агент потратил, пока не начал править код**, и **сколько из прочитанного
    он в итоге тронул**. Общие токены этот механизм не видят: агент может сэкономить
    тем, что просто меньше сделал.

    Если поток не распознан (запуск с --output-format json), возвращается пустой блок,
    а usage берётся старым путём.
    """
    events, result = [], None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        events.append(ev)
        if ev.get("type") == "result":
            result = ev
    if not events:
        return {}

    (run_dir / "agent_events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    calls, read_paths, edit_paths = [], [], []
    for ev in events:
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "?")
            calls.append(name)
            inp = block.get("input") or {}
            path = next((inp[k] for k in PATH_KEYS if isinstance(inp.get(k), str)), None)
            if path:
                (edit_paths if name in EDIT_TOOLS else
                 read_paths if name in READ_TOOLS else []).append(path)

    first_edit = next((i for i, n in enumerate(calls) if n in EDIT_TOOLS), None)
    reads, edits = set(read_paths), set(edit_paths)
    by_tool: dict[str, int] = {}
    for n in calls:
        by_tool[n] = by_tool.get(n, 0) + 1

    return {
        "tool_calls": len(calls),
        "tool_calls_by_name": by_tool,
        # сколько инструментов агент потратил, прежде чем впервые тронул код
        "calls_before_first_edit": first_edit,
        "share_before_first_edit": round(first_edit / len(calls), 3) if calls and first_edit else 0.0,
        "files_read": len(reads),
        "files_edited": len(edits),
        # какая доля прочитанного оказалась нужной: память должна её поднимать
        "exploration_precision": round(len(reads & edits) / len(reads), 3) if reads else None,
        "result_event": bool(result),
    }


# --------------------------------------------------------------------------- агент


def build_prompt(task: Task) -> str:
    """
    contract — только публичные имена (маршрут, поле схемы, переменная окружения).
    Их скрытый тест пиняет буквально, угадать нельзя ни с памятью, ни без, и без
    подсказки эвал мерил бы лотерею. Архитектура (где внутри и по какой конвенции)
    в промпт не попадает никогда — это ровно то, что проверяется.
    """
    parts = [task.prompt]
    if task.contract:
        parts.append(f"Публичный контракт, которого нужно придерживаться: {task.contract}")
    parts.append("Работай в этом репозитории. Реализуй изменение так, как это принято "
                 "в проекте. Когда закончишь — коротко перечисли, что изменил.")
    return "\n\n".join(parts)


def changed_sources(clone: Path, task: Task) -> list[str]:
    """Исходники эталонного коммита без тестов — для режима oracle.

    Удалённые файлы отфильтрованы: git checkout атомарен, один несуществующий
    путь отменяет весь checkout, и оракул тихо не накатывает ничего.
    """
    rc, out, _ = sh(["git", "show", "--diff-filter=ACMR", "--name-only", "--format=",
                     task.solution_commit], cwd=clone)
    if rc != 0:
        return []
    return [f for f in out.splitlines() if f.strip() and not f.startswith("tests/")]


def run_agent(kind: str, cfg, wt: Path, task: Task, clone: Path,
              run_dir: Path, env_extra: dict) -> dict:
    prompt = build_prompt(task)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    t0 = time.time()

    if kind == "null":
        # ничего не делает: скрытые тесты обязаны упасть
        return {"kind": kind, "rc": 0, "wall_sec": 0.0, "usage": {}, "usage_parsed": True}

    if kind == "oracle":
        # накатывает исходники эталонного PR (без тестов): обязаны пройти.
        # Проверяет каркас, а не модель.
        paths = changed_sources(clone, task)
        if not paths:
            return {"kind": kind, "rc": 1, "wall_sec": time.time() - t0,
                    "usage": {}, "usage_parsed": True,
                    "error": "не удалось получить список исходников эталонного коммита"}
        try:
            extract_from_reference(clone, task.solution_commit, wt, paths)
            rc, err = 0, ""
        except RuntimeError as e:
            rc, err = 1, str(e)
        (run_dir / "agent_stderr.log").write_text(err, encoding="utf-8")
        return {"kind": kind, "rc": rc, "wall_sec": time.time() - t0,
                "usage": {}, "usage_parsed": True}

    model = cfg["agent"].get("model", "").strip()
    if (not model or model in {"sonnet", "opus", "haiku"}
            or not re.fullmatch(
                r"claude-(?:sonnet|opus|haiku|fable|mythos)-\d+(?:-\d+)*(?:-\d{8})?",
                model,
            )):
        return {"kind": kind, "rc": 2, "wall_sec": time.time() - t0,
                "usage": {}, "usage_parsed": False,
                "error": "agent.model должен быть полным pinned Claude API id"}
    effort = cfg["agent"].get("effort", "").strip()
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        return {"kind": kind, "rc": 2, "wall_sec": time.time() - t0,
                "usage": {}, "usage_parsed": False,
                "error": "agent.effort должен быть low/medium/high/xhigh/max"}
    cmd = [c.replace("{prompt}", prompt).replace("{model}", model).replace("{effort}", effort)
           for c in cfg["agent"]["cmd"]]
    _, cli_version, _ = sh([cmd[0], "--version"], cwd=wt, env=env_extra, timeout=30)
    rc, out, err = sh(cmd, cwd=wt, env=env_extra, timeout=cfg["agent"].get("timeout_sec", 3600))
    (run_dir / "agent_stdout.log").write_text(out, encoding="utf-8")
    (run_dir / "agent_stderr.log").write_text(err, encoding="utf-8")

    exploration = parse_stream_events(out, run_dir)

    usage, parsed = {}, False
    try:  # json отдаёт один объект, stream-json — последний эвент type=result
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = next(
                (json.loads(l) for l in reversed(out.splitlines())
                 if l.strip().startswith("{") and '"type"' in l and '"result"' in l),
                None,
            )
            if payload is None:
                raise
        u = payload.get("usage", {})
        usage = {
            "input_tokens": u.get("input_tokens"),
            "output_tokens": u.get("output_tokens"),
            "cache_read_tokens": u.get("cache_read_input_tokens"),
            "cache_creation_tokens": u.get("cache_creation_input_tokens"),
            "total_cost_usd": payload.get("total_cost_usd"),
            "num_turns": payload.get("num_turns"),
        }
        parsed = True
    except Exception as e:
        print(f"[usage] не разобрал вывод агента как JSON ({e}); ценовая ось будет пустой",
              file=sys.stderr)

    return {"kind": kind, "rc": rc, "wall_sec": time.time() - t0,
            "model": model, "effort": effort, "cli_version": cli_version.strip(),
            "usage": usage, "usage_parsed": parsed, "exploration": exploration}


# --------------------------------------------------------------------------- проверки


def capture_diff(wt: Path, run_dir: Path, exclude: list[str] | None = None) -> dict:
    """
    Из диффа выкидываем всё, что положил или убрал сам раннер:
      * .claude — наш служебный каталог; иначе memory-on систематически «на файл
        больше», а llm-judge, который «читает только дифф», узнаёт из него режим;
      * удалённые AGENTS.md / CLAUDE.md — иначе прогон, где агент не сделал ничего,
        отчитывается как «-238 строк».
    """
    specs = [".", ":(exclude).claude", ":(exclude).kata-bin",
             ":(exclude).kata-hooks", ":(exclude).kata-run", ":(exclude).uv-cache"]
    specs += [f":(exclude){p}" for p in (exclude or [])]
    sh(["git", "add", "-A", "--", *specs], cwd=wt)
    _, diff, _ = sh(["git", "diff", "--cached"], cwd=wt)
    (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
    _, stat, _ = sh(["git", "diff", "--cached", "--numstat"], cwd=wt)
    rows = [l.split("\t") for l in stat.splitlines() if l.strip()]
    touched = [r[2] for r in rows if len(r) == 3]
    return {
        "files_changed": len(touched),
        "touched": touched[:60],
        "agent_touched_tests": any(p.startswith("tests/") for p in touched),
        "insertions": sum(int(r[0]) for r in rows if r[0].isdigit()),
        "deletions": sum(int(r[1]) for r in rows if r[1].isdigit()),
    }


def run_tests(wt: Path, cfg, targets: list[str], run_dir: Path, label: str,
              ignore: list[str] | None = None) -> dict:
    xml = run_dir / f"{label}.xml"
    cmd = (cfg["repo"]["test_cmd"].split()
           + ["-q", "--no-header", "-p", "no:cacheprovider", f"--junitxml={xml}"]
           + [f"--ignore={p}" for p in (ignore or [])]
           + targets)
    rc, out, err = sh(cmd, cwd=wt, env=cfg["repo"].get("env", {}),
                      timeout=cfg["repo"].get("test_timeout_sec", 1800))
    (run_dir / f"pytest_{label}.log").write_text(out + err, encoding="utf-8")
    return parse_junit(xml, rc)


def overlay_hidden_tests(clone: Path, wt: Path, task: Task) -> None:
    extract_from_reference(clone, task.solution_commit, wt, task.hidden_tests)


# --------------------------------------------------------------------------- прогон


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="dataset/runner/config.toml")
    ap.add_argument("--tasks", default="dataset/tasks.yaml")
    ap.add_argument("--task", required=True)
    ap.add_argument("--mode", choices=["memory-off", "memory-on"], required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--agent", default=None, help="claude | codex | null | oracle")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--skip-setup", action="store_true")
    ap.add_argument("--keep-worktree", action="store_true",
                    help="не удалять рабочее дерево после прогона (для разбора)")
    args = ap.parse_args()

    cfg = tomllib.loads((ROOT / args.config).read_text(encoding="utf-8"))
    meta, tasks = load_tasks(ROOT / args.tasks)
    if args.task not in tasks:
        sys.exit(f"нет задачи {args.task}; есть: {', '.join(tasks)}")
    task = tasks[args.task]
    kind = args.agent or cfg["agent"]["kind"]
    write_back = bool(cfg.get("memory", {}).get("write_back", False))

    run_dir = ROOT / args.out / task.id / args.mode / f"seed{args.seed}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    clone = ensure_clone(cfg)

    problems = validate_task(clone, task)
    if problems:
        for p in problems:
            print(f"[валидация] {p}", file=sys.stderr)
        print("[валидация] задача не готова к прогону, токены не тратим", file=sys.stderr)
        return 3

    workspace_root = Path(tempfile.gettempdir()) / "kata-eval-workspaces"
    wt, removed = make_workspace(
        clone,
        task.base_commit,
        workspace_root / f"{task.id}-{args.mode}-{args.seed}",
        cfg["repo"].get("strip_files", []),
    )
    try:
        print(f"[workspace] {task.id} @ {task.base_commit}, убрано: {removed or '—'}")

        install_agent_settings(wt, args.mode == "memory-on", write_back)

        if not args.skip_setup:
            rc, out, err = sh(cfg["repo"]["setup_cmd"], cwd=wt,
                              env=cfg["repo"].get("env", {}), timeout=3600)
            (run_dir / "setup.log").write_text(out + err, encoding="utf-8")
            if rc != 0:
                print("[setup] упал, дальше идти бессмысленно", file=sys.stderr)
                return 2

        # Runtime копируется в workspace: subprocess sandbox не получает доступ
        # к reference clone, датасету и снапшоту на хосте.
        env_extra = install_agent_runtime(wt, cfg, args.mode == "memory-on")
        if args.mode == "memory-on":
            env_extra.update({
                "KATA_TASK_ID": task.id,
                "KATA_SEED": str(args.seed),
            })

        print(f"[agent] {kind}, режим {args.mode}, сид {args.seed}"
              + (", запись памяти включена" if args.mode == "memory-on" and write_back else ""))
        agent = run_agent(kind, cfg, wt, task, clone, run_dir, env_extra)
        collect_hook_artifacts(wt, run_dir)
        if agent["rc"] not in (0, None):
            print(f"[agent] rc={agent['rc']} — прогон пойдёт дальше, но смотри логи",
                  file=sys.stderr)

        ctx = run_dir / "context_injected.txt"
        ctx_chars = len(ctx.read_text(encoding="utf-8")) if ctx.exists() else 0
        # Молчаливый memory-on, в который ничего не приехало, — это memory-off
        # под другим именем. Такой прогон не считается.
        memory_ok = not (args.mode == "memory-on" and kind not in ("null", "oracle")
                         and ctx_chars == 0)
        if not memory_ok:
            print("[память] в контекст ничего не уехало: снапшот пуст или хук не отработал.\n"
                  "         Прогон помечен невалидным — сравнивать его с memory-off нельзя.",
                  file=sys.stderr)

        diff = capture_diff(wt, run_dir, exclude=removed)
        print(f"[diff] файлов {diff['files_changed']}, +{diff['insertions']}/-{diff['deletions']}")

        # регрессия — на дереве агента, до наложения скрытых тестов.
        # Скрытые тесты исключены: в старой редакции они могут честно упасть
        # на правильной реализации, и это не «сломал чужое».
        regression = run_tests(wt, cfg, [cfg["repo"]["regression_scope"]], run_dir,
                               "regression", ignore=task.hidden_tests)

        overlay_hidden_tests(clone, wt, task)
        hidden = run_tests(wt, cfg, task.hidden_tests, run_dir, "hidden")

        invalid_reasons = []
        if agent["rc"] != 0:
            invalid_reasons.append(f"agent_rc={agent['rc']}")
        if kind not in ("null", "oracle") and not agent["usage_parsed"]:
            invalid_reasons.append("usage_unparsed")
        if kind not in ("null", "oracle") and not agent.get("cli_version"):
            invalid_reasons.append("agent_cli_version_missing")
        if not memory_ok:
            invalid_reasons.append("memory_not_injected")
        if not regression["parsed"]:
            invalid_reasons.append("regression_junit_missing")
        if not hidden["parsed"]:
            invalid_reasons.append("hidden_junit_missing")
        valid_run = not invalid_reasons
        task_success = valid_run and regression["green"] and hidden["green"]

        metrics = {
            "task": task.id,
            "title": task.title,
            "mode": args.mode,
            "seed": args.seed,
            "agent": agent["kind"],
            "agent_rc": agent["rc"],
            "agent_model": agent.get("model", cfg["agent"].get("model")),
            "agent_effort": agent.get("effort", cfg["agent"].get("effort")),
            "agent_cli_version": agent.get("cli_version"),
            "agent_cmd": cfg["agent"].get("cmd") if kind not in ("null", "oracle") else None,
            "memory_mode": cfg["memory"]["mode"],
            "memory_write_back": write_back,
            "memory_ok": memory_ok,
            "base_commit": task.base_commit,
            "solution_commit": task.solution_commit,
            "c0": meta.get("c0"),
            "slices": task.slices,
            "wall_sec": round(agent["wall_sec"], 1),
            "usage": agent["usage"],
            "exploration": agent.get("exploration") or {},
            "usage_parsed": agent["usage_parsed"],
            "valid_run": valid_run,
            "invalid_reasons": invalid_reasons,
            "task_success": task_success,
            "diff": diff,
            "regression": regression,
            "hidden": hidden,
            "score": hidden["ratio"],          # доля пройденных проверок, не бинарь
            "score_binary": 1.0 if hidden["green"] else 0.0,
            "context_injected_chars": ctx_chars,
        }
        (run_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")

        verdict = "ЗЕЛЁНО" if hidden["green"] else "красно"
        print(f"[итог] скрытые: {verdict} "
              f"({hidden['passed']}/{max(hidden['tests'] - hidden['skipped'], 0)} "
              f"= {hidden['ratio']}), регрессия: {'ok' if regression['green'] else 'красная'}"
              + ("" if memory_ok else ", ПАМЯТЬ НЕ ПРИЕХАЛА"))
        print(f"[итог] артефакты в {run_dir}")
        return 0 if valid_run else 4
    finally:
        if not args.keep_worktree:
            drop_workspace(wt)


if __name__ == "__main__":
    raise SystemExit(main())
