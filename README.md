# subtitle-translate

日文语音视频 → 中文 SRT 字幕,纯云端 GitHub Actions 流水线。

## 做什么

1. 列出源网盘指定目录里的视频分片(`.partNNN` 分卷)
2. 按「番号」把分片归组,用 ffmpeg 拼成整片
3. `faster-whisper`(large-v3)做日文语音转写
4. 调 OpenAI 兼容网关,批量把日文字幕翻成简体中文(保留时间轴)
5. 生成同名 `.srt`,上传回源目录

## 怎么跑

Actions → `subtitle-translate` → Run workflow

| 输入 | 说明 |
|---|---|
| `apply` | `true` 真跑;`false` 只列分组统计 |
| `only` | 只处理某一组(按番号,留空=全部) |
| `limit_groups` | 最多处理前 N 组(默认 1) |
| `budget_min` | 本轮时间预算(分钟) |
| `whisper_model` | `large-v3` / `medium` / `small` |

## 需要的 Secrets

| Secret | 用途 |
|---|---|
| `RCLONE_CONF` | 源远端的 rclone 配置(含 refresh_token) |
| `PATHS_JSON` | 源远端名 / 目录名 + 日志脱敏词表 |
| `SUBTITLE_API_BASE` | 翻译网关 base(OpenAI 兼容, 形如 `https://host/v1`) |
| `SUBTITLE_API_KEY` | 翻译网关 key |
| `SUBTITLE_MODEL` | 主翻译模型 |
| `SUBTITLE_MODEL_FB` | 备用翻译模型 |

## 脱敏

本仓库公开。运行日志经 `logmask` 处理后,文件名/路径/远端名/番号/PII 全部替换为
不可逆短哈希;真实盘名与目录名只存在于 `PATHS_JSON` Secret 中,源码里不出现。
