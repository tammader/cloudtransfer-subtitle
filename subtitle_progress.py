# -*- coding: utf-8 -*-
"""字幕批量进度统计 -> Telegram 报告

统计源目录(默认 gdrive6:out3)里:
  - 已有 srt 数(视频组完成字幕)
  - 字幕版/ 下硬字幕 mp4 数
  - 分组总数 / 待处理组数
并结合最近几轮 subtitle-translate 的运行状态, 拼一条 Telegram 消息发出去。

环境变量: RCLONE_CONF 已写入 ~/.config/rclone; SUB_SRC/SUB_DIR 由 PATHS_JSON 给;
         TG_TOKEN / TG_CHAT_ID 用于发消息。
脱敏: 日志走 logmask; 番号/盘名不出现明文。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pathcfg
import logmask

logmask.install()

SRC_REMOTE = os.environ.get("SUB_SRC") or pathcfg.get("SUB_SRC", "")
SRC_DIR = os.environ.get("SUB_DIR") or pathcfg.get("SUB_DIR", "out3")
BURN_DIR = os.environ.get("BURN_DIR", "字幕版")
TG = "https://api.telegram.org"

_RE_PART = re.compile(r"(?:^|_)(?P<base>.+?)\.part(?P<idx>\d+)(?:_[A-Za-z])?\.(mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)
_RE_PLAIN = re.compile(r"(?:^|_)(?P<base>.+?)\.(mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def tg_send(text):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not tok or not chat:
        log("!! 缺 TG_TOKEN/TG_CHAT_ID, 不发送")
        return False
    url = "%s/bot%s/sendMessage" % (TG, tok)
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            r = json.loads(x.read().decode("utf-8", "replace"))
            return bool(r.get("ok"))
    except Exception as e:
        log("!! 发消息失败: %s" % str(e)[:120])
        return False


def rclone_files(remote, path):
    import subprocess
    p = subprocess.run(["rclone", "lsf", "%s:%s" % (remote, path), "--files-only", "--max-depth", "1"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:200])
    return [l.strip() for l in p.stdout.splitlines() if l.strip()]


def collect():
    files = rclone_files(SRC_REMOTE, SRC_DIR)
    groups = set()
    srt = set()
    for fn in files:
        if fn.lower().endswith(".srt"):
            srt.add(fn[:-4])
            continue
        m = _RE_PART.search(fn) or _RE_PLAIN.search(fn)
        if m:
            groups.add(m.group("base"))
    burn = set()
    try:
        for fn in rclone_files(SRC_REMOTE, "%s/%s" % (SRC_DIR, BURN_DIR)):
            if fn.lower().endswith(".mp4") and fn.endswith("_字幕版.mp4"):
                burn.add(fn[:-len("_字幕版.mp4")])
    except Exception as e:
        log("列字幕版失败(视为0): %s" % str(e)[:120])
    return {
        "total_groups": len(groups),
        "srt": len(srt),
        "burn": len(burn),
        "pending": len(groups - srt),
        "srt_no_burn": len(srt - burn),   # 有srt但没硬字幕(待补烧)
    }


def gh_runs():
    """最近几轮 subtitle-translate 运行概况"""
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        return None
    url = "https://api.github.com/repos/tammader/cloudtransfer-subtitle/actions/runs?per_page=20"
    req = urllib.request.Request(url, headers={"Authorization": "token " + tok,
                                               "User-Agent": "wb-substat",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            d = json.loads(x.read().decode("utf-8", "replace"))
    except Exception as e:
        log("!! 查 GitHub 失败: %s" % str(e)[:120])
        return None
    runs = d.get("workflow_runs") or []
    act = [r for r in runs if r["status"] in ("queued", "in_progress")]
    recent = [r for r in runs if r["status"] == "completed"][:10]
    succ = sum(1 for r in recent if r.get("conclusion") == "success")
    fail = sum(1 for r in recent if r.get("conclusion") == "failure")
    return {"active": len(act), "recent_n": len(recent), "succ": succ, "fail": fail}


def build_text(stats, runs):
    t = time.strftime("%m-%d %H:%M")
    s = "🎬 字幕批量进度 · %s\n" % t
    s += "总组 %d | ✅已出字幕 %d | 🎞硬字幕 %d | ⏳待处理 %d" % (
        stats["total_groups"], stats["srt"], stats["burn"], stats["pending"])
    if stats["srt_no_burn"]:
        s += "\n⚠ 有字幕但缺硬字幕: %d 部(待补烧)" % stats["srt_no_burn"]
    pct = (stats["srt"] * 100.0 / stats["total_groups"]) if stats["total_groups"] else 0
    s += "\n完成率 %.1f%%" % pct
    if runs:
        s += "\n近期运行: 成功 %d / 失败 %d / 进行中 %d" % (runs["succ"], runs["fail"], runs["active"])
    s += "\n\n发送 /run subtitle 触发新批次"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not SRC_REMOTE:
        log("!! 缺 SUB_SRC")
        return 1
    stats = collect()
    runs = gh_runs()
    if a.json:
        print(json.dumps({"stats": stats, "runs": runs}, ensure_ascii=False))
        return 0
    text = build_text(stats, runs)
    print(text)
    if not a.no_send:
        ok = tg_send(text)
        log("发送 -> %s" % ("OK" if ok else "FAIL"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
