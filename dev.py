"""
Auto-restart watcher — like nodemon for Python.
Usage:
    python dev.py bot        → runs nifty_zerodha_bot.py with auto-restart
    python dev.py backtest   → runs nifty_zerodha_backtest.py with auto-restart
    python dev.py bot --no-watch  → runs without watching (one-shot)
"""

import subprocess
import sys
import time
import os
from pathlib import Path

SCRIPTS = {
    'bot':      'nifty_zerodha_bot.py',
    'backtest': 'nifty_zerodha_backtest.py',
}

WATCH_EXTENSIONS = {'.py'}
DEBOUNCE_SECONDS = 1.5


def get_py_files(directory):
    return {
        p: p.stat().st_mtime
        for p in Path(directory).iterdir()
        if p.suffix in WATCH_EXTENSIONS and p.name != 'dev.py'
    }


def run_with_watch(script_path):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"\n🔄 [dev] Starting {script_path} (watching for changes...)")
    print(f"   Press Ctrl+C to stop\n")

    while True:
        snapshots = get_py_files(base_dir)
        proc = subprocess.Popen([sys.executable, script_path], cwd=base_dir)

        try:
            while proc.poll() is None:
                time.sleep(DEBOUNCE_SECONDS)
                current = get_py_files(base_dir)
                changed = False
                for f, mtime in current.items():
                    if f not in snapshots or snapshots[f] != mtime:
                        print(f"\n🔄 [dev] Change detected: {f.name}")
                        changed = True
                        break
                for f in snapshots:
                    if f not in current:
                        print(f"\n🔄 [dev] File removed: {f.name}")
                        changed = True
                        break

                if changed:
                    print(f"🔄 [dev] Restarting {script_path}...")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break

            if not changed:
                exit_code = proc.returncode
                if exit_code != 0:
                    print(f"\n❌ [dev] {script_path} exited with code {exit_code}")
                    print(f"   Waiting for file changes to restart...")
                    while True:
                        time.sleep(DEBOUNCE_SECONDS)
                        current = get_py_files(base_dir)
                        restart = False
                        for f, mtime in current.items():
                            if f not in snapshots or snapshots[f] != mtime:
                                restart = True
                                break
                        if restart:
                            print(f"\n🔄 [dev] Change detected, restarting...")
                            break
                else:
                    print(f"\n✅ [dev] {script_path} finished successfully")
                    break

        except KeyboardInterrupt:
            print(f"\n⛔ [dev] Stopping...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            break


def run_once(script_path):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"\n▶️ [dev] Running {script_path}...\n")
    result = subprocess.run([sys.executable, script_path], cwd=base_dir)
    sys.exit(result.returncode)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print("Available scripts:")
        for name, path in SCRIPTS.items():
            print(f"  {name:<12} → {path}")
        sys.exit(0)

    script_name = sys.argv[1]
    no_watch = '--no-watch' in sys.argv

    if script_name not in SCRIPTS:
        print(f"❌ Unknown script: '{script_name}'")
        print(f"   Available: {', '.join(SCRIPTS.keys())}")
        sys.exit(1)

    script_path = SCRIPTS[script_name]

    if not os.path.exists(script_path):
        print(f"❌ File not found: {script_path}")
        sys.exit(1)

    if no_watch:
        run_once(script_path)
    else:
        run_with_watch(script_path)


if __name__ == "__main__":
    main()
