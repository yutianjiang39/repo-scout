# Repo Scout

Repo Scout 帮你留意开源项目里值得修的小问题。它把证据和英文草稿存到本地；你有空时看一眼中文摘要，再决定哪条值得发。

## 一条草稿是怎么来的？

比如，某个 README 链接写的是 `docs/guide.md`，仓库里的文件却叫 `docs/Guide.md`。

Repo Scout 会核对这个版本的文件列表，再搜索上游有没有人提过同一问题。如果没找到重复记录，它就把出错的行和英文 Issue 草稿放进待验收队列。像这样能唯一确定目标文件的大小写错误，还会附上 PR 说明和一行 diff。

**验收通过只记在本地。是否发送、发送哪一条，由你单独决定。**

## 现在能跑什么？

当前的一周试跑关注 **5 个公开仓库**。服务器定时器每 6 小时检查一次，提交没变就跳过；有更新时，最多检查 3 份 Markdown 文档。试跑不调用模型，也不发布 Issue 或 PR。

第一次使用，按[试跑指南](docs/pilot.md)安装并启动。之后想看有哪些草稿，在服务器的 `/opt/repo-scout` 目录运行：

```sh
sudo .venv/bin/python -m repo_scout.pilot --home /var/lib/repo-scout-pilot review
```

想先在本地体验，可以跑[离线演练](docs/demo.md)。要接着上次的进度做，看[交接记录](docs/handoff.md)；需要完整命令或恢复数据时，再查[运维手册](docs/operations.md)。
