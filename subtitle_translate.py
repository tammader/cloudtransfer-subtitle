# -*- coding: utf-8 -*-
"""日文视频 → 中文 SRT 字幕流水线(云端 runner 用)

流程:
  1. 列出源远端(如 gdrive6)目标目录(默认 out3)的 mp4 分片
  2. 按「番号」归组(去掉 已处理_ 前缀 / .partNNN_D 后缀)
  3. 每组的 .partNNN 分片按序号用 ffmpeg concat 拼成整片(单分片直接拷)
  4. faster-whisper (large-v3-turbo) 做日文语音转写 -> 日文 srt 段
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
  WHISPER_MODEL     默认 large-v3-turbo(别名 turbo 亦可; 也接受 medium/large-v3/small)
  BATCH_SIZE        每批翻译条数, 默认 40
  BUDGET_MIN        本轮时间预算(分钟)
"""
import argparse
import datetime
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

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
# whisper 模型: 默认 large-v3-turbo(编码器=large-v3, 解码器4层; 精度接近 large-v3, 速度 4-5x)
#   兼容口径: 接受 "large-v3-turbo" / "turbo" 等别名, 内部映射到 HF 上可直连下载的 CT2 仓库
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
# 别名 -> HF 仓库(仅当传入的是别名而非真实路径/仓库时使用)
_WHISPER_ALIAS = {
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3_turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def resolve_whisper_model(name):
    """把 whisper 模型别名解析成可直接加载的标识。
    'large-v3-turbo'/'turbo' 等别名 -> HF CT2 仓库; 其它(medium/large-v3/small/本地路径)原样返回。"""
    key = (name or "").strip().lower()
    return _WHISPER_ALIAS.get(key, name)
# --- 速度旋钮(都可用仓库 Variables 覆盖, 不改代码) ---
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))        # 每请求条数: 10 -> 50
XLATE_CONC = int(os.environ.get("XLATE_CONC", "4"))        # 并发翻译请求数(网关 503 敏感, 别超 6)
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "4"))
WHISPER_BATCH = int(os.environ.get("WHISPER_BATCH", "8"))  # 批量推理批大小(BatchedInferencePipeline)
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "1"))    # 1=贪心, 最快
BURN_PRESET = os.environ.get("BURN_PRESET", "ultrafast")   # veryfast -> ultrafast
BURN_CRF = os.environ.get("BURN_CRF", "23")
BURN_MODE = os.environ.get("BURN_MODE", "hard")            # hard=烧进画面 / soft=软字幕(近乎免转码)

