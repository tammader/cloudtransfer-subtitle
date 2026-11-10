# -*- coding: utf-8 -*-
"""日文视频 → 中文 SRT 字幕流水线(云端 runner 用)

流程:
  1. 列出源远端(如 gdrive6)目标目录(默认 out3)的 mp4 分片
  2. 按「番号」归组(去掉 已处理_ 前缀 / .partNNN_D 后缀)
  3. 每组的 .partNNN 分片按序号用 ffmpeg concat 拼成整片(单分片直接拷)
  4. faster-whisper (large-v3) 做日文语音转写 -> 日文 srt 段
  5. 调 OpenAI 兼容翻译网关, 每 N 条一批译成简体中文(保留时间轴)
  6. 写中文 .srt, 上传回源远端同目录, 文件名 = <番号>.srt
  7. run 结束前清理本地临时文件(可选)

脱敏: 公开仓库, 日志里不出现真实盘名/目录名/文件名/番号 —— 全部 hash。
      真值来自 pathcfg(CI: Secret PATHS_JSON / 本机: paths.local.json)。

用法:
  python subtitle_translate.py --apply                 # 真跑
  python subtitle_translate.py                          # 演练(只列分组不处理)
  python subtitle_translate.py --only GROUP --apply     # 只处理某一组(按番号)
  python subtitle_translate.py --limit-groups 1 --apply # 最多处理前 N 组

环境变量(CI 里由 workflow 注入):
  SRC_REMOTE        源 rclone 远端名, 默认可由 pathcfg "SUB_SRC" 给
  SRC_DIR           源目录, 默认 pathcfg "SUB_DIR" 或 out3
  SUBTITLE_API_BASE 翻译网关 base, 如 https://xxx/v1
  SUBTITLE_API_KEY  翻译网关 key
  SUBTITLE_MODEL    主模型, 如 glm-5.3
  SUBTITLE_MODEL_FB 备用模型, 如 qwen3.8-max
  WHISPER_MODEL     默认 large-v3
  BATCH_SIZE        每批翻译条数, 默认 40
  BUDGET_MIN        本轮时间预算(分钟)
"""
import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pathcfg
import logmask

logmask.install()

# ---------------- 配置读取(脱敏: 真值走 pathcfg) ----------------
def cfg(key, default=""):
    return os.environ.get(key) or pathcfg.get(key, default)


SRC_REMOTE = cfg("SUB_SRC", "")
SRC_DIR = cfg("SUB_DIR", "out3")
API_BASE = os.environ.get("SUBTITLE_API_BASE", "").rstrip("/")
API_KEY = os.environ.get("SUBTITLE_API_KEY", "")
MODEL = os.environ.get("SUBTITLE_MODEL", "glm-5.3")
MODEL_FB = os.environ.get("SUBTITLE_MODEL_FB", "qwen3.8-max")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "40"))

