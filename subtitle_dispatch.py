# -*- coding: utf-8 -*-
"""字幕批量自动调度器(云端, GitHub Actions 定时跑)

单阶段: 只出 srt(下载/拼接/转写/翻译/上传 srt), **不烧录**。
  (原 PHASE=burn 烧录阶段已随流程取消; 传 PHASE=burn 会被拒绝并退出)

职责(每 10 分钟检查一次, 把并行批次补满):
  1. 统计源目录待办数(subtitle_progress.collect) —— pending = 没 srt 的组
  2. 查 subtitle-translate 活跃 run 数; 少于 TARGET_PARALLEL 就补发差值个批次
     - start_index 按 GROUPS_PER_RUN 递进(0,3,6...), 相邻批次不重叠
     - 幂等: 已有 srt 的组自动跳过
  Secrets: GH_TOKEN / RCLONE_CONF / PATHS_JSON
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pathcfg
import logmask
import subtitle_progress as sp

logmask.install()

API = "https://api.github.com"
REPO = "tammader/cloudtransfer-subtitle"
WF = "subtitle-translate.yml"
TARGET_PARALLEL = int(os.environ.get("TARGET_PARALLEL", "20"))   # 并发批次(仓库 Variables 可调)
GROUPS_PER_RUN = int(os.environ.get("GROUPS_PER_RUN", "3"))     # 每批次处理几组
PHASE = (os.environ.get("PHASE") or "translate").strip().lower()  # translate | burn


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def tg_alert(text):
    """派发异常时给 Telegram 发告警。缺 token/网络异常都静默失败, 绝不影响主流程。

    触发场景: GH_TOKEN 失效或权限不足 → dispatch API 回 404, 补发 0/N, 流程静默停摆。
    (2026-11-16 实战教训: 静默 404 让字幕流程停了一小时无人知, 故加此告警)
    """
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                     data=body, method="POST")
        with urllib.request.urlopen(req, timeout=20) as x:
            x.read()
    except Exception as e:
        log("!! TG 告警发送失败: %s" % str(e)[:120])


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
    if PHASE != "translate":
        log("!! 本流程已取消烧录阶段(PHASE=%s 不再支持), 只跑 translate" % PHASE)
        return 0
    log("阶段 = translate (只出 srt, 不烧录)")

    # 1) 统计
    try:
        stats = sp.collect()
        log("总组=%d 已出字幕=%d 无字幕=%d" % (
            stats["total_groups"], stats["srt"], stats["pending"]))
    except Exception as e:
        log("!! 统计失败: %s" % str(e)[:200])
        return 1

    todo = stats["pending"]

    # 2) 活跃 run 数
    st, d = gh_req("GET", "/repos/%s/actions/workflows/%s/runs?per_page=30" % (REPO, WF), tok)
    if st != 200:
        # token 失效/权限不足时连查询都会失败 —— 这是比 404 更早的信号
        log("!! 查询活跃 run 失败: %s %s" % (st, d.get("_err", ""))[:180])
        tg_alert("⚠️ 字幕调度器异常\n查询 subtitle-translate runs 失败 (HTTP %s)\n"
                 "多半是 GH_TOKEN 失效/权限不足，自动派发已停，请检查 Secret。"
                 % st)
        return 1
    runs = d.get("workflow_runs") or [] if st == 200 else []
    act = [r for r in runs if r["status"] in ("queued", "in_progress")]
    log("活跃批次: %d" % len(act))

    # 3) 无待办且无活跃: 只打日志
    if todo == 0 and not act:
        log("字幕全部完成: 总组 %d | 已出字幕 %d" % (stats["total_groups"], stats["srt"]))
        return 0

    # 4) 补满并行
    need = TARGET_PARALLEL - len(act)
    if need <= 0:
        log("并发已满, 不补发")
        return 0
    bt = int(time.time())
    n_ok = 0
    first_err = ""
    first_code = None
    base = len(act)                      # 排在已在跑的批次之后, 避免区间重叠
    for i in range(need):
        si = (base + i) * GROUPS_PER_RUN    # 0,3,6,9... 不重叠
        inputs = {"apply": "true", "limit_groups": str(GROUPS_PER_RUN), "budget_min": "340",
                  "whisper_model": os.environ.get("WHISPER_MODEL", "large-v3"), "start_index": str(si),
                  "batch_tag": "translate_%d_%d" % (bt, i)}
        st, dd = gh_req("POST", "/repos/%s/actions/workflows/%s/dispatches" % (REPO, WF), tok,
                        {"ref": "main", "inputs": inputs})
        if st in (204, 200, 201):
            n_ok += 1
            log("补发批次 si=%d 组数=%d OK" % (si, GROUPS_PER_RUN))
        else:
            if first_code is None:
                first_code = st
                first_err = dd.get("_err", "")
            log("补发失败 si=%d: %s %s" % (si, st, dd.get("_err", ""))[:180])
        time.sleep(2)
    log("本轮补发 %d/%d" % (n_ok, need))

    # 关键告警: 明明有活要派(need>0) 却一个批次都没派出去 —— 就是今天这种静默停摆
    if need > 0 and n_ok == 0:
        tg_alert("🛑 字幕流程自动派发失败(0/%d)\n"
                 "待处理 %d 组, 无活跃批次, 但 dispatch 全部失败 (HTTP %s)。\n"
                 "通常=GH_TOKEN 失效/权限不足或 workflow 定义变更。\n"
                 "错误: %s\n请尽快检查，否则字幕将停摆。" % (
                     need, todo, first_code, (first_err or "")[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
