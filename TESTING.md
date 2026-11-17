# 研究迁移包 · 实际验证记录

本文件只记录在本机实际执行过的命令与结果，不是预期清单。

## 环境

- Python 3.11.2，依赖 fastapi 0.115.6 / SQLAlchemy 2.0.36 / numpy 2.2.1 / scipy 1.15.1 / redis 5.2.1 / PyJWT 2.10.1，另加 pytest、httpx、fakeredis 仅用于测试。
- 测试数据库为 SQLite；生产配置仍为 PostgreSQL 16。包格式、状态机与文件落盘逻辑与数据库方言无关；`ALTER TABLE ... ADD COLUMN` 只在 PostgreSQL 上执行（见 `backend/migrations/README.md`）。
- 本环境中没有 Docker/PostgreSQL 服务，因此未执行 `docker compose up`；数据库行为用同一套 SQLAlchemy 模型在 SQLite 上验证，另用两个独立 uvicorn 与两个独立 worker 进程做真实 HTTP 端到端验证。

## 后端接口测试

```bash
cd /workspace/backend
python -m pytest tests -q
```

结果：`15 passed`（1 条 starlette TestClient 的 DeprecationWarning）。

覆盖场景（`backend/tests/test_transfer_package.py`）：

| 场景 | 断言 |
|---|---|
| 打包再接收 | 返回 `imported`/`ready`；本地批次与运行取得新编号；来源复核只作依据（`review_is_local=false`），本地仍可另行复核；审计写入 `run.package`/`transfer.receive`/`transfer.ready` |
| 逐项校验 | 包内含 study/observations/run/result/review 五类条目；接收记录 `validation.ok=true`；本地留档 `archive_verified=true` |
| 重试与并发 | 重复上传返回 `duplicate` 且指向同一传输编号；4 线程并发上传同一包只建一条运行、一个批次 |
| 重新打包同一次研究 | 新打包编号不同但内容相同，接收端判为 `duplicate`，复用原研究 |
| 编号碰撞 | 接收端已有批次/运行时，导入仍取得新编号，本地同号记录不变 |
| 来源同号不同内容 | 自洽但内容不同的包被 409 拒绝，提示“拒绝混合”，已有导入不被改写 |
| 缺件 | 删除 `items/observations.json` 后整包 422 拒收，且没有产生可见研究 |
| 损坏附件 | 修改 `files/result.json` 数值后哈希不符，整包 422 |
| 篡改且重写清单 | 同时改写观测、运行校验和、结果、清单哈希后仍被 `result-reproducible` 拒绝 |
| 落库后写档失败 | 模拟接收凭据写入失败，接口 503，研究保持不可见；恢复后 `resume` 返回 `recovered`/`ready`，既有研究仍在 |
| 留档被篡改 | 覆盖 ZIP 字节后 `verify` 判定失败、状态转 `incomplete`、研究隐藏但数据库行仍在 |
| 接收凭据缺失 | 删除 `.receipt.json` 后 `verify` 失败；`resume` 重建凭据后恢复可用 |
| 本地新增观测 | 导入后本地新增观测不影响 `verify`，冻结行仍逐条比对 |
| 损坏压缩条目 | 条目内容损坏但 ZIP 结构完整时 422，不返回 500 |
| 非 ZIP | 非 ZIP 字节 422 |
| 观察员权限 | viewer 上传返回 403；打包同样仅限 analyst/admin |

## 两工作席端到端

```bash
python /tmp/e2e_transfer.py   # 脚本不随仓库发布
```

流程：fakeredis TCP 服务 + 工作席 A（端口 19101）与 B（19102）各自独立数据库、产物目录与 `WORKSPACE_INSTANCE_ID`，各起 uvicorn 与 worker；A 创建运行并由 worker 实算、复核、下载研究包；B 已有自己的三个演示批次，上传后取得新编号，本地完成复核；重复上传为 `duplicate`；损坏附件包被拒收；`verify` 通过。

实际输出：

```
package bytes 6270 id ef9cc7f2a7f095b3
rejection: files/result.json size 校验失败：声明4631字节，实际5089字节（共3项不通过）
checks: 111 all passed
archives: [.../transfers/<prefix>/<package>.zip, .../<package>.receipt.json]
E2E OK
```

## 未执行项

- 未在本机运行 `docker compose up`（无 Docker）；容器内 PostgreSQL/Redis/Nginx 的启动未验证。
- 未在真实 PostgreSQL 上执行 `ALTER TABLE ... ADD COLUMN`（见迁移说明）；v1 数据库升级前应备份 `db-data` 卷。
- 未做大规模并发压力测试；并发结论仅来自 4 线程同包上传。
