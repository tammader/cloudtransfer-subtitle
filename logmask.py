# -*- coding: utf-8 -*-
"""日志脱敏: 输出里的文件名 / 挂载路径 / 远端名 一律换成不可逆短哈希

背景: 仓库已公开(cloudtransfer-gd-out-pub), Actions 日志任何人可读,
      所以文件名和网盘挂载路径都不能明文出现在日志里。

用法(脚本开头, main 之前):
    import logmask
    logmask.install()

原理: 把 sys.stdout / sys.stderr 包一层, 每次 write 都过 scrub()。
      所有脚本的 rclone/ffmpeg 调用都是 subprocess + capture_output,
      输出最终都由 print 落地 -> 在 stdout 层拦一道即可全覆盖, 不会漏。

开关: 默认开启。本机调试想看真名 -> 设 LOG_MASK=0。

替换效果:
    已处理_ANKK-081.part004_D.mp4  ->  <file:3fa9c1d2>.mp4
    /some/dir/out2                 ->  <path:7b2e1a44>
    remote:dir/report.txt          ->  <remote:9c4d2e10>
同一串每次都得到同一标签 -> 排障时仍能对应"是不是同一个文件", 但无法反推原名。
"""
import hashlib
import os
import re
import sys

import pathcfg          # 关键词映射表从配置读(CI: Secret / 本机: paths.local.json) —— 源码里不留真名

_WORDS_CACHE = None


def _words():
    """敏感词 -> 中性代号; 映射表本身也存在配置里, 不写进源码"""
    global _WORDS_CACHE
    if _WORDS_CACHE is None:
        try:
            w = pathcfg._load().get("_WORDS") or {}
        except Exception:
            w = {}
        # 长词优先: 长的词必须先于它的前缀命中
        _WORDS_CACHE = dict(sorted(w.items(), key=lambda kv: -len(str(kv[0]))))
    return _WORDS_CACHE

_ON = os.environ.get("LOG_MASK", "1").strip().lower() not in ("0", "false", "no", "off")

_MEDIA_EXT = r"(?:mp4|mkv|avi|wmv|mov|flv|m4v|rmvb|ts|mpg|mpeg|webm|3gp|part\d*)"

# 1) 媒体文件名(含中文/全角括号/空格/编号) —— 最需要保护的目标
_RE_MEDIA = re.compile(
    r"[\w\u4e00-\u9fff（(【\[][^\s\"'|,;<>]*?\.%s\b" % _MEDIA_EXT, re.I)

# 1.5) 全角方括号块(常做文件名前缀, 如 【 5d86.shop】) —— 媒体名正则遇到中间的空格就断了, 得单独兜
_RE_BRACKET = re.compile(r"【[^】\n]{0,60}】")

# 2) rclone 远端:  name:path   例 remote1:out2 / remote2:dir/x.txt
_RE_REMOTE = re.compile(r"(?<!<)\b[A-Za-z0-9_\-]{2,}:[\w\u4e00-\u9fff][\w\u4e00-\u9fff/.\-]*")

# 2.5) OAuth 令牌形态(兜底): Google ya29./1///GOCSPX-, 微软 Ew.A/M.C
_RE_TOKEN = re.compile(r"\b(?:ya29\.[\w\-]{8,}|1//[\w\-]{10,}"
                       r"|GOCSPX-[\w\-]{6,}"
                       r"|Ew[A-Z][\w\-.]{18,}|0\.A[\w\-.]{18,}|M\.C[\w\-.]{18,})")

# 2.7) 个人身份信息: 手机号 / 邮箱 —— 云盘的账号就是手机号, 绝不能进日志
_RE_PII = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|[\w.+\-]+@[\w\-]+\.[\w.\-]+")

# 3) 绝对路径: /a/b/c 与 X:/a/b   (URL 里的 // 和 127.0.0.1:5244/api 由 lookbehind 排除)
_RE_PATH = re.compile(r"(?<![A-Za-z0-9:/\\])/[^\s\"'|,;)\]}\u3002\uff0c]+")


def _tag(kind, s):
    return "<%s:%s>" % (kind, hashlib.sha1(s.encode("utf-8")).hexdigest()[:8])


def scrub(text):
    """把一段文本里的文件名/路径/远端名换成短哈希标签

    单遍扫描: 一次收集所有匹配、按位置铺开、丢弃重叠的 —— 避免上一步生成的
    "<file:xxx>" 标签又被下一步(remote/path 正则)二次替换。
    """
    if not _ON or not text:
        return text
    try:
        for k, v in _words().items():          # 先换敏感词(词表见配置里的 _WORDS), 再收拾路径与文件名
            if k in text:
                text = text.replace(k, v)

        spans = []
        for pat, kind in ((_RE_MEDIA, "file"), (_RE_BRACKET, "brk"), (_RE_REMOTE, "remote"),
                         (_RE_PATH, "path"), (_RE_TOKEN, "tok"), (_RE_PII, "pii")):
            for m in pat.finditer(text):
                spans.append((m.start(), m.end(), m.group(0), kind))
        if not spans:
            return text
        prio = {"tok": 0, "pii": 0, "file": 0, "brk": 0, "remote": 1, "path": 2}   # 同起点时更具体的优先
        spans.sort(key=lambda x: (x[0], prio[x[3]], -(x[1] - x[0])))
        out, last = [], 0
        for s, e, raw, kind in spans:
            if s < last:                                 # 与已采用的区间重叠 -> 丢弃
                continue
            out.append(text[last:s])
            if kind == "file" and "." in raw:
                out.append(_tag("file", raw) + "." + raw.rsplit(".", 1)[1])
            else:
                out.append(_tag(kind, raw))
            last = e
        out.append(text[last:])
        return "".join(out)
    except Exception:
        return text


class _ScrubStream(object):
    """包一层 stdout/stderr, 写出去之前先脱敏"""

    def __init__(self, real):
        self._real = real

    def write(self, s):
        try:
            self._real.write(scrub(s))
        except Exception:
            self._real.write(s)
        return len(s)

    def writelines(self, lines):
        for l in lines:
            self.write(l)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def install():
    """装上脱敏层; 已装过或 LOG_MASK=0 时返回 False"""
    if not _ON:
        return False
    if getattr(sys.stdout, "_wb_logmask", False):
        return True
    sys.stdout = _ScrubStream(sys.stdout)
    sys.stderr = _ScrubStream(sys.stderr)
    try:
        sys.stdout._wb_logmask = True
    except Exception:
        pass
    return True


if __name__ == "__main__":
    install()
    print("样例输出:")
    print("  [380 MB] 已处理_ANKK-081.part004_D.mp4 | 对半切并上传 OK")
    print("  源 remote-a:out: 42 个文件 | 目标 remote-b:out2: 33 个")
    print("  [验证] /some/dir -> 5 个条目: ['some_X.mp4', 'a.wmv']")
    print("  工作区 /tmp/gdout2 | rclone.conf 不变 | ALIST_URL http://127.0.0.1:5244")