# 分片命名: [已处理_]<base>.partNNN[_D].mp4  ；<base> 可能含中文/番号/空格
_RE_PART = re.compile(r"(?:^|_)(?P<base>.+?)\.part(?P<idx>\d+)(?:_[A-Za-z])?\.(?P<ext>mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)
_RE_PLAIN = re.compile(r"(?:^|_)(?P<base>.+?)\.(?P<ext>mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp)$", re.I)


def _norm_base(base):
    """归一化组名: 剥掉 已处理_ 前缀(可能多个), 让同番号的已处理/未处理分片归为一组。"""
    b = base
    while True:
        nb = re.sub(r"^已处理_", "", b)
        if nb == b:
            break
        b = nb
    return b


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
    """返回 [(文件名, 字节大小), ...]  —— 用 lsjson, 避免 lsf 分隔符歧义"""
    import json as _json
    rc, out, err = run(["rclone", "lsjson", "%s:%s" % (remote, path),
                        "--files-only", "--max-depth", "1"])
    if rc != 0:
        raise SystemExit("rclone lsjson 失败: %s" % err[:300])
    try:
        arr = _json.loads(out) if out.strip() else []
    except Exception:
        arr = []
    res = []
    for it in arr:
        if it.get("IsDir"):
            continue
        res.append((it.get("Name", ""), int(it.get("Size", 0) or 0)))
    return res


def group_files(files):
    """files: [(fn, size) 或 fn] -> {base: [(idx, fname), ...]}; 兼容纯名单"""
    groups = {}
    for item in files:
        fn = item[0] if isinstance(item, (tuple, list)) else item
        m = _RE_PART.search(fn)
        if m:
            base = _norm_base(m.group("base"))
            idx = int(m.group("idx"))
            groups.setdefault(base, []).append((idx, fn))
        else:
            mp = _RE_PLAIN.search(fn)
            if mp:
                groups.setdefault(_norm_base(mp.group("base")), []).append((0, fn))
    # 排序每个组内分片
    for b in groups:
        groups[b].sort(key=lambda x: x[0])
    return groups


def group_sizes(files, groups):
    """{base: 总字节大小}; files=[(fn,size)]"""
    sz = {}
    sizemap = {fn: s for fn, s in files if isinstance((fn, s), tuple)} if files and isinstance(files[0], (tuple, list)) else {}
    for base, parts in groups.items():
        tot = 0
        for _idx, fn in parts:
            tot += sizemap.get(fn, 0)
        sz[base] = tot
    return sz


def download_group(remote, path, base, parts, workdir):
    """下载一组分片到 workdir, 返回按序的本地路径列表。

    速度: 一次 rclone copy --files-from(并行 transfers), 不再逐片起进程。
    """
    lst = os.path.join(workdir, "dl.txt")
    with io.open(lst, "w", encoding="utf-8") as f:
        for _idx, fn in parts:
            f.write(fn + "\n")
    rc, out, err = run(["rclone", "copy", "%s:%s" % (remote, path), workdir,
                        "--files-from", lst, "--transfers", "8", "--checkers", "8",
                        "--drive-chunk-size", "128M", "--multi-thread-streams", "4",
                        "--retries", "3", "--stats-one-line", "--stats", "30s"])
    if rc != 0:
        raise RuntimeError("下载失败: %s" % err[-200:])
    locals_ = []
    for idx, fn in parts:
        local = os.path.join(workdir, os.path.basename(fn))
        if not os.path.exists(local):
            raise RuntimeError("下载后缺文件 idx=%d" % idx)
        locals_.append(local)
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
def transcribe(video_path, whisper_model, max_seconds=0):
    """faster-whisper 日文转写, 返回 [(start, end, text), ...]

    速度: CPU int8 + 批量推理(BatchedInferencePipeline) + 贪心解码(beam=1)。
    VAD 空结果时先"放宽 VAD"重跑, 最后才全量无 VAD —— 不再一上来就跑两遍全片。
    """
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    resolved = resolve_whisper_model(whisper_model)
    log("加载 whisper 模型 %s (解析=%s, CPU int8, threads=%d)..." % (
        whisper_model, resolved, WHISPER_THREADS))
    model = WhisperModel(resolved, device="cpu", compute_type="int8",
                         cpu_threads=WHISPER_THREADS, num_workers=1)
    pipe = BatchedInferencePipeline(model=model)

    # 紧 VAD: 这类片对白稀疏, 但阈值过高会把对白整段滤掉(旧版实测 long-audio 出 0 条)
    VAD_TIGHT = {"min_silence_duration_ms": 2000, "threshold": 0.15,
                 "min_speech_duration_ms": 50, "speech_pad_ms": 400}
    # 放宽 VAD: 出 0 条时的第一手段(比无 VAD 全量重跑快好几倍)
    VAD_LOOSE = {"min_silence_duration_ms": 400, "threshold": 0.04,
                 "min_speech_duration_ms": 30, "speech_pad_ms": 500}

    def _collect(segments, info):
        out, lastlog = [], time.time()
        for seg in segments:
            txt = (seg.text or "").strip()
            if txt:
                out.append((seg.start, seg.end, txt))
            # 每 30s 打一次进度(能看到是否在推进)
            if time.time() - lastlog > 30:
                log("  转写进度: 已到 %.0fs / 共 %.0fs, 已收 %d 条" % (seg.end, info.duration, len(out)))
                lastlog = time.time()
        return out

    def _run(use_vad, vad_params=None):
        log("开始转写(日文, vad=%s, beam=%d, batch=%d, max_s=%s)..." % (
            use_vad, WHISPER_BEAM, WHISPER_BATCH, max_seconds or "full"))
        kw = dict(language="ja", beam_size=WHISPER_BEAM, condition_on_previous_text=False,
                  vad_filter=use_vad)
        if use_vad and vad_params:
            kw["vad_parameters"] = vad_params
        if max_seconds or not use_vad:
            # 探针(需 clip_timestamps 字符串)或全量无 VAD:
            # 批量管线在 vad_filter=False 且无 clip_timestamps 时直接 RuntimeError, 所以走单条路径
            if max_seconds:
                kw["clip_timestamps"] = "0,%d" % max_seconds
            segments, info = model.transcribe(video_path, **kw)
        else:
            segments, info = pipe.transcribe(video_path, batch_size=WHISPER_BATCH, **kw)
        log("检测语种=%s p=%.2f, 时长=%.1fs" % (info.language, info.language_probability, info.duration))
        return _collect(segments, info)

    out = _run(True, VAD_TIGHT)
    if not out and not max_seconds:
        log("!! 紧 VAD 后无字幕, 先用放宽 VAD 重跑(比全量重跑快得多)")
        out = _run(True, VAD_LOOSE)
    if not out and not max_seconds:
        log("!! 放宽 VAD 仍无字幕, 回退到无 VAD 全量转写")
        out = _run(False)
    log("转写完成: %d 条字幕" % len(out))
    return out


# ---------------- 翻译 ----------------
def _post_chat(model, messages, timeout=120):
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0.2, "max_tokens": 8000, "stream": False}).encode()
    req = urllib.request.Request(API_BASE + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + API_KEY,
                                          "Content-Type": "application/json",
                                          "Connection": "keep-alive"})
    r = urllib.request.urlopen(req, timeout=timeout)
    d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


