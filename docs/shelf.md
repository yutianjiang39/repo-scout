# 仓库书架

一个上游 `owner/repo` 一本书。新工作直接从 `--home` 下的运行库按仓库归类；
已有人工活动可按示例导入。浏览不请求 GitHub、不调用模型，也不产生审批或成果。

```sh
repo-scout shelf list
repo-scout shelf import examples/shelf-history.json
repo-scout shelf show infinitefield/hypersdk
repo-scout shelf search Q96
repo-scout shelf --pilot pilot-readonly-week export
```

`export` 刷新 `<home>/shelf/index.md` 及每库一页的中文时间线，
可用于离线浏览；SQLite 是权威来源，不手改生成页。`--pilot` 也可用于 list/show/search，
以显示独立试跑库的扫描覆盖。不要把不同试跑数据库的结果混成同一个试跑指标。

人工记录包含 `repository, kind, key, title, status, observed_at`，
可附 `material`（本地路径说明）。Issue/PR 用编号和 GitHub URL，
跟帖用评论 ID 和带 `#issuecomment-` 的 URL。未发布线索用 `finding` 或 `draft`。
相同输入重复导入不新增；状态、材料或时间改变会保留新观察。同库同类型同编号只算一条。
显示状态是最后一次本地观察，不表示当前远端状态。

历史索引不迁移、移动或上传原文件；材料路径在没有原试跑目录的机器上可能不可用。
工作流事件提供版本和状态变化，不重建旧数据库未保存的正文。书架的 Issue/PR 数量是活动数，
不是被上游采纳数。
