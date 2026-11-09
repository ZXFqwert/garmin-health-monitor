"""早上简报脚本里加的一段：把昨晚身体状态和最近一天的邮件拼进简报素材。没登录、没授权就跳过。"""
import os
# 机主的生活数据（li33-life）：昨晚睡眠和身体状态、最近一天的邮件。没登录、没授权就跳过，不影响简报。
import subprocess
LIFE = [os.path.expanduser("~/.local/share/li33-life/venv/bin/python"), os.path.expanduser("~/.local/share/li33-life/life.py")]
for args in (["health"], ["mail", "--days", "1"]):
    try:
        out = subprocess.run(LIFE + args, capture_output=True, text=True, timeout=90)
        text = (out.stdout or out.stderr or "").strip()
        if text:
            print("\n" + text)
    except Exception as exc:
        print("\n（li33-life %s 暂时取不到：%s）" % (args[0], exc))
