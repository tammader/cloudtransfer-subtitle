# -*- coding: utf-8 -*-
"""字幕批量自动调度器(云端, GitHub Actions 定时跑)

两阶段(用环境变量 PHASE 切换):
  PHASE=translate(默认) —— 只出 srt: 下载/拼接/转写/翻译/上传 srt, **不烧录**。
  PHASE=burn            —— 只烧录: 对"已有 srt 但缺硬字幕视频"的组 拼片+烧制+上传。
                          前置闸门: 必须"所有组都已有 srt"(翻译阶段彻底完成)才启动,
                          这样两阶段天然串行, 不会互抢并发。

职责(每 10 分钟检查一次, 把并行批次补满):
  1. 统计源目录待办(subtitle_progress.collect)
     - translate 阶段看 pending_positions(没 srt 的组在「按大小升序全表」里的下标)
     - burn      阶段看 srt_no_burn(该阶段的目标列表本身就只含待办)
  2. 查 subtitle-translate 活跃 run; 少于 TARGET_PARALLEL 就补发差值个批次
     - start_index = **待办组的真实下标**(不是并发槽位 × 每组数!)
       旧写法 si=(活跃数+i)*每组数 只能覆盖前 并发数×每组数≈0~21 这段,
       那一段做完以后每轮都在撞已完成区间 -> run 全绿但 ok=0, 后面的组永远轮不到。
     - 已占窗口从活跃 run 的 run-name(`si=NN g=M`)读出来, 相邻批次不重复派发
     - 幂等: 已有 srt 的组在 worker 里自动跳过

Secrets: GH_TOKEN / RCLONE_CONF / PATHS_JSON / TG_TOKEN / TG_CHAT_ID
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pathcfg
import logmask
import subtitle_progress as sp

logmask.install()

API = "https://api.github.com"
REPO = "tammader/cloudtransfer-subtitle"
WF = "subtitle-translate.yml"
TARGET_PARALLEL = int(os.environ.get("TARGET_PARALLEL", "6"))   # 并发批次(仓库 Variables 可调)
GROUPS_PER_RUN = int(os.environ.get("GROUPS_PER_RUN", "3"))     # 每批次处理几组
PHASE = (os.environ.get("PHASE") or "translate").strip().lower()  # translate | burn


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def gh_req(method, path, tok, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "token " + tok, "User-Agent": "wb-dispatch",
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            raw = x.read().decode("utf-8", "replace")
            return x.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", "replace")[:300]}


def parse_int(name, pat, default=None):
    """从 run-name 里抠出 si=NN / g=M"""
    if not name:
        return default
    m = re.search(pat, name)
    if not m:
        return default
    try:
        return int(m.group(1))
    except Exception:
        return default


def claimed_windows(act, default_gpr):
    """活跃 run 已占的下标窗口 [(si, 组数)] —— 来自 run-name(si=NN g=M)"""
    win = []
    for r in act:
        si = parse_int(r.get("name"), r"si=(\d+)")
        if si is None:
            continue
        g = parse_int(r.get("name"), r"g=(\d+)", default_gpr) or default_gpr
        win.append((si, max(1, g)))
    return win


def covered(pos, windows):
    for s, g in windows:
        if s <= pos < s + g:
            return True
    return False


def pick_starts(pending_positions, windows, need, gpr):
    """按待办组的真实下标派发: 跳过已被在跑批次盖住的位置, 逐个往前推"""
    chosen = []
    win = list(windows)
    for p in pending_positions:
        if covered(p, win):
            continue
        chosen.append(p)
        win.append((p, gpr))
        if len(chosen) >= need:
            break
    return chosen


def main():
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        log("!! 缺 GH_TOKEN")
        return 1
    src = os.environ.get("SUB_SRC") or pathcfg.get("SUB_SRC", "")
    if not src:
        log("!! 缺 SUB_SRC(PATHS_JSON)")
        return 1
    log("阶段 = %s" % PHASE)

    # 1) 统计
    try:
        stats = sp.collect()
        log("总组=%d 已出字幕=%d(对得上组名=%d 对不上=%d) 硬字幕=%d 无字幕=%d 待补烧=%d" % (
            stats["total_groups"], stats["srt"], stats["srt_matched"], stats["srt_orphan"],
            stats["burn"], stats["pending"], stats["srt_no_burn"]))
        if stats["pending"]:
            log("待办下标区间: %s ~ %s (共 %d 组)" % (
                stats["first_pending"], stats["last_pending"], stats["pending"]))
    except Exception as e:
        log("!! 统计失败: %s" % str(e)[:200])
        return 1

    # 烧录阶段的前置闸门: 翻译必须全部完成, 否则两阶段会互相抢并发
    if PHASE == "burn" and stats["pending"] > 0:
        log("翻译阶段未完成(仍有 %d 组无 srt), 烧录阶段暂不启动" % stats["pending"])
        return 0

    todo = stats["srt_no_burn"] if PHASE == "burn" else stats["pending"]

    # 2) 活跃 run 数 + 已占窗口
    st, d = gh_req("GET", "/repos/%s/actions/workflows/%s/runs?per_page=30" % (REPO, WF), tok)
    runs = d.get("workflow_runs") or [] if st == 200 else []
    act = [r for r in runs if r["status"] in ("queued", "in_progress")]
    win = claimed_windows(act, GROUPS_PER_RUN)
    log("活跃批次: %d (已占窗口 %s)" % (len(act), win if win else "无"))

    # 3) 本阶段无待办且无活跃: 发一条完成通知(6h 内只发一次, 防刷屏)
    if todo == 0 and not act:
        recent = [r for r in runs if r["status"] == "completed"][:1]
        if recent:
            try:
                created = time.mktime(time.strptime(recent[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
                if time.time() - created < 6 * 3600:
                    if PHASE == "burn":
                        sp.tg_send("🎬 硬字幕烧录全部完成!\n总组 %d | 字幕 %d | 硬字幕 %d" % (
                            stats["total_groups"], stats["srt"], stats["burn"]))
                    else:
                        sp.tg_send("📝 字幕翻译全部完成(待烧录 %d 组)!\n总组 %d | 字幕 %d | 硬字幕 %d" % (
                            stats["srt_no_burn"], stats["total_groups"], stats["srt"], stats["burn"]))
                    log("已发完成通知")
            except Exception:
                pass
        return 0

    # 4) 补满并行
    need = TARGET_PARALLEL - len(act)
    if need <= 0:
        log("并发已满, 不补发")
        return 0

    if PHASE == "burn":
        # 烧录阶段的目标列表本身 = 待补烧组(全待办), 下标就是游标
        starts = [(len(act) + i) * GROUPS_PER_RUN for i in range(need)]
    else:
        starts = pick_starts(stats["pending_positions"], win, need, GROUPS_PER_RUN)
        if not starts:
            log("待办 %d 组, 但可用派发位置都被在跑的批次盖住了, 本轮不补发(等它们收尾)" % todo)
            return 0

    bt = int(time.time())
    n_ok = 0
    for i, si in enumerate(starts):
        inputs = {"apply": "true", "limit_groups": str(GROUPS_PER_RUN), "budget_min": "340",
                  "whisper_model": "large-v3-turbo", "start_index": str(si),
                  "batch_tag": "%s_%d_%d_si%d" % (PHASE, bt, i, si),
                  # 翻译阶段只出 srt; 烧录阶段只烧已有 srt
                  "burn_video": "true" if PHASE == "burn" else "false"}
        if PHASE == "burn":
            inputs["burn_only"] = "true"
        st, dd = gh_req("POST", "/repos/%s/actions/workflows/%s/dispatches" % (REPO, WF), tok,
                        {"ref": "main", "inputs": inputs})
        if st in (204, 200, 201):
            n_ok += 1
            log("补发批次 si=%d 组数=%d (窗口 %d-%d) OK" % (si, GROUPS_PER_RUN, si, si + GROUPS_PER_RUN - 1))
        else:
            log("补发失败 si=%d: %s %s" % (si, st, dd.get("_err", ""))[:180])
        time.sleep(2)
    log("本轮补发 %d/%d" % (n_ok, need))
    return 0


if __name__ == "__main__":
    sys.exit(main())