# --- 翻译可定制规则 ---
# 三条来源(优先级从高到低叠加): 环境变量 EXTRA_RULES(CI 输入/vars) + 术语表文件 TERMS_FILE(JSON)
# 用法: 手动 dispatch 填 extra_rules, 或把仓库变量 TRANSLATE_RULES 设成固定文案; 术语表放源目录同层
EXTRA_RULES = (os.environ.get("EXTRA_RULES") or "").strip()
TERMS_FILE = (os.environ.get("TERMS_FILE") or "translate_terms.json").strip()

_BASE_SYS = ("你是专业的日译中字幕翻译。你会收到一个 JSON 数组, 每个元素是 {i, t} 字幕条目。"
             "请把每条 t 从日文翻译成简洁自然的简体中文, 保持口语化、符合字幕习惯。"
             "严格只返回一个 JSON 数组, 每个元素 {\"i\": 原i, \"z\": 中文译文}, 顺序与输入一致, 不要任何解释或多余文字。")


def _terms_to_text(data):
    """{日文原文: 中文译法} -> "术语对照"附加规则文本; 非 dict / 空则返回空串。"""
    if not isinstance(data, dict):
        return ""
    pairs = ["%s => %s" % (str(k), str(v)) for k, v in data.items() if str(k).strip()]
    if not pairs:
        return ""
    return "以下是必须遵守的术语/专有名词对照(遇到即按此译法, 勿自行意译):\n" + "\n".join(pairs)


def _load_terms_text():
    """术语对照来源优先级: 环境变量 TRANSLATE_TERMS(JSON 文本, CI 走 Secret) > 本地文件 TERMS_FILE。
    找不到/解析失败返回空(不阻断流程)。"""
    # 1) CI Secret 注入的 JSON 文本(不落进公开仓库)
    env_raw = (os.environ.get("TRANSLATE_TERMS") or "").strip()
    if env_raw:
        try:
            txt = _terms_to_text(json.loads(env_raw))
            if txt:
                return txt
        except Exception as e:
            log("!! 术语表(env TRANSLATE_TERMS)解析失败: %s" % str(e)[:120])
    # 2) 本地/仓库文件兜底
    if not TERMS_FILE:
        return ""
    path = TERMS_FILE
    if not os.path.isabs(path):
        # 优先当前工作目录, 其次脚本目录
        cand = os.path.join(os.getcwd(), path)
        if not os.path.exists(cand):
            cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        path = cand
    if not os.path.exists(path):
        return ""
    try:
        # utf-8-sig 兼容 Windows 记事本/PowerShell 写入的 BOM
        return _terms_to_text(json.loads(io.open(path, encoding="utf-8-sig").read()))
    except Exception as e:
        log("!! 术语表解析失败(%s): %s" % (TERMS_FILE, str(e)[:120]))
        return ""


