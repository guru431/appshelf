"""Подставной ipatool для test_ipatool.py.

Ответы — из $HOME/scenario.json: {"<команда>": {"code": 0, "out": {...} | [...], "sleep": 0, "copy": "<ipa>"}}.
Команда — позиционные аргументы после `--format json`: "auth login", "list-purchases", "download"…
Каждый вызов дописывается в $HOME/calls.jsonl: argv, stdin, https_proxy, IPATOOL_DEVICE_MAC. JSON — ASCII (кодировка консоли Windows).
"""
import json
import os
import shutil
import sys
import time

home = os.environ["HOME"]
args = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.path.join(home, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "stdin": stdin, "https_proxy": os.environ.get("https_proxy", ""),
                        "device_mac": os.environ.get("IPATOOL_DEVICE_MAC", "")}) + "\n")
pos = [a for a in args[2:] if not a.startswith("-")]          # args[:2] == ["--format", "json"]
cmd = " ".join(pos[:2]) if pos and pos[0] == "auth" else (pos[0] if pos else "")
with open(os.path.join(home, "scenario.json"), encoding="utf-8") as f:
    step = json.load(f).get(cmd, {"code": 1, "out": {"error": "error", "message": "no scenario for " + cmd}})
time.sleep(step.get("sleep", 0))
out = step.get("out")
if "copy" in step:
    dest = os.path.join(args[args.index("-o") + 1], "app.ipa")
    shutil.copyfile(step["copy"], dest)
    out = {"output": dest, "purchased": False, "success": True}
if out is not None:
    print(json.dumps(out))
sys.exit(step.get("code", 0))