# 分片命名: [已处理_]<base>.partNNN[_D].mp4  ；<base> 可能含中文/番号/空格
_RE_PART = re.compile(r"(?:^|_)(?P<base>.+?)\.part(?P<idx>\d+)(?:_[A-Za-z])?\.(?P<ext>mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)
_RE_PLAIN = re.compile(r"(?:^|_)(?P<base>.+?)\.(?P<ext>mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)


def now_ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    print("[%s] %s" % (now_ts(), msg), flush=True)


def run(cmd, **kw):
    """跑外部命令, 返回 (rc, stdout, stderr); 日志经 logmask 脱敏"""
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


# ---------------- rclone / 文件分组 ----------------
def rclone_lsf(remote, path):
    rc, out, err = run(["rclone", "lsf", "%s:%s" % (remote, path), "--files-only", "--max-depth", "1"])
    if rc != 0:
        raise SystemExit("rclone lsf 失败: %s" % err[:300])
    return [l.strip() for l in out.splitlines() if l.strip()]


def group_files(files):
    """把分片按 base 归组 -> {base: [(idx, fname), ...]}；非分片单独成组"""
    groups = {}
    for fn in files:
        m = _RE_PART.search(fn)
        if m:
            base = m.group("base")
            idx = int(m.group("idx"))
            groups.setdefault(base, []).append((idx, fn))
        else:
            mp = _RE_PLAIN.search(fn)
            if mp:
                groups.setdefault(mp.group("base"), []).append((0, fn))
    # 排序每个组内分片
    for b in groups:
        groups[b].sort(key=lambda x: x[0])
    return groups


def download_group(remote, path, base, parts, workdir):
    """下载一组分片到 workdir, 返回按序的本地路径列表"""
    locals_ = []
    for idx, fn in parts:
        dst = os.path.join(workdir, "p%04d_%s" % (idx, re.sub(r"[^\w.\-]", "_", os.path.basename(fn))[-60:]))
        rc, out, err = run(["rclone", "copyto", "%s:%s/%s" % (remote, path, fn), dst, "--retries", "3"])
        if rc != 0:
            raise RuntimeError("下载失败 %s: %s" % (fn[:20], err[:200]))
        locals_.append(dst)
    return locals_


def concat_parts(local_parts, outpath):
    """用 ffmpeg concat demuxer 把分片按序拼成整片; 校验/重编码音频确保音轨可用"""
    if len(local_parts) == 1:
        shutil.copyfile(local_parts[0], outpath)
    else:
        lst = outpath + ".txt"
        with io.open(lst, "w", encoding="utf-8") as f:
            for p in local_parts:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        rc, out, err = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", outpath])
        if rc != 0 or not os.path.exists(outpath):
            rc2, out2, err2 = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                                   "-c:v", "libx264", "-c:a", "aac", outpath])
            if rc2 != 0:
                raise RuntimeError("ffmpeg 拼接失败: %s / %s" % (err[-200:], err2[-200:]))
    # 校验音轨: 无音轨 / 音频异常时, 用统一 16k 单声道 wav 作为转写输入
    rc, out, err = run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_name,channels,sample_rate",
                        "-of", "csv=p=0", outpath])
    has_audio = rc == 0 and out.strip() != ""
    log("音轨检测: %s" % (out.strip() if has_audio else "无音轨!"))
    if has_audio:
        wav = outpath + ".wav"
        rc2, o2, e2 = run(["ffmpeg", "-y", "-i", outpath, "-vn", "-ac", "1", "-ar", "16000",
                           "-c:a", "pcm_s16le", wav])
        if rc2 == 0 and os.path.exists(wav) and os.path.getsize(wav) > 1024:
            log("已抽出声轨 wav -> %.1f MB" % (os.path.getsize(wav) / 1048576.0))
            return wav
        log("!! 抽音轨失败, 直接用原视频: %s" % (e2[-150:] if e2 else ""))
    return outpath


# ---------------- ASR ----------------
def transcribe(video_path, whisper_model):
    """faster-whisper 日文转写, 返回 [(start, end, text), ...]"""
    from faster_whisper import WhisperModel
    log("加载 whisper 模型 %s (CPU int8)..." % whisper_model)
    model = WhisperModel(whisper_model, device="cpu", compute_type="int8")

    def _run(use_vad):
        log("开始转写(日文, vad=%s)..." % use_vad)
        kw = dict(language="ja", beam_size=5, condition_on_previous_text=False)
        if use_vad:
            kw["vad_filter"] = True
            kw["vad_parameters"] = {"min_silence_duration_ms": 500, "threshold": 0.3,
                                    "min_speech_duration_ms": 100, "speech_pad_ms": 200}
        else:
            kw["vad_filter"] = False
        segments, info = model.transcribe(video_path, **kw)
        log("检测语种=%s p=%.2f, 时长=%.1fs" % (info.language, info.language_probability, info.duration))
        out = []
        for seg in segments:
            txt = (seg.text or "").strip()
            if txt:
                out.append((seg.start, seg.end, txt))
        return out

    out = _run(True)
    if not out:
        log("!! VAD 后无字幕, 回退到无 VAD 重转写")
        out = _run(False)
    log("转写完成: %d 条字幕" % len(out))
    return out


# ---------------- 翻译 ----------------
def _post_chat(model, messages, timeout=120):
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0.2, "max_tokens": 4000, "stream": False}).encode()
    req = urllib.request.Request(API_BASE + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + API_KEY,
                                          "Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=timeout)
    d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


_SYS = ("你是专业的日译中字幕翻译。你会收到一个 JSON 数组, 每个元素是 {i, t} 字幕条目。"
        "请把每条 t 从日文翻译成简洁自然的简体中文, 保持口语化、符合字幕习惯。"
        "严格只返回一个 JSON 数组, 每个元素 {\"i\": 原i, \"z\": 中文译文}, 顺序与输入一致, 不要任何解释或多余文字。")


def _extract_json_array(text):
    """从模型返回里抠出 JSON 数组(容忍代码块围栏/前后废话)"""
    if not text:
        return None
    # 去 ```json fence
    t = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t).strip()
    # 找第一个 [ ... 最后一个 ]
    s = t.find("[")
    e = t.rfind("]")
    if s >= 0 and e > s:
        cand = t[s:e + 1]
        try:
            return json.loads(cand)
        except Exception:
            pass
    try:
        return json.loads(t)
    except Exception:
        return None