def _build_sys():
    """组合默认规则 + 术语表 + 用户附加规则, 生成最终系统提示词。"""
    parts = [_BASE_SYS]
    terms = _load_terms_text()
    if terms:
        parts.append(terms)
    if EXTRA_RULES:
        parts.append("额外翻译规则(必须严格遵守):\n" + EXTRA_RULES)
    if len(parts) > 1:
        parts.append("上面所有规则都必须遵守; 输出格式仍然严格遵循: 只返回 JSON 数组 [{\"i\":..,\"z\":..}]。")
    return "\n\n".join(parts)


_SYS = _build_sys()


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


_FAST_MODELS = [MODEL, MODEL_FB]
_RESCUE_MODELS = [MODEL, MODEL_FB, "qwen3.7-max", "qwen3.8-max", "deepseek-v4-pro"]
# 网关并发闸门: 高并发会大量 503/504, 所以同时最多 XLATE_CONC 个请求在飞
_XLATE_SEM = threading.Semaphore(max(1, XLATE_CONC))


def _translate_batch(payload, models=None, attempts=2):
    """翻译一批 [{i,t}], 返回 {i: zh}; 失败返回 None。

    速度: 阶梯收窄(2 模型 × 2 次), 退避带抖动; 旧的 5 模型 × 4 次 × sleep 会拖到分钟级。
    """
    user = json.dumps(payload, ensure_ascii=False)
    for model in (models or _FAST_MODELS):
        for attempt in range(attempts):
            try:
                with _XLATE_SEM:
                    txt = _post_chat(model, [
                        {"role": "system", "content": _SYS},
                        {"role": "user", "content": user},
                    ], timeout=180)
                arr = _extract_json_array(txt)
                if isinstance(arr, list) and len(arr) >= 1:
                    got = {int(x["i"]): str(x["z"]).strip() for x in arr if "i" in x and "z" in x}
                    if got:
                        return got
                log("翻译批(len=%d) 用 %s 返回不可解析(第%d次)" % (len(payload), model, attempt + 1))
            except Exception as e:
                log("翻译批(len=%d) 用 %s 失败(第%d次): %s" % (len(payload), model, attempt + 1, str(e)[:100]))
            time.sleep(min(15.0, (2 ** attempt) + random.uniform(0, 1.5)))


def _translate_items(items, models=None, attempts=2):
    """items: [(全局i, text)]; 成功返回 {i: zh}。失败则二分拆分重试, 最差保证单条重试。"""
    got = _translate_batch([{"i": gi, "t": tx} for gi, tx in items], models, attempts)
    if got:
        return got
    if len(items) == 1:
        # 单条仍失败: 换全模型阶梯狠狠重试几轮
        gi, tx = items[0]
        for _ in range(2):
            g2 = _translate_batch([{"i": gi, "t": tx}], _RESCUE_MODELS, attempts=2)
            if g2:
                return g2
        log("!! 单条 i=%d 多次重试仍失败" % gi)
        return {}
    mid = len(items) // 2
    out = {}
    out.update(_translate_items(items[:mid], models, attempts))
    out.update(_translate_items(items[mid:], models, attempts))
    return out


def _translate_all(items, models=None, attempts=2):
    """把 [(全局i,text)] 并行分批翻译, 返回 {i: zh}。"""
    if not items:
        return {}
    chunks = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    out = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(XLATE_CONC, len(chunks)))) as ex:
        futs = [ex.submit(_translate_items, c, models, attempts) for c in chunks]
        for fu in as_completed(futs):
            try:
                out.update(fu.result() or {})
            except Exception as e:
                log("!! 翻译分批任务异常: %s" % str(e)[:120])
            done += 1
            log("翻译进度 批 %d/%d, 已得 %d 条" % (done, len(chunks), len(out)))
    return out


