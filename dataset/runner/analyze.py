#!/usr/bin/env python3
"""
Разбор прогонов: парные дельты off/on и метрики, которые не дают себя обмануть.

    python dataset/runner/analyze.py --runs runs/sonnet5-medium-seed1-20260901
    python dataset/runner/analyze.py --csv evals/results/2026-09-01-sonnet5-medium-seed1.csv
    python dataset/runner/analyze.py --csv ... --out evals/results/analysis.md

Зачем отдельный инструмент, а не колонки в sweep.

Сводная строка «memory-on на 19% дешевле» верна арифметически и при этом может
не означать ничего. Три способа получить такую строку, не улучшив ничего:

1. **Одна задача делает всю дельту.** Если 65% экономии дал один прогон, это не
   свойство памяти, а свойство прогона. Считаем вклад каждой задачи в общую дельту.
2. **Сэкономил, потому что не сделал.** Агент, который написал меньше кода и
   провалил больше проверок, «дешевле». Нормируем: токены на одну пройденную
   feature-зависимую проверку, а не на прогон.
3. **Сэкономил, потому что сломал.** Красная регрессия и самостоятельная правка
   тестов дисквалифицируют прогон: его экономия в зачёт не идёт.

Feature-зависимые проверки — те, что красные у null-агента. Остальные зелены и
без всякой фичи, и разбавляют картину: в a2 из 44 проверок от задачи зависят 3.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODES = ("memory-off", "memory-on")

# Сколько проверок у задачи зелены ещё до реализации фичи (прогон null-агента).
# Пересчитывается автоматически, если рядом есть runs/_selftest/null.
DEFAULT_BASELINE = {"a1": 15, "a2": 41, "a3": 23, "a4": 0, "a5": 4, "a6": 0}


# --------------------------------------------------------------------------- загрузка


def load_from_runs(runs_dir: Path) -> list[dict]:
    rows = []
    for m in sorted(runs_dir.rglob("metrics.json")):
        if "_selftest" in m.parts:
            continue
        r = json.loads(m.read_text(encoding="utf-8"))
        if r.get("agent") in ("null", "oracle"):
            continue
        u = r.get("usage") or {}
        rows.append({
            "task": r["task"], "mode": r["mode"], "seed": int(r["seed"]),
            "hidden_passed": r["hidden"].get("passed", 0),
            "hidden_total": r["hidden"].get("tests", 0) - r["hidden"].get("skipped", 0),
            "regression_green": bool(r["regression"].get("green")),
            "files_changed": r["diff"]["files_changed"],
            "touched": r["diff"].get("touched", []),
            "insertions": r["diff"]["insertions"],
            "agent_touched_tests": bool(r["diff"]["agent_touched_tests"]),
            "wall_sec": float(r.get("wall_sec") or 0),
            "output_tokens": u.get("output_tokens") or 0,
            "num_turns": u.get("num_turns") or 0,
            "context_chars": r.get("context_injected_chars", 0),
            "exploration": r.get("exploration") or {},
            "solution_commit": r.get("solution_commit"),
            "run_dir": str(m.parent),
        })
    return rows


def load_from_csv(path: Path) -> list[dict]:
    def b(v):
        return str(v).strip().lower() in ("true", "1", "yes")
    rows = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        rows.append({
            "task": r["task"], "mode": r["mode"], "seed": int(r["seed"]),
            "hidden_passed": int(r["hidden_passed"]),
            "hidden_total": int(r["hidden_total"]),
            "regression_green": b(r.get("regression_green")),
            "files_changed": int(r.get("files_changed") or 0),
            "touched": [],
            "insertions": int(r.get("insertions") or 0),
            "agent_touched_tests": b(r.get("agent_touched_tests")),
            "wall_sec": float(r.get("wall_sec") or 0),
            "output_tokens": int(r.get("output_tokens") or 0),
            "num_turns": int(r.get("num_turns") or 0),
            "context_chars": int(r.get("context_chars") or 0),
            "exploration": {},
            "solution_commit": None,
            "run_dir": "",
        })
    return rows


def detect_baseline(runs_dir: Path | None) -> dict[str, int]:
    """Feature-зависимые проверки считаем от null-агента, а не на глаз."""
    if not runs_dir:
        return dict(DEFAULT_BASELINE)
    found = {}
    for m in runs_dir.rglob("_selftest/null/*/*/*/metrics.json"):
        r = json.loads(m.read_text(encoding="utf-8"))
        found[r["task"]] = r["hidden"].get("passed", 0)
    return found or dict(DEFAULT_BASELINE)


# --------------------------------------------------------------------------- метрики


def disqualified(r: dict) -> list[str]:
    """Причины, по которым экономию этого прогона нельзя засчитывать."""
    why = []
    if not r["regression_green"]:
        why.append("красная регрессия")
    if r["agent_touched_tests"]:
        why.append("правил тесты")
    return why


def pair_rows(rows: list[dict]) -> dict[tuple[str, int], dict[str, dict]]:
    pairs: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    for r in rows:
        pairs[(r["task"], r["seed"])][r["mode"]] = r
    return {k: v for k, v in pairs.items() if set(v) == set(MODES)}


def fmt_pct(a: float, b: float) -> str:
    return "—" if not a else f"{(b - a) / a * 100:+.1f}%"



# --------------------------------------------------------------------------- доп. срезы


def section_exploration(pairs) -> list[str]:
    """
    Разведка против исполнения.

    Механизм гипотезы: память заменяет разведку, а не работу. Общие токены этого
    не видят — агент может «сэкономить», просто сделав меньше. Здесь смотрим,
    сколько инструментов ушло до первой правки кода и какая доля прочитанного
    оказалась нужной. Если память работает, первая цифра падает, вторая растёт,
    а число правок остаётся тем же.

    Считается из agent_events.jsonl, то есть требует --output-format stream-json.
    """
    have = [p for p in pairs.values()
            if p["memory-off"].get("exploration") and p["memory-on"].get("exploration")]
    if not have:
        return ["## Разведка против исполнения\n",
                "Нет данных: прогон сделан с `--output-format json`, следов инструментов не осталось. "
                "Переключи агента на `stream-json` (см. `config.example.toml`) — тогда появятся "
                "ходы до первой правки и точность разведки.\n"]
    out = ["## Разведка против исполнения\n",
           "| метрика | off | on | изменение |", "| --- | ---: | ---: | ---: |"]

    def avg(mode, key):
        vals = [p[mode]["exploration"].get(key) for p in have]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return statistics.mean(vals) if vals else 0.0

    for key, label in (("tool_calls", "тул-колов на прогон"),
                       ("calls_before_first_edit", "тул-колов до первой правки"),
                       ("share_before_first_edit", "доля ходов до первой правки"),
                       ("files_read", "прочитано файлов"),
                       ("files_edited", "изменено файлов"),
                       ("exploration_precision", "точность разведки")):
        a, b = avg("memory-off", key), avg("memory-on", key)
        out.append(f"| {label} | {a:,.2f} | {b:,.2f} | {fmt_pct(a, b)} |")
    out.append("")
    out.append("Падение «до первой правки» при неизменном числе правок — это и есть "
               "заявленный эффект. Падение обеих цифр сразу значит, что агент просто "
               "сделал меньше.\n")
    return out


def section_reference_overlap(pairs, clone: Path | None, tasks_file: Path) -> list[str]:
    """
    Попадание в файлы эталонного PR.

    Даёт сигнал там, где скрытые тесты молчат: если обе стороны красные, всё равно
    видно, кто ближе к тому, что реально трогал автор PR. Память должна поднимать
    именно precision — меньше правок мимо цели.
    """
    if not clone:
        return []
    try:
        import yaml as _y
        doc = _y.safe_load(tasks_file.read_text(encoding="utf-8"))
        sol = {t["id"]: t["solution_commit"] for t in doc.get("tasks", [])}
    except Exception:
        return []
    import subprocess

    def ref_files(sha: str) -> set[str]:
        r = subprocess.run(["git", "show", "--diff-filter=ACMR", "--name-only", "--format=", sha],
                           cwd=clone, capture_output=True, text=True)
        return {f for f in r.stdout.split() if f and not f.startswith("tests/")}

    rows = []
    for (task, seed), p in sorted(pairs.items()):
        sha = sol.get(task)
        if not sha:
            continue
        ref = ref_files(sha)
        if not ref:
            continue
        cell = {}
        for mode in MODES:
            got = {f for f in p[mode].get("touched", []) if not f.startswith("tests/")}
            if not got:
                cell[mode] = None
                continue
            hit = len(got & ref)
            cell[mode] = (hit / len(got), hit / len(ref))
        if cell.get("memory-off") or cell.get("memory-on"):
            rows.append((task, seed, cell))
    if not rows:
        return ["## Попадание в файлы эталона\n",
                "Нет данных: нужны артефакты прогонов (`--runs`), из CSV список файлов не восстановить.\n"]

    out = ["## Попадание в файлы эталона\n",
           "Считаем по путям исходников, тесты исключены. Метрика работает даже когда "
           "обе стороны провалили скрытые тесты — видно, кто ближе к тому, что трогал автор PR.\n",
           "| задача | сид | precision off | recall off | precision on | recall on |",
           "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for task, seed, cell in rows:
        def f(m, i):
            v = cell.get(m)
            return f"{v[i]:.2f}" if v else "—"
        out.append(f"| {task} | {seed} | {f('memory-off', 0)} | {f('memory-off', 1)} | "
                   f"{f('memory-on', 0)} | {f('memory-on', 1)} |")
    out.append("")
    return out


# --------------------------------------------------------------------------- отчёт


def report(rows: list[dict], baseline: dict[str, int],
           clone: Path | None = None,
           tasks_file: Path | None = None) -> str:
    out: list[str] = []
    w = out.append
    pairs = pair_rows(rows)
    if not pairs:
        return "нет ни одной полной пары memory-off / memory-on"

    seeds = sorted({s for _, s in pairs})
    w(f"# Разбор прогонов\n")
    w(f"Пар off/on: **{len(pairs)}**, задач: **{len({t for t, _ in pairs})}**, "
      f"повторов: **{len(seeds)}** ({', '.join(map(str, seeds))}).\n")
    if len(seeds) < 3:
        w("> Повторов меньше трёх. Всё ниже — описание того, что произошло, "
          "а не вывод о влиянии памяти: разброс модели не отделён от эффекта.\n")

    # --- 1. по задачам --------------------------------------------------------
    w("## По задачам\n")
    w("| задача | сид | feat off | feat on | Δ проверок | out off | out on | Δ токенов | дисквалификация |")
    w("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    contrib: dict[str, int] = defaultdict(int)
    for (task, seed), p in sorted(pairs.items()):
        o, n = p["memory-off"], p["memory-on"]
        b = baseline.get(task, 0)
        fo, fn = o["hidden_passed"] - b, n["hidden_passed"] - b
        d = n["output_tokens"] - o["output_tokens"]
        contrib[task] += d
        dq = []
        for mode, r in (("off", o), ("on", n)):
            for why in disqualified(r):
                dq.append(f"{mode}: {why}")
        w(f"| {task} | {seed} | {fo} | {fn} | {fn - fo:+d} | {o['output_tokens']:,} | "
          f"{n['output_tokens']:,} | {d:+,} | {'; '.join(dq) or '—'} |")
    w("")

    # --- 2. кто делает заголовок ---------------------------------------------
    tot_off = sum(p["memory-off"]["output_tokens"] for p in pairs.values())
    tot_on = sum(p["memory-on"]["output_tokens"] for p in pairs.values())
    delta = tot_on - tot_off
    w("## Кто делает сводную цифру\n")
    w(f"Суммарно: off **{tot_off:,}** → on **{tot_on:,}**, то есть **{fmt_pct(tot_off, tot_on)}**.\n")
    if delta:
        w("Вклад задач в эту дельту:\n")
        w("| задача | Δ токенов | доля дельты |")
        w("| --- | ---: | ---: |")
        for task, v in sorted(contrib.items(), key=lambda x: x[1]):
            w(f"| {task} | {v:+,} | {v / delta * 100:.0f}% |")
        top = min(contrib.items(), key=lambda x: x[1])
        share = top[1] / delta * 100
        w("")
        if share >= 50:
            rest_off = tot_off - sum(p["memory-off"]["output_tokens"]
                                     for (t, _), p in pairs.items() if t == top[0])
            rest_on = tot_on - sum(p["memory-on"]["output_tokens"]
                                   for (t, _), p in pairs.items() if t == top[0])
            w(f"> **{share:.0f}% всей экономии даёт одна задача — `{top[0]}`.** "
              f"Без неё дельта {fmt_pct(rest_off, rest_on)}. "
              f"Прежде чем выносить сводную цифру на слайд, проверь, что этот прогон "
              f"не сэкономил тем, что сделал меньше или сломал сюит.\n")

    # --- 3. нормировка --------------------------------------------------------
    feat_off = sum(p["memory-off"]["hidden_passed"] - baseline.get(t, 0)
                   for (t, _), p in pairs.items())
    feat_on = sum(p["memory-on"]["hidden_passed"] - baseline.get(t, 0)
                  for (t, _), p in pairs.items())
    w("## Нормированная цена\n")
    w("Токены на прогон — плохая метрика: агент, который меньше сделал, «дешевле». "
      "Делим на пройденные feature-зависимые проверки.\n")
    w("| метрика | off | on | изменение |")
    w("| --- | ---: | ---: | ---: |")
    w(f"| feature-зависимые проверки | {feat_off} | {feat_on} | {feat_on - feat_off:+d} |")
    if feat_off and feat_on:
        co, cn = tot_off / feat_off, tot_on / feat_on
        w(f"| токенов на проверку | {co:,.0f} | {cn:,.0f} | {fmt_pct(co, cn)} |")
    w(f"| токенов всего | {tot_off:,} | {tot_on:,} | {fmt_pct(tot_off, tot_on)} |")
    ws_off = sum(p["memory-off"]["wall_sec"] for p in pairs.values())
    ws_on = sum(p["memory-on"]["wall_sec"] for p in pairs.values())
    w(f"| секунд всего | {ws_off:,.0f} | {ws_on:,.0f} | {fmt_pct(ws_off, ws_on)} |")
    w("")

    # --- 4. дисквалификации ---------------------------------------------------
    bad = [(t, s, m, disqualified(r))
           for (t, s), p in sorted(pairs.items()) for m, r in p.items() if disqualified(r)]
    w("## Прогоны, которые нельзя засчитывать как есть\n")
    if not bad:
        w("Нет: регрессия везде зелёная, тесты агент не трогал.\n")
    else:
        for t, s, m, why in bad:
            w(f"- `{t}` seed{s} **{m}** — {', '.join(why)}")
        w("")
        w("Красная регрессия означает, что агент починил своё и сломал чужое: его экономия "
          "куплена сломанным сюитом. Правка тестов означает, что часть скрытых проверок "
          "могла быть подогнана — такой прогон смотрят глазами, а не считают.\n")

    # --- 5. цена памяти -------------------------------------------------------
    ctx = [p["memory-on"]["context_chars"] for p in pairs.values()]
    if any(ctx):
        uniq = len(set(ctx))
        w("## Цена памяти\n")
        approx_tokens = statistics.mean(ctx) / 4
        w(f"В контекст уезжает {statistics.mean(ctx):,.0f} символов (~{approx_tokens:,.0f} токенов) "
          f"на сессию, {uniq} различных значений на {len(ctx)} прогонов.\n")
        if uniq == 1:
            w("> Всем задачам инжектится **один и тот же** снапшот. Значит проверяется не "
              "«релевантная память помогает», а «полный дамп фактов в промпте помогает». "
              "Пока факты не отбираются под задачу, гипотеза о системном дизайне не проверена — "
              "проверен объём контекста.\n")

    out.extend(section_exploration(pairs))
    out.extend(section_reference_overlap(pairs, clone, tasks_file or ROOT / "dataset/tasks.yaml"))

    # --- 6. разброс -----------------------------------------------------------
    if len(seeds) >= 2:
        w("## Разброс между повторами\n")
        w("| задача | режим | feat-проверки по сидам | out-токены по сидам |")
        w("| --- | --- | --- | --- |")
        for task in sorted({t for t, _ in pairs}):
            for mode in MODES:
                sel = [p[mode] for (t, _), p in sorted(pairs.items()) if t == task]
                f = [r["hidden_passed"] - baseline.get(task, 0) for r in sel]
                tk = [r["output_tokens"] for r in sel]
                w(f"| {task} | {mode} | {', '.join(map(str, f))} | "
                  f"{', '.join(f'{x:,}' for x in tk)} |")
        w("")
        w("Если разброс внутри режима сопоставим с разницей между режимами, сравнивать нечего.\n")

    return "\n".join(out)


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", help="каталог с артефактами прогонов")
    ap.add_argument("--csv", help="готовый results.csv")
    ap.add_argument("--out", help="куда положить отчёт (по умолчанию stdout)")
    ap.add_argument("--clone", help="клон целевого репозитория — включает сравнение с эталонным PR")
    ap.add_argument("--tasks-file", default="dataset/tasks.yaml")
    args = ap.parse_args()

    if not args.runs and not args.csv:
        sys.exit("нужен --runs или --csv")

    runs_dir = Path(args.runs).resolve() if args.runs else None
    rows = load_from_runs(runs_dir) if runs_dir else load_from_csv(Path(args.csv).resolve())
    if not rows:
        sys.exit("не нашёл ни одного прогона")

    text = report(rows, detect_baseline(runs_dir),
                  clone=Path(args.clone).resolve() if args.clone else None,
                  tasks_file=ROOT / args.tasks_file)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"отчёт: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
