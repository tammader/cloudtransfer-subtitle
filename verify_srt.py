# -*- coding: utf-8 -*-
"""srt 质量校验 —— 当前流程「只出日文 srt, 不翻译, 不烧录」。

扫描源目录里所有 .srt, 逐条检查:
  - 空条目
  - 思考过程/元信息泄漏(模型自述、英文 prompt 残留)
  - 单条过长(字数或时长) —— 说明没切成字幕行
  - 非日文内容(既无假名也无汉字, 基本是杂音/英文乱码)
按文件汇总坏条目占比, 给出全局结论; --gate 时以退出码标红(非 0)。

用法:
  python3 verify_srt.py                        # 打印报告
  python3 verify_srt.py --json                 # 机器可读
  python3 verify_srt.py --tg                   # (兼容参数) 报告打日志
  python3 verify_srt.py --gate                 # 超阈值 exit 1(只看结论)
  python3 verify_srt.py --gate --max-bad-file-ratio 0.02 --max-bad-seg-ratio 0.05

环境: RCLONE_CONF(已写入 ~/.config/rclone) / PATHS_JSON(SUB_SRC/SUB_DIR)
      (TG 已取消, 报告只进 run 日志)
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pathcfg
import logmask

logmask.install()

SRC_REMOTE = os.environ.get("SUB_SRC") or pathcfg.get("SUB_SRC", "")
SRC_DIR = os.environ.get("SUB_DIR") or pathcfg.get("SUB_DIR", "out3")

_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 元信息/思考泄漏的强特征(注意: 日文对白里出现「好的」不影响, 这里只看英文自述与译文前缀)
_LEAK_HEAD = ("the user", "i need", "i should", "let me", "we need",
              "translate the", "translation:", "as follows", "以下是翻译", "以下是译文")
_LEAK_ANY = ("用户想", "用户希望", "需要翻译", "翻译成中文", "assistant:", "sure, here")
# 单条上限(与 translate 的 MAX_SEG_DUR / MAX_SEG_CHARS 对齐, 稍放宽作为阈值)
MAX_SEG_DUR = float(os.environ.get("MAX_SEG_DUR", "8"))
MAX_SEG_CHARS = int(os.environ.get("MAX_SEG_CHARS", "42"))
_TOL_DUR = MAX_SEG_DUR + 2.0
_TOL_CHARS = MAX_SEG_CHARS + 18


def log(m):
    print(m, flush=True)


def parse_srt(path):
    """返回 [(text, dur_sec)]"""
    txt = io.open(path, encoding="utf-8", errors="replace").read()
    out = []

    def _ts(s):
        s = s.strip()
        hh, mm, rest = s.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    for blk in re.split(r"\r?\n\r?\n", txt.strip()):
        lines = [l for l in blk.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        ti = None
        for i, l in enumerate(lines):
            if "-->" in l:
                ti = i
                break
        if ti is None:
            continue
        dur = 0.0
        try:
            a, b = lines[ti].split("-->")
            dur = max(0.0, _ts(b) - _ts(a))
        except Exception:
            pass
        text = " ".join(lines[ti + 1:]).strip()
        out.append((text, dur))
    return out


def bad_reason(text, dur=None):
    """坏条目判定(日文 srt 版): 空 / 泄漏 / 过长 / 非日文"""
    t = (text or "").strip()
    if not t:
        return "空"
    low = t.lower()
    head = low[:40]
    for w in _LEAK_HEAD:
        if w in head:
            return "元信息泄漏"
    for w in _LEAK_ANY:
        if w in low:
            return "元信息泄漏"
    if dur and dur > _TOL_DUR:
        return "单条过长(%.0fs)" % dur
    if len(t) > _TOL_CHARS:
        return "单条过长(%d字)" % len(t)
    if not _KANA.search(t) and not _CJK.search(t):
        return "非日文内容"
    return None


def fetch_srt(tmpdir):
    """把源目录顶层的 .srt 全部拉下来"""
    p = subprocess.run(["rclone", "copy", "%s:%s" % (SRC_REMOTE, SRC_DIR), tmpdir,
                        "--include", "*.srt", "--max-depth", "1",
                        "--transfers", "16", "--checkers", "16", "--retries", "3"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("rclone 拉取 srt 失败: %s" % p.stderr[-200:])
    return sorted(f for f in os.listdir(tmpdir) if f.lower().endswith(".srt"))


def tg_send(text):
    """TG 已取消: 报告只打日志"""
    log(text)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tg", action="store_true")
    ap.add_argument("--gate", action="store_true", help="超阈值则 exit 1(卡住烧录)")
    ap.add_argument("--max-bad-file-ratio", type=float, default=0.02,
                    help="允许的'有问题的 srt 文件'占比上限, 默认 2%%")
    ap.add_argument("--max-bad-seg-ratio", type=float, default=0.05,
                    help="允许的'坏条目'占比上限, 默认 5%%")
    a = ap.parse_args()

    if not SRC_REMOTE:
        log("!! 缺 SUB_SRC(PATHS_JSON)")
        return 1

    tmp = tempfile.mkdtemp(prefix="vsrt_")
    try:
        names = fetch_srt(tmp)
        log("拉到 srt 文件 %d 个" % len(names))
        if not names:
            log("!! 源目录里没有任何 .srt —— 翻译阶段还没产出")
            return 1 if a.gate else 0

        files, tot_seg, tot_bad = [], 0, 0
        for fn in names:
            segs = parse_srt(os.path.join(tmp, fn))
            n = len(segs)
            bad = [(t, d) for t, d in segs if bad_reason(t, d)]
            nb = len(bad)
            tot_seg += n
            tot_bad += nb
            files.append({
                "name": fn[:-4],
                "segments": n,
                "bad": nb,
                "bad_ratio": (nb / n) if n else 0.0,
                "whole_untranslated": bool(n and nb / n >= 0.9),
                "sample_reason": bad_reason(bad[0][0], bad[0][1]) if bad else None,
            })

        bad_files = [f for f in files if f["bad"] > 0]
        whole = [f for f in files if f["whole_untranslated"]]
        bad_file_ratio = len(bad_files) / len(files)
        bad_seg_ratio = (tot_bad / tot_seg) if tot_seg else 0.0

        result = {
            "srt_files": len(files),
            "segments": tot_seg,
            "bad_segments": tot_bad,
            "bad_seg_ratio": round(bad_seg_ratio, 5),
            "bad_files": len(bad_files),
            "bad_file_ratio": round(bad_file_ratio, 5),
            "whole_untranslated": len(whole),
            "thresholds": {"bad_file_ratio": a.max_bad_file_ratio,
                           "bad_seg_ratio": a.max_bad_seg_ratio},
        }
        ok = (bad_file_ratio <= a.max_bad_file_ratio) and (bad_seg_ratio <= a.max_bad_seg_ratio)
        result["pass"] = ok

        if a.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            log("")
            log("=== srt 质量校验 ===")
            log("  srt 文件 %d | 条目 %d | 坏条目 %d (%.2f%%)" % (
                tot_seg and len(files) or len(files), tot_seg, tot_bad, bad_seg_ratio * 100))
            log("  有问题的文件 %d/%d (%.2f%%)  | 整片未翻译 %d" % (
                len(bad_files), len(files), bad_file_ratio * 100, len(whole)))
            log("  阈值: 坏文件<=%.2f%% 且 坏条目<=%.2f%%  -> %s" % (
                a.max_bad_file_ratio * 100, a.max_bad_seg_ratio * 100,
                "通过 ✅" if ok else "不通过 ❌"))
            if bad_files:
                log("")
                log("  最差的前 10 个(文件 / 坏条目占比 / 示例原因):")
                for f in sorted(bad_files, key=lambda x: -x["bad_ratio"])[:10]:
                    log("    %s  %.1f%% (%d/%d)  %s" % (
                        f["name"], f["bad_ratio"] * 100, f["bad"], f["segments"],
                        f["sample_reason"] or ""))

        if a.tg:
            msg = ("%s srt 质量校验\n文件 %d | 坏文件 %d (%.2f%%) | 条目 %d | 坏条目 %d (%.2f%%)\n整片未翻译 %d\n阈值 坏文件<=%.2f%% / 坏条目<=%.2f%%" % (
                "✅" if ok else "❌", len(files), len(bad_files), bad_file_ratio * 100,
                tot_seg, tot_bad, bad_seg_ratio * 100, len(whole),
                a.max_bad_file_ratio * 100, a.max_bad_seg_ratio * 100))
            if not ok:
                msg += "\n\n烧录已被闸门拦住; 修好后再跑。"
            tg_send(msg)

        if a.gate and not ok:
            log("!! 闸门拦截: 质量不达标, 不进入烧录阶段")
            return 1
        return 0
    except Exception as e:
        log("!! 校验异常: %s" % str(e)[:250])
        return 1 if a.gate else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