def _has_japanese(text):
    """检测是否含日文假名(平假名/片假名)"""
    return bool(re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text or ""))


_LEAK_WORDS = ("the user", "i need", "i should", "let me", "translate", "translation",
               "用户想", "用户希望", "需要翻译", "翻译成", "we need", "sure,", "当然，以下是")
def _is_bad_translation(text):
    """坏译文: 含日文假名 / 泄漏思考过程 / 为空"""
    t = (text or "").strip()
    if not t:
        return True
    if _has_japanese(t):
        return True
    low = t.lower()
    for w in _LEAK_WORDS:
        if w in low:
            return True
    return False


def _retranslate_bad(result, rounds=3):
    """结果里仍是坏译文(日文/泄漏思考/空)的条目, 重新翻译, 直到合格或轮次用尽"""
    for rd in range(rounds):
        bad = [(i, r[2]) for i, r in enumerate(result) if _is_bad_translation(r[2])]
        if not bad:
            return result
        log("第%d轮补译: %d 条坏译文(日文/泄漏/空)" % (rd + 1, len(bad)))
        got = _translate_all(bad, _RESCUE_MODELS, attempts=2)
        for i, _ in bad:
            zh = got.get(i)
            if zh and not _is_bad_translation(zh):
                result[i] = (result[i][0], result[i][1], zh)
    still = sum(1 for r in result if _is_bad_translation(r[2]))
    if still:
        log("!! 补译后仍有 %d 条坏译文" % still)
    return result


def translate_segments(segs):
    """segs: [(start,end,text)] -> [(start,end,zh)]；并发分批 + 失败二分重试 + 日文残留补译"""
    total = len(segs)
    items = [(i, s[2]) for i, s in enumerate(segs)]
    got = _translate_all(items)
    miss = 0
    result = [None] * total
    for i, s in enumerate(segs):
        zh = got.get(i)
        if not zh:
            zh = s[2]   # 极端兜底: 只有彻底失败才会到这里, 之后由补译轮再救
            miss += 1
        result[i] = (s[0], s[1], zh)
    log("翻译完成 %d/%d 条(缺 %d)" % (total - miss, total, miss))
    # 收尾: 补译所有坏译文(日文残留/泄漏思考/空)
    result = _retranslate_bad(result)
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


def _parse_ts(s):
    """"00:01:02,500" -> 秒(float)"""
    s = s.strip()
    hh, mm, rest = s.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt(path):
    """解析 srt -> [(start, end, text)]"""
    segs = []
    txt = io.open(path, encoding="utf-8", errors="replace").read()
    # 按空行分块
    blocks = re.split(r"\r?\n\r?\n", txt.strip())
    for blk in blocks:
        lines = [l for l in blk.splitlines() if l.strip() != ""]
        if len(lines) < 2:
            continue
        # 找含 --> 的行
        ti = None
        for idx, l in enumerate(lines):
            if "-->" in l:
                ti = idx
                break
        if ti is None:
            continue
        try:
            a, b = lines[ti].split("-->")
            st = _parse_ts(a)
            en = _parse_ts(b)
        except Exception:
            continue
        text = " ".join(lines[ti + 1:]).strip()
        if text:
            segs.append((st, en, text))
    return segs


