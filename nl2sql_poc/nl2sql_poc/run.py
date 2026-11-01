#!/usr/bin/env python3
"""
run.py - ask questions in plain English, get SQL + results.

  python run.py "How many households are enrolled in AutoBill?"
  python run.py --file demo_questions.txt          # one question per line
  python run.py                                    # interactive prompt
  python run.py --csv result.csv "..."             # also save the rows
  python run.py --show-prompt                      # print the system prompt and exit
  python run.py --config clients/other.yaml "..."  # a different client's config
"""
import argparse
import csv
import sys
from pathlib import Path

import yaml

from app.db import build_db
from app.llm import build_llm
from app.pipeline import Pipeline


def cell(v, width=28):
    if v is None:
        s = "NULL"
    elif isinstance(v, float):
        s = f"{v:,.2f}"
    else:
        s = str(v)
    return s if len(s) <= width else s[: width - 1] + "…"


def show_table(columns, rows, limit=20):
    shown = rows[:limit]
    widths = [max(len(c), *(len(cell(r[i])) for r in shown)) if shown else len(c)
              for i, c in enumerate(columns)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths))
    print(line(columns))
    print(line(["-" * w for w in widths]))
    for r in shown:
        print(line([cell(v) for v in r]))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows not shown")


def show(res):
    print(f"\n> {res.question}")
    if res.status == "chat":
        print(f"\n{res.clarification}")
    elif res.status == "clarify":
        print(f"\nThe model needs one detail first: {res.clarification}")
    elif res.status == "ok":
        for a in res.assumptions:
            print(f"  assumption: {a}")
        print(f"\n{res.sql}\n")
        show_table(res.columns, res.rows)
        note = " (truncated at the row cap)" if res.truncated else ""
        print(f"\n{len(res.rows)} row(s){note}")
    else:
        print(f"\nFAILED after {res.attempts} attempt(s): {res.error}")
        if res.sql:
            print(f"last SQL tried:\n{res.sql}")
    cached = f", {res.cache_read_tokens} cached" if res.cache_read_tokens else ""
    print(f"[{res.request_id}] {res.attempts} attempt(s), {res.latency_s}s, "
          f"{res.input_tokens} in / {res.output_tokens} out tokens{cached}")


def save_csv(res, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(res.columns)
        w.writerows(res.rows)
    print(f"saved {len(res.rows)} rows to {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?", help="question in plain English")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--file", help="text file with one question per line")
    ap.add_argument("--csv", help="save the result rows to this CSV (single question only)")
    ap.add_argument("--show-prompt", action="store_true", help="print the system prompt and exit")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    try:
        pipe = Pipeline(build_llm(cfg["llm"]), build_db(cfg["db"]), cfg, base_dir=cfg_path.parent)
    except Exception as e:
        sys.exit(f"Setup failed: {e}")

    if args.show_prompt:
        print(pipe.system)
        return

    if args.file:
        questions = [q.strip() for q in Path(args.file).read_text().splitlines()
                     if q.strip() and not q.startswith("#")]
    elif args.question:
        questions = [args.question]
    else:
        questions = None                                   # interactive

    if questions is not None:
        for q in questions:
            res = pipe.ask(q)
            show(res)
            if args.csv and res.status == "ok":
                save_csv(res, args.csv)
    else:
        print("Ask a question (blank line to quit).")
        while True:
            try:
                q = input("\n? ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            show(pipe.ask(q))


if __name__ == "__main__":
    main()
