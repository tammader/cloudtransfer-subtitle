# cloudtransfer · 设置清单（只出日文 srt）

> 仓库: `tammader/cloudtransfer-subtitle`（public，default branch `main`）
> 导出时间: 2026-11-14
> 说明: GitHub 的 Secret 是**只写不可回读**的，所以下面是**名称 + 含义 + 从哪来**，不含值。
> 真正的值需要你从原处（本地配置 / 各服务面板）补齐。

## 一、Secrets（3 个）

| Secret | 必填 | 含义 | 值的来源 |
|---|---|---|---|
| `RCLONE_CONF` | ✅ | rclone 配置全文（至少含源远端；烧录/备份要写回同一个源） | 本地 `~/.config/rclone/rclone.conf` 里对应远端那一段 |
| `PATHS_JSON` | ✅ | 路径字典，见下方「二」 | 自己写 |
| `GH_TOKEN` | ✅ | 调 GitHub API 用（dispatcher 补发批次、自接力） | 一个有 `repo`+`workflow` 权限的 PAT |

> TG 通知已取消：不需要 `TG_TOKEN` / `TG_CHAT_ID`，所有报告只打进 run 日志。

## 二、PATHS_JSON 结构

```json
{
  "SUB_SRC": "<rclone.conf 里的源远端名>",
  "SUB_DIR": "out5",
  "_WORDS": { "<真实词>": "<脱敏替换>" }
}
```

- `SUB_SRC` / `SUB_DIR` 是**唯二必填**。日志里会被 `logmask` 脱敏成 `<remote:xxxxxxxx>`。
- 可选 `SUB_DIRS`（数组）——当前线上代码**还没用**这个键；多目录轮转那版在本地 `_sub_repo/patched/`（未推送），见 `RESTORE.md`。
- `_WORDS` 是日志脱敏词典，可留空。

## 三、Variables

**当前：无**（线上代码里所有速度旋钮都有内置默认值）

可选键（设了就覆盖默认）：

| Variable | 默认 | 作用 |
|---|---|---|
| `WHISPER_THREADS` | 4 | whisper CPU 线程 |
| `WHISPER_BATCH` | 8 | 批量推理批大小 |
| `WHISPER_BEAM` | 1 | 1=贪心解码最快 |
| `TARGET_PARALLEL` | 20 | dispatch 补满的并发批次数 |
| `GROUPS_PER_RUN` | 3 | 每批次处理几组 |

> 烧录相关变量（`BURN_PRESET` / `BURN_CRF` / `BURN_MODE` / `BURN_PARALLEL`）已随烧录阶段一起去掉。

## 四、Workflows（4 个）

| 文件 | 触发 | 作用 |
|---|---|---|
| `subtitle-translate.yml` | schedule `23 */2` + 手动 | 干活的：下载→拼接→转写→重切→上传日文同名 srt |
| `subtitle-dispatch.yml` | schedule `8,18,28,38,48,58 * * * *` | 调度器，补满转写并发 |
| `subtitle-report.yml` | schedule `43 */2` | 进度报告（打进 run 日志，无 TG） |
| `subtitle-verify.yml` | schedule `17 */6` | srt 质量校验（空/泄漏/单条过长/非日文） |

> cron 都错峰过，故意避开整点 —— 因为 GitHub Actions 的并发额度是**账号级**的，
> 同一账号下别的定时任务会抢。

## 五、流水线设计（单阶段：只出 srt，不烧录）

```
转写阶段（subtitle-dispatch 派发，TRANSLATE=off）
     下载 → 拼接 → whisper 日文转写 → 按标点重切(≤8s/≤42字) → 上传与视频同名的 srt
                        ↓
verify 阶段（subtitle-verify）
     verify_srt.py：逐条查 空条目 / 元信息泄漏 / 单条过长 / 非日文 → 报告(run 日志)
```

> 烧录阶段已从流程中去掉：不产出 `字幕版/`、不跑 x264 转码。
> `subtitle-burn.yml` 已删除；`subtitle_dispatch.py` 收到 `PHASE=burn` 直接退出；
> `subtitle_translate.py` 里 `BURN_VIDEO` 恒为 False，`--burn-only` 会报错退出。
> **不翻译**：`TRANSLATE=off` 时直接输出日文 srt，不调用任何 LLM 网关（要恢复中译设 `TRANSLATE=on`）。

### srt 命名规则（与视频同名）

- 单文件视频：`已处理_PIYO-035_14.mp4` → `已处理_PIYO-035_14.srt`
- 多分片组：`已处理_AOZ-292Z.part001_D.mp4 … part017_D.mp4` →
  **每个分片各一份** `已处理_AOZ-292Z.part001_D.srt … part017_D.srt`
  （ffprobe 取各片时长 → 把合并整片的时间轴切分 → 每片时间轴从 0 开始）
- 完成判定：组内所有分片都有同名 srt 才跳过

## 六、关键行为参数（写在代码里）

- whisper 模型默认 `large-v3`（日语最准；`large-v3-turbo` 快 4-5 倍但精度略低）
- 依赖锁：`av==18.1.0` / `ctranslate2==4.8.5` / `faster-whisper==1.2.1`（workflow 里 pip 安装）
- 分片归组：剥 `已处理_` 前缀 + 按 `partNNN` **数字序**（不是字典序）
- 幂等：已有 `<番号>.srt` 的组跳过
- 输出：只在源目录写 `<番号>.srt`，**不写任何视频文件**
