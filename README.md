# cloudtransfer · bat 字幕流水线

日文语音视频 → **日文 SRT 字幕**，纯云端 GitHub Actions 流水线。
**不翻译、不烧录、无 Telegram 通知。**

## 做什么

1. 列出源网盘指定目录里的视频文件（单文件 + `.partNNN` 分卷）
2. 按「番号」把分片归组，用 ffmpeg 拼成整片
3. `faster-whisper`(**large-v3**) 做**日文**语音转写
4. 按标点把长段重切成字幕行（默认单条 ≤8 秒、≤42 字）
5. **按视频文件名逐个产出同名 srt**：`X.part002_D.mp4` → `X.part002_D.srt`
   - 单文件视频：直接 `<文件名去扩展名>.srt`
   - 多分片视频：把合并整片的时间轴按各分片时长切开，**每个分片各一份同名 srt**
   - 打开任意一个视频文件，播放器会自动加载同名日文字幕
   - 不产出任何视频文件（不烧录），也不调用任何翻译接口

幂等判定：一个组内**所有分片都有同名 srt** 才算完成并跳过。

## 怎么跑

Actions → `subtitle-translate` → Run workflow

| 输入 | 说明 |
|---|---|
| `apply` | `true` 真跑；`false` 只列分组统计 |
| `only` | 只处理某一组（按番号，留空=全部） |
| `limit_groups` | 最多处理前 N 组（默认 1） |
| `budget_min` | 本轮时间预算（分钟） |
| `whisper_model` | `large-v3`（默认，最准）/ `large-v3-turbo` / `medium` / `small` |
| `start_index` | 并行分批：从待处理列表第 K 个开始 |
| `force` | 配合 `only`：即使已有同名 srt 也强制重跑 |

日常不用手动跑：`subtitle-dispatch` 每 10 分钟补满并发（`TARGET_PARALLEL=20`，每批 3 组）。

## 需要的 Secrets（3 个）

| Secret | 用途 |
|---|---|
| `RCLONE_CONF` | 源远端的 rclone 配置（含 refresh_token） |
| `PATHS_JSON` | `{"SUB_SRC":"gd2","SUB_DIR":"out5"}` |
| `GH_TOKEN` | 调 GitHub API（dispatcher 补发批次、自接力） |

Variables：`TARGET_PARALLEL` / `GROUPS_PER_RUN`（可选 `MAX_SEG_DUR` / `MAX_SEG_CHARS`）。

> 不翻译就不需要任何 LLM 网关配置；`SUBTITLE_API_*` / `TRANSLATE_*` 已删除。

## 质量校验

`subtitle-verify` 定时扫源目录所有 `.srt`，查空条目 / 元信息泄漏 / 单条过长 / 非日文内容，
报告打进 run 日志。

## 脱敏

本仓库公开。运行日志经 `logmask` 处理后，文件名/路径/远端名/番号/PII 全部替换为
不可逆短哈希；真实盘名与目录名只存在于 `PATHS_JSON` Secret 中，源码里不出现。
