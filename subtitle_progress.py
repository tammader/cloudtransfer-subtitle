# -*- coding: utf-8 -*-
"""字幕批量进度统计(日志输出, TG 已取消)

统计源目录(默认 gd2:out3)里:
  - 已有 srt 数(视频组完成字幕)
  - (旧) 字幕版/ 下硬字幕 mp4 数 —— 流程已不烧录, 该值恒为 0
  - 分组总数 / 待处理组数
并结合最近几轮 subtitle-translate 的运行状态, 拼报告打日志。

环境变量: RCLONE_CONF 已写入 ~/.config/rclone; SUB_SRC/SUB_DIR 由 PATHS_JSON 给。
TG 已取消, 统计只打日志。
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

_RE_PART = re.compile(r"(?:^|_)(?P<base>.+?)\.part(?P<idx>\d+)(?:_[A-Za-z])?\.(mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)
_RE_PLAIN = re.compile(r"(?:^|_)(?P<base>.+?)\.(mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


# TG 已取消: 统计只打日志


def rclone_files(remote, path):
    import subprocess
    p = subprocess.run(["rclone", "lsf", "%s:%s" % (remote, path), "--files-only", "--max-depth", "1"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:200])
    return [l.strip() for l in p.stdout.splitlines() if l.strip()]


def collect():
    """统计: 组数 / 与视频同名的 srt 数 / 还没配齐同名 srt 的组数。

    新命名规则: 每个视频文件旁应有同名 srt(<视频文件名去扩展名>.srt);
    一组内所有分片都配齐同名 srt 才算该组完成。
    """
    files = rclone_files(SRC_REMOTE, SRC_DIR)
    groups = {}          # 番号 -> {视频文件名, ...}
    srt_all = set()      # 目录里所有 .srt
    vid_stem = set()     # 所有视频文件去扩展名
    for fn in files:
        if fn.lower().endswith(".srt"):
            srt_all.add(fn)
            continue
        vid_stem.add(os.path.splitext(fn)[0])
        m = _RE_PART.search(fn) or _RE_PLAIN.search(fn)
        if m:
            groups.setdefault(m.group("base"), set()).add(fn)
    # 与视频同名的 srt
    srt_matched = set(s for s in srt_all if s[:-4] in vid_stem)
    # 组完成判定
    done = 0
    for _base, parts in groups.items():
        need = set(os.path.splitext(p)[0] + ".srt" for p in parts)
        if need <= srt_all:
            done += 1
    return {
        "total_groups": len(groups),
        "srt": len(srt_matched),          # 已配字幕的视频文件数
        "burn": 0,                        # 流程已不烧录
        "pending": len(groups) - done,    # 还没配齐同名 srt 的组数
        "srt_no_burn": 0,
        "orphan_srt": len(srt_all) - len(srt_matched),   # 没有同名视频的 srt
    }


def gh_runs():
    """最近几轮 subtitle-translate 运行概况"""
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        return None
    url = "https://api.github.com/repos/maddertam-oss/cloudtransfer/actions/runs?per_page=20"
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
    s += "\n\n(触发新批次: Actions → subtitle-translate → Run workflow)"
    return s


def main():
    ap = argparse.ArgumentParser()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
