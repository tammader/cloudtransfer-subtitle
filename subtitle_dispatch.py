# -*- coding: utf-8 -*-
"""字幕批量自动调度器(云端, GitHub Actions 定时跑)

职责: 每 30 分钟检查一次, 把 subtitle-translate 的并行批次补满到 4 路。
  1. 统计 gdrive6:out3 的待处理组数(subtitle_progress.collect)
     - 待处理=0 且近期无活动 -> 发一条"全部完成"到 TG(防重复: 6h 内没跑过才发)
  2. 查 subtitle-translate 活跃 run 数; 少于 4 就补发差值个批次
     - medium + 硬字幕 + 大小升序(脚本内部排序), start_index 0..3
     - 幂等: 已有 srt 的组自动跳过, 重复触发不产生重复字幕
环境: GH_TOKEN(调 API+dispatch) / RCLONE_CONF / PATHS_JSON / TG_TOKEN / TG_CHAT_ID
"""
import json
import os
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
TARGET_PARALLEL = 6          # 并发批次(旧 4; GH 免费额度允许 20, 6 是网关 503 与网盘限速的折中)
GROUPS_PER_RUN = 3           # 每批次处理几组(摊薄每轮 ~40s 固定开销)


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


def main():
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        log("!! 缺 GH_TOKEN")
        return 1
    src = os.environ.get("SUB_SRC") or pathcfg.get("SUB_SRC", "")
    if not src:
        log("!! 缺 SUB_SRC(PATHS_JSON)")
        return 1

    # 1) 待处理统计
    try:
        stats = sp.collect()
        log("总组=%d 已出字幕=%d 硬字幕=%d 待处理=%d" % (
            stats["total_groups"], stats["srt"], stats["burn"], stats["pending"]))
    except Exception as e:
        log("!! 统计失败: %s" % str(e)[:200])
        return 1

    # 2) 活跃 run 数
    st, d = gh_req("GET", "/repos/%s/actions/workflows/%s/runs?per_page=30" % (REPO, WF), tok)
    runs = d.get("workflow_runs") or [] if st == 200 else []
    act = [r for r in runs if r["status"] in ("queued", "in_progress")]
    log("活跃批次: %d" % len(act))

    # 3) 全部完成时: 只在"近期确实跑过"的情况下发一条完成通知(防刷屏)
    if stats["pending"] == 0 and not act:
        recent = [r for r in runs if r["status"] == "completed"][:1]
        if recent:
            try:
                created = time.mktime(time.strptime(recent[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
                if time.time() - created < 6 * 3600:
                    sp.tg_send("🎉 字幕批量全部完成!\n总组 %d | 字幕 %d | 硬字幕 %d" % (
                        stats["total_groups"], stats["srt"], stats["burn"]))
                    log("已发完成通知")
            except Exception:
                pass
        return 0

    # 4) 补满并行
    need = TARGET_PARALLEL - len(act)
    if need <= 0:
        log("并发已满, 不补发")
        return 0
    bt = int(time.time())
    n_ok = 0
    for i in range(need):
        inputs = {"apply": "true", "limit_groups": str(GROUPS_PER_RUN), "budget_min": "340",
                  "whisper_model": "large-v3-turbo", "start_index": str(i % TARGET_PARALLEL),
                  "batch_tag": "auto_%d_%d" % (bt, i), "burn_video": "true"}
        st, dd = gh_req("POST", "/repos/%s/actions/workflows/%s/dispatches" % (REPO, WF), tok,
                        {"ref": "main", "inputs": inputs})
        if st in (204, 200, 201):
            n_ok += 1
            log("补发批次 si=%d 组数=%d OK" % (i % TARGET_PARALLEL, GROUPS_PER_RUN))
        else:
            log("补发失败 si=%d: %s %s" % (i, st, dd.get("_err", ""))[:180])
        time.sleep(2)
    log("本轮补发 %d/%d" % (n_ok, need))
    return 0


if __name__ == "__main__":
    sys.exit(main())
