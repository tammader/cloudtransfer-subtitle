# -*- coding: utf-8 -*-
"""路径配置: 让脚本代码里不出现真实的网盘名 / 目录名

真值来源(优先级从高到低):
  1) 同名环境变量(如 GD_SRC)   —— CI 里若由 Secret 注入, 日志会显示 ***
  2) 环境变量 PATHS_JSON       —— CI 用一个 Secret 注入整本字典
  3) 同目录的 paths.local.json —— 本机专用, 不进仓库

用法:
    import pathcfg
    SRC = pathcfg.get("GD_SRC")

为什么这么做: 仓库已公开, 代码、命令行、日志里的盘名与目录名都要脱敏
(日志层见 logmask.py; 代码层就是这里 —— 值不再硬编码在源码里)。
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    d = {}
    raw = os.environ.get("PATHS_JSON", "")
    if raw:
        try:
            d.update(json.loads(raw))
        except Exception:
            pass
    lp = os.path.join(_HERE, "paths.local.json")
    if os.path.exists(lp):
        try:
            with open(lp, encoding="utf-8") as f:
                d.update(json.load(f))
        except Exception:
            pass
    _cache = d
    return d


def get(key, default=""):
    """环境变量 > PATHS_JSON / paths.local.json > default"""
    v = os.environ.get(key)
    if v:
        return v
    v = _load().get(key)
    return v if v else default


def require(key):
    v = get(key)
    if not v:
        raise SystemExit(
            "!! 缺少路径配置 %s —— CI 检查 Secret PATHS_JSON; 本机检查 paths.local.json" % key)
    return v


if __name__ == "__main__":
    d = _load()
    print("PATHS_JSON:", "已注入" if os.environ.get("PATHS_JSON") else "无")
    print("paths.local.json:", "存在" if os.path.exists(os.path.join(_HERE, "paths.local.json")) else "无")
    print("配置项 %d 个: %s" % (len(d), ", ".join(sorted(d.keys()))))
