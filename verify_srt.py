# -*- coding: utf-8 -*-
"""烧录前的 srt 质量闸门 —— 统一校验翻译结果, 通过后才允许进入烧录阶段。

扫描源目录里所有 .srt, 逐条检查:
  - 日文假名残留(没翻译干净)
  - 思考过程泄漏(the user / i need / 用户想 ...)
  - 空条目
按文件汇总坏条目占比, 给出全局结论; --gate 时以退出码卡住烧录批次。

用法:
  python3 verify_srt.py                        # 打印报告(不发 TG)
  python3 verify_srt.py --json                 # 机器可读
  python3 verify_srt.py --tg                   # 报告发 Telegram
  python3 verify_srt.py --gate                 # 闸门: 超阈值 exit 1
  python3 verify_srt.py --gate --max-bad-file-ratio 0.02 --max-bad-seg-ratio 0.05

环境: RCLONE_CONF(已写入 ~/.config/rclone) / PATHS_JSON(SUB_SRC/SUB_DIR) /
      TG_TOKEN / TG_CHAT_ID(可选, 只有 --tg 需要)
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
_LEAK = ("the user", "i need", "i should", "let me", "translate", "translation",
         "用户想", "用户希望", "需要翻译", "翻译成", "we need", "sure,", "当然，以下是",
         "以下是", "好的，", "assistant")


def log(m):
    print(m, flush=True)


def parse_srt(path):
    """只取每条的字幕文本(时间轴不需要)"""
    txt = io.open(path, encoding="utf-8", errors="replace").read()
    out = []
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
        text = " ".join(lines[ti + 1:]).strip()
        if text:
            out.append(text)
    return out


def bad_reason(text):
    t = (text or "").strip()
    if not t:
        return "空"
    if _KANA.search(t):
        return "日文残留"
    low = t.lower()
    for w in _LEAK:
        if w in low:
            return "思考泄漏"
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
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not tok or not chat:
        log("!! 缺 TG_TOKEN/TG_CHAT_ID, 不发 TG")
        return False
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            return bool(json.loads(x.read().decode("utf-8", "replace")).get("ok"))
    except Exception as e:
        log("!! 发 TG 失败: %s" % str(e)[:120])
        return False


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
            bad = [s for s in segs if bad_reason(s)]
            nb = len(bad)
            tot_seg += n
            tot_bad += nb
            files.append({
                "name": fn[:-4],
                "segments": n,
                "bad": nb,
                "bad_ratio": (nb / n) if n else 0.0,
                "whole_untranslated": bool(n and nb / n >= 0.9),
                "sample_reason": bad_reason(bad[0]) if bad else None,
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
