"""
Laufzeitmessung der deterministischen Stufe 1 über alle 34 Exportarchive.

Jedes Archiv wird RUNS-mal vollständig ausgewertet (Entpacken im Arbeitsspeicher + Stufe 1,
ohne LLM). Gemessen wird die Zeit für Stufe 1 pro Archiv; berichtet werden Median und Maximum
über alle Einzelmessungen sowie die Hardware. Die Werte werden als LaTeX-Makros nach
thesis/generated/messwerte.tex geschrieben und dort von Kapitel 6 eingebunden.

Aufruf im Repository-Wurzelverzeichnis auf dem Referenzrechner:
    python scripts/measure_performance.py --runs 20
"""
import argparse
import glob
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
os.chdir(REPO)
os.environ["LLM_API_KEY"] = ""

from rule_engine import _parse_archive_bytes, evaluate_stage1_ast, load_rules  # noqa: E402


def cpu_name() -> str:
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            if out:
                return out.splitlines()[0].strip()
        elif platform.system() == "Linux":
            for line in open("/proc/cpuinfo", encoding="utf-8"):
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        elif platform.system() == "Darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    return platform.processor() or "unbekannt"


def short_cpu(name: str) -> str:
    """Prozessorname für den Fließtext: ohne Markenzeichen, Generationsvorsatz und Taktangabe."""
    name = re.sub(r"\((R|TM)\)|[\u00ae\u2122]", "", name, flags=re.I)
    name = re.sub(r"^\s*\d+(st|nd|rd|th) Gen\s+", "", name, flags=re.I)
    name = re.sub(r"\s+CPU\b.*$|\s+@.*$", "", name)
    return " ".join(name.split())


def tex_num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", "{,}")


def tex_escape(s: str) -> str:
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("_", r"\_"), ("#", r"\#"), ("$", r"\$"), ("{", r"\{"), ("}", r"\}")):
        s = s.replace(a, b)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    args = ap.parse_args()
    reg = load_rules()
    files = sorted(glob.glob("data/raw_forms/prod/*.xfp")) + sorted(glob.glob("data/raw_forms/test/**/*.xfp", recursive=True))
    samples, per_file = [], {}
    for f in files:
        raw = open(f, "rb").read()
        times = []
        for _ in range(args.runs):
            fd, em, st = _parse_archive_bytes(raw, os.path.basename(f))
            t0 = time.perf_counter()
            evaluate_stage1_ast(fd, em, st, os.path.basename(f), reg)
            times.append((time.perf_counter() - t0) * 1000)
        per_file[f] = statistics.median(times)
        samples.extend(times)
    slowest = max(per_file, key=per_file.get)
    cpu = cpu_name()
    hw = f"{cpu}, {platform.system()} {platform.release()}, Python {platform.python_version()}"
    rel = re.match(r"\d+(\.\d+)?", platform.release())
    hw_text = f"{short_cpu(cpu)}, {platform.system()} {rel.group(0) if rel else platform.release()}, Python {platform.python_version()}"
    result = {
        "archives": len(files),
        "runs_per_archive": args.runs,
        "median_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[int(0.95 * (len(samples) - 1))],
        "max_ms": max(samples),
        "slowest_archive": os.path.basename(slowest),
        "slowest_archive_median_ms": per_file[slowest],
        "hardware": hw,
        "measured_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    os.makedirs("data/evaluation", exist_ok=True)
    with open("data/evaluation/performance_stage1.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    with open("thesis/generated/messwerte.tex", "w", encoding="utf-8") as fh:
        fh.write("% Automatisch erzeugt von scripts/measure_performance.py – nicht von Hand bearbeiten.\n")
        fh.write(f"\\renewcommand{{\\StufeEinsAnzahlArchive}}{{{len(files)}}}\n")
        fh.write(f"\\renewcommand{{\\StufeEinsLaeufe}}{{{args.runs}}}\n")
        fh.write(f"\\renewcommand{{\\StufeEinsMedianMs}}{{{tex_num(result['median_ms'])}}}\n")
        fh.write(f"\\renewcommand{{\\StufeEinsPfuenfundneunzigMs}}{{{tex_num(result['p95_ms'])}}}\n")
        fh.write(f"\\renewcommand{{\\StufeEinsMaxMs}}{{{tex_num(result['max_ms'])}}}\n")
        fh.write(f"\\renewcommand{{\\StufeEinsHardware}}{{{tex_escape(hw_text)}}}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
