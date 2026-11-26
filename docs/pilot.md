# 一周只读试跑

本阶段在五个公开仓库检查默认分支 SHA，每个新版本最多检查三个 Markdown 文件，
只对确实不存在于完整 Git 树的相对链接生成英文 Issue 草稿。若路径仅大小写有误且目标唯一，
同时保存英文 PR 说明及 diff。不运行上游代码、模型或公开写入。模型费用为零，
服务器租金另计。入队前按目标路径搜索上游 Issue 和 PR，跳过同时提到源文件、路径和 link 的记录。
搜索不完整时保留原游标，下轮重试。此规则可能漏掉不同措辞，验收时仍需确认上游现状和渲染环境。

Linux/systemd：将项目源码（排除本机虚拟环境、数据库、试验目录和凭证）部署在 `/opt/repo-scout`。在该目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
sudo install -m 644 deploy/repo-scout-pilot.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now repo-scout-pilot.timer
sudo systemctl start repo-scout-pilot.service
journalctl -u repo-scout-pilot.service -n 30
```

每六小时巡检，七天从首次 tick 开始；到期不再访问网络。单个仓库的扫描、草稿和游标在同一个 SQLite 事务提交。
崩溃时未完成的只读请求可能在下轮重做，已提交版本在 HEAD 未变时直接跳过。
过期草稿退出待验收队列。验收决定只记本地，不触发发送。

```sh
sudo .venv/bin/python -m repo_scout.pilot --home /var/lib/repo-scout-pilot review
sudo .venv/bin/python -m repo_scout.pilot --home /var/lib/repo-scout-pilot show ITEM_ID
sudo .venv/bin/python -m repo_scout.pilot --home /var/lib/repo-scout-pilot judge ITEM_ID worth --sha FULL_SHA --reason '已确认目标且上游未有重复问题'
sudo .venv/bin/python -m repo_scout.pilot --home /var/lib/repo-scout-pilot stats
sudo systemctl disable --now repo-scout-pilot.timer
```

不值得发送用 `reject`。用 `review` 给出的完整 SHA 验收；
新 SHA 再次检出同一问题时需要重新判断，历史决定仍计入统计。没有人工决定时比例为 null，
不代表 0%。首次匿名采样若遇共享 IP 的 GitHub 限速，保留已完成事务，
按限流响应记录最早重试时间，等待之后的定时巡检。可在服务器 `/etc/repo-scout-pilot.env` 中配置 `GITHUB_READ_TOKEN=...`（文件由 root 持有，
权限 0600）；服务不需要写 token。
