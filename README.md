# BTC 周期定位页

手机看的一页：https://claude.ai/artifact/KA7ywjoXcqZfRUJ3HgRWeb （私密，需登录 Claude）

| 文件 | 作用 | 谁改 |
|---|---|---|
| `template.html` | 页面模板，`$xxx` 是占位符 | 改版面时手动改 |
| `data.json` | 判断类手动字段（结论、条件、情景、ETF/联储/鲸鱼数字） | 每日任务按规则改；人也可以改 |
| `update_data.py` | 拉 Bybit + alternative.me，渲染页面，打印 ALERT | 不动 |
| `btc-cycle-position.html` | 渲染结果，发布到 Artifact | 自动生成 |
| `history.json` | 每日恐贪 / 价格记录，用来算「一个月前」 | 自动生成 |

每日任务：`btc-cycle-daily-update`（08:30 吉隆坡），见 `~/.claude/scheduled-tasks/btc-cycle-daily-update/SKILL.md`。

手动跑一次：

```bash
cd "/Users/merlin/Desktop/second brain/btc-cycle" && python3 update_data.py
```

跑完后在 Claude 里用 Artifact 工具把 `btc-cycle-position.html` 发布到上面的 URL。