# ---------------- 硬字幕烧制 ----------------
BURN_VIDEO = os.environ.get("BURN_VIDEO", "1").strip() not in ("0", "false", "no", "off")
BURN_DIR = os.environ.get("BURN_DIR", "字幕版")   # 输出子目录(相对源目录)
# 烧制时字幕滤镜里要转义的字符(路径中的 : ' \ 等)
def _ff_escape_path(p):
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def burn_subtitles(video_path, srt_local, base, remote, path, gdir):
    """把 srt 硬烧进整片, 输出 <番号>_字幕版.mp4, 上传到 <源目录>/字幕版/

    BURN_MODE=hard(默认): libx264 -preset ultrafast -crf 23(比 veryfast 快约 2-3 倍)
    BURN_MODE=soft      : 视频流直接 copy, 字幕作为软字幕塞进 mp4(近乎免转码, 秒级)
    """
    out_mp4 = os.path.join(gdir, "burned.mp4")
    vf = "subtitles='%s':force_style='FontName=Noto Sans CJK SC,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=1,Shadow=0,MarginV=18'" % _ff_escape_path(srt_local)
    if BURN_MODE == "soft":
        log("装软字幕(视频流 copy, 不重编码)...")
        rc, out, err = run(["ffmpeg", "-y", "-i", video_path, "-i", srt_local,
                            "-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
                            "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                            "-metadata:s:s:0", "language=chi", out_mp4])
    else:
        log("开始烧制硬字幕(CPU 编码 preset=%s crf=%s)..." % (BURN_PRESET, BURN_CRF))
        # 先试 -c:v copy 不行(烧字幕必须重编码), 直接 libx264 + 快速档
        rc, out, err = run(["ffmpeg", "-y", "-i", video_path, "-vf", vf,
                            "-c:v", "libx264", "-preset", BURN_PRESET, "-crf", BURN_CRF,
                            "-x264-params", "rc-lookahead=0:sync-lookahead=0:bframes=0:ref=1:me=dia:subme=0:trellis=0:mbtree=0",
                            "-threads", "0", "-c:a", "copy", out_mp4])
    if rc != 0 or not os.path.exists(out_mp4):
        log("!! 字幕封装/烧制失败: %s" % (err[-250:] if err else ""))
        return False
    log("输出完成 -> %.1f MB" % (os.path.getsize(out_mp4) / 1048576.0))
    burn_name = "%s_字幕版.mp4" % base
    dst = "%s:%s/%s/%s" % (remote, path, BURN_DIR, burn_name)
    rc2, o2, e2 = run(["rclone", "copyto", out_mp4, dst, "--retries", "3",
                       "--drive-chunk-size", "128M", "--multi-thread-streams", "4"])
    log("上传字幕版视频 -> rc=%d" % rc2)
    if rc2 != 0:
        log("!! 上传字幕版视频失败: %s" % (e2[:200] if e2 else ""))
    try:
        os.remove(out_mp4)
    except Exception:
        pass
    return rc2 == 0


def fix_srt_group(remote, path, base, parts, workdir, apply, re_burn=True):
    """修复模式: 下载已有 srt, 重新翻译其中文(替换日文残留), 写回修正 srt; 可选重烧硬字幕。
    不重跑 whisper 转写。"""
    log("修复组 srt (parts=%d, re_burn=%s)" % (len(parts), re_burn))
    if not apply:
        return
    t0 = time.time()
    gdir = os.path.join(workdir, "grp")
    os.makedirs(gdir, exist_ok=True)
    srt_local = os.path.join(gdir, "in.srt")
    rc0, o0, e0 = run(["rclone", "copyto", "%s:%s/%s.srt" % (remote, path, base), srt_local, "--retries", "3"])
    if rc0 != 0 or not os.path.exists(srt_local):
        log("!! 下载 srt 失败, 跳过")
        return
    segs = parse_srt(srt_local)
    log("解析 srt: %d 条" % len(segs))
    if not segs:
        return
    ja_before = sum(1 for _, _, t in segs if _is_bad_translation(t))
    log("坏译文条数(修前, 日文/泄漏/空): %d/%d" % (ja_before, len(segs)))
    zh = translate_segments(segs)
    ja_after = sum(1 for _, _, t in zh if _is_bad_translation(t))
    log("坏译文条数(修后): %d/%d" % (ja_after, len(zh)))
    out_srt = os.path.join(gdir, "fixed.srt")
    write_srt(zh, out_srt)
    rc, out, err = run(["rclone", "copyto", out_srt, "%s:%s/%s.srt" % (remote, path, base), "--retries", "3"])
    log("回写修正 srt -> rc=%d (%.1fs)" % (rc, time.time() - t0))
    if re_burn and BURN_VIDEO:
        local_parts = download_group(remote, path, base, parts, gdir)
        video = os.path.join(gdir, "merged.mp4")
        concat_parts(local_parts, video)
        try:
            burn_subtitles(video, out_srt, base, remote, path, gdir)
        except Exception as e:
            log("!! 重烧异常: %s" % str(e)[:200])
        for p in local_parts + [video]:
            try: os.remove(p)
            except Exception: pass
    for p in [srt_local, out_srt]:
        try: os.remove(p)
        except Exception: pass


# ---------------- 主流程 ----------------
def process_group(remote, path, base, parts, workdir, apply, probe_seconds=0, burn_only=False):
    log("处理组 (parts=%d, probe=%s, burn_only=%s)" % (len(parts), probe_seconds or "off", burn_only))
    if not apply:
        return
    t0 = time.time()
    gdir = os.path.join(workdir, "grp")
    os.makedirs(gdir, exist_ok=True)
    local_parts = download_group(remote, path, base, parts, gdir)
    video = os.path.join(gdir, "merged.mp4")
    concat_parts(local_parts, video)
    log("拼接完成 -> %.1f MB" % (os.path.getsize(video) / 1048576.0))

    if burn_only:
        # 补烧模式: 已有 srt, 只需下载 srt + 拼片 + 烧制, 跳过转写/翻译
        srt_local = os.path.join(gdir, "existing.srt")
        rc0, o0, e0 = run(["rclone", "copyto", "%s:%s/%s.srt" % (remote, path, base), srt_local, "--retries", "3"])
        if rc0 != 0 or not os.path.exists(srt_local) or os.path.getsize(srt_local) < 10:
            log("!! 补烧: 下载已有 srt 失败, 跳过")
            for p in local_parts + [video]:
                try: os.remove(p)
                except Exception: pass
            return
        try:
            burn_subtitles(video, srt_local, base, remote, path, gdir)
        except Exception as e:
            log("!! 补烧硬字幕异常: %s" % str(e)[:200])
        for p in local_parts + [video, srt_local]:
            try: os.remove(p)
            except Exception: pass
        return

    segs = transcribe(video, WHISPER_MODEL, max_seconds=probe_seconds)
    if probe_seconds:
        log("=== PROBE 结果: 前 %ds 收到 %d 条字幕 ===" % (probe_seconds, len(segs)))
        for st, en, tx in segs[:10]:
            log("  [%.1f-%.1f] %s" % (st, en, tx))
        for p in local_parts + [video]:
            try: os.remove(p)
            except Exception: pass
        return
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
    # 硬字幕(可选): 把 srt 烧进整片, 上传到 字幕版/ 子目录
    if BURN_VIDEO:
        try:
            burn_subtitles(video, srt_local, base, remote, path, gdir)
        except Exception as e:
            log("!! 硬字幕步骤异常: %s" % str(e)[:200])
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
    ap.add_argument("--probe-only", type=int, default=0, help="只转写前 N 秒做验证(不上传)")
    ap.add_argument("--start-index", type=int, default=0, help="从待处理列表第 K 个开始(并行分批用)")
    ap.add_argument("--force", action="store_true", help="配合 --only: 即使已有 srt 也强制重跑(测试硬字幕用)")
    ap.add_argument("--burn-only", action="store_true", help="补烧模式: 对有 srt 但缺字幕版视频的组, 只拼片+烧制(不重跑转写)")
    ap.add_argument("--fix-srt", action="store_true", help="修复模式: 重译已有 srt 的日文残留, 写回并可选重烧硬字幕")
    ap.add_argument("--no-reburn", action="store_true", help="配合 --fix-srt: 只修 srt 不重烧视频")
    a = ap.parse_args()

    if not SRC_REMOTE:
        raise SystemExit("!! 缺少 SRC_REMOTE(SUB_SRC) —— 检查 Secret PATHS_JSON")

    deadline = time.time() + a.budget_min * 60
    log("源=%s:%s | 翻译模型=%s | whisper=%s | apply=%s" % (SRC_REMOTE, SRC_DIR, MODEL, WHISPER_MODEL, a.apply))
    _terms = _load_terms_text()
    log("翻译规则: 附加规则=%s, 术语表=%s" % (
        "有(%d字)" % len(EXTRA_RULES) if EXTRA_RULES else "无",
        "有(%d条)" % (_terms.count("=>") if _terms else 0) if _terms else "无"))

    files = rclone_lsf(SRC_REMOTE, SRC_DIR)
    log("目录内文件数=%d" % len(files))
    groups = group_files(files)
    gsizes = group_sizes(files, groups)
    log("归组后=%d 组" % len(groups))

    # 已存在 srt 的组跳过(幂等)
    existing = set(fn[:-4] for fn, _sz in files if fn.lower().endswith(".srt"))

    # 目标顺序: 按组总大小升序(小的先做, 巨型文件排最后, 避免阻塞)
    order_by_size = sorted(groups.keys(), key=lambda b: gsizes.get(b, 0))

    if a.burn_only:
        # 补烧模式: 目标 = 有 srt 但 字幕版/ 里还没有对应 <番号>_字幕版.mp4 的组
        burn_names = set()
        try:
            sub = rclone_lsf(SRC_REMOTE, "%s/%s" % (SRC_DIR, BURN_DIR))
            for fn, _sz in sub:
                if fn.lower().endswith(".mp4") and fn.endswith("_字幕版.mp4"):
                    burn_names.add(fn[:-len("_字幕版.mp4")])
        except Exception as e:
            log("!! 列字幕版目录失败(可能不存在, 视为全缺): %s" % str(e)[:120])
        targets = []
        for base in order_by_size:
            if base not in existing:
                continue  # 没 srt 的组不补烧(等正常流程出 srt)
            if base in burn_names:
                continue  # 已有硬字幕视频
            if a.only and base != a.only:
                continue
            targets.append(base)
        log("补烧模式: 有srt组=%d, 已有字幕版=%d, 待补烧=%d" % (len(existing), len(burn_names), len(targets)))
    elif a.fix_srt:
        # 修复模式: 扫描有 srt 的组(可按 --only 限定), 重译日文残留
        targets = []
        for base in order_by_size:
            if base not in existing:
                continue
            if a.only and base != a.only:
                continue
            targets.append(base)
        log("修复模式: 有srt组=%d, 目标=%d" % (len(existing), len(targets)))
    else:
        targets = []
        for base in order_by_size:
            if base in existing and not a.force:
                continue
            if a.only and base != a.only:
                continue
            targets.append(base)
        log("待处理组=%d (已跳过 %d 个已有srt; 按大小升序)" % (len(targets), len(existing)))

    if a.list:
        for b in targets:
            print("  group parts=%d" % len(groups[b]))
        return

    # 并行分批: 从第 start_index 个待处理组开始
    if a.start_index > 0:
        targets = targets[a.start_index:]
        log("start-index=%d, 剩余可处理=%d" % (a.start_index, len(targets)))
    if a.limit_groups > 0:
        targets = targets[:a.limit_groups]
    log("本批目标组: %d" % len(targets))

    workdir = tempfile.mkdtemp(prefix="subtr_")
    log("临时目录=%s" % workdir)
    ok = fail = 0
    for base in targets:
        if time.time() > deadline:
            log("!! 到达时间预算, 收工")
            break
        try:
            if a.fix_srt:
                fix_srt_group(SRC_REMOTE, SRC_DIR, base, groups[base], workdir, a.apply,
                              re_burn=(not a.no_reburn))
            else:
                process_group(SRC_REMOTE, SRC_DIR, base, groups[base], workdir, a.apply,
                              probe_seconds=a.probe_only, burn_only=a.burn_only)
            ok += 1
        except Exception as e:
            fail += 1
            log("!! 组处理失败: %s" % str(e)[:200])
    shutil.rmtree(workdir, ignore_errors=True)
    log("完成: ok=%d fail=%d" % (ok, fail))


if __name__ == "__main__":
    sys.exit(main())