def translate_segments(segs):
    """segs: [(start,end,text)] -> [(start,end,zh)]；每批 BATCH_SIZE 条"""
    result = [None] * len(segs)
    total = len(segs)
    for bs in range(0, total, BATCH_SIZE):
        batch = segs[bs:bs + BATCH_SIZE]
        payload = [{"i": bs + j, "t": b[2]} for j, b in enumerate(batch)]
        user = json.dumps(payload, ensure_ascii=False)
        got = None
        for model in [MODEL, MODEL_FB]:
            for attempt in range(3):
                try:
                    txt = _post_chat(model, [
                        {"role": "system", "content": _SYS},
                        {"role": "user", "content": user},
                    ])
                    arr = _extract_json_array(txt)
                    if isinstance(arr, list) and len(arr) >= 1:
                        got = {int(x["i"]): str(x["z"]).strip() for x in arr if "i" in x and "z" in x}
                        break
                except Exception as e:
                    log("翻译批 %d-%d 用 %s 失败(第%d次): %s" % (bs, bs + len(batch) - 1, model, attempt + 1, str(e)[:120]))
                    time.sleep(2 + attempt * 2)
            if got:
                break
        if not got:
            log("!! 批 %d-%d 翻译失败, 用原文占位" % (bs, bs + len(batch) - 1))
            got = {}
        for j, b in enumerate(batch):
            gi = bs + j
            result[gi] = (b[0], b[1], got.get(gi, b[2]))
        log("翻译进度 %d/%d" % (min(bs + BATCH_SIZE, total), total))
    return result


# ---------------- SRT ----------------
def _fmt_ts(sec):
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    mm = ms % 1000
    return "%02d:%02d:%02d,%03d" % (h, m, s, mm)


def write_srt(segs, path):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        for n, (st, en, txt) in enumerate(segs, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (n, _fmt_ts(st), _fmt_ts(en), txt))


# ---------------- 主流程 ----------------
def process_group(remote, path, base, parts, workdir, apply):
    log("处理组 (parts=%d)" % len(parts))
    if not apply:
        return
    t0 = time.time()
    gdir = os.path.join(workdir, "grp")
    os.makedirs(gdir, exist_ok=True)
    local_parts = download_group(remote, path, base, parts, gdir)
    video = os.path.join(gdir, "merged.mp4")
    concat_parts(local_parts, video)
    log("拼接完成 -> %.1f MB" % (os.path.getsize(video) / 1048576.0))
    segs = transcribe(video, WHISPER_MODEL)
    if not segs:
        log("!! 无字幕段, 跳过")
        return
    zh = translate_segments(segs)
    srt_local = os.path.join(gdir, "out.srt")
    write_srt(zh, srt_local)
    srt_name = "%s.srt" % base
    rc, out, err = run(["rclone", "copyto", srt_local, "%s:%s/%s" % (remote, path, srt_name),
                        "--retries", "3"])
    log("上传 srt -> rc=%d (%.1fs)" % (rc, time.time() - t0))
    if rc != 0:
        log("!! 上传失败: %s" % err[:200])
    # 清理
    for p in local_parts + [video, srt_local]:
        try:
            os.remove(p)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit-groups", type=int, default=0)
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--list", action="store_true", help="只列出分组统计")
    a = ap.parse_args()

    if not SRC_REMOTE:
        raise SystemExit("!! 缺少 SRC_REMOTE(SUB_SRC) —— 检查 Secret PATHS_JSON")

    deadline = time.time() + a.budget_min * 60
    log("源=%s:%s | 翻译模型=%s | whisper=%s | apply=%s" % (SRC_REMOTE, SRC_DIR, MODEL, WHISPER_MODEL, a.apply))

    files = rclone_lsf(SRC_REMOTE, SRC_DIR)
    log("目录内文件数=%d" % len(files))
    groups = group_files(files)
    log("归组后=%d 组" % len(groups))

    # 已存在 srt 的组跳过(幂等)
    existing = set(f[:-4] for f in files if f.lower().endswith(".srt"))

    targets = []
    for base in sorted(groups.keys()):
        if base in existing:
            continue
        if a.only and base != a.only:
            continue
        targets.append(base)
    log("待处理组=%d (已跳过 %d 个已有srt)" % (len(targets), len(existing)))

    if a.list:
        for b in targets:
            print("  group parts=%d" % len(groups[b]))
        return

    if a.limit_groups > 0:
        targets = targets[:a.limit_groups]

    workdir = tempfile.mkdtemp(prefix="subtr_")
    log("临时目录=%s" % workdir)
    ok = fail = 0
    for base in targets:
        if time.time() > deadline:
            log("!! 到达时间预算, 收工")
            break
        try:
            process_group(SRC_REMOTE, SRC_DIR, base, groups[base], workdir, a.apply)
            ok += 1
        except Exception as e:
            fail += 1
            log("!! 组处理失败: %s" % str(e)[:200])
    shutil.rmtree(workdir, ignore_errors=True)
    log("完成: ok=%d fail=%d" % (ok, fail))


if __name__ == "__main__":
    sys.exit(main())
