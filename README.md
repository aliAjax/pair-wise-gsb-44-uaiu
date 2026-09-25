# 个人数据权利请求处理系统

标准库实现的跨地区数据访问、更正、删除、撤回同意和限制处理请求后台，使用 SQLite 保存案件、数据位置、时限和审计时间线。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8210`，数据库默认 `privacy_requests.db`。可用 `--db`、`--host`、`--port` 修改。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `intake`、`privacy_officer`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`、`GET /api/requests/{id}`
- `POST /api/jurisdictions`：配置处理时限、延期上限、未成年人和代理规则
- `POST /api/subjects`：保存不含明文联系方式的索引
- `POST /api/requests`：创建权利请求，支持幂等键和30天重复请求识别
- `POST /api/requests/verify`、`POST /api/requests/assign`
- `POST /api/locations`、`POST /api/locations/classify`：多系统定位和第三方/保留分类
- `POST /api/locations/register-destination`：为每条定位登记目的地代码和境外接收方
- `POST /api/export-destinations`：主管登记目的地与风险等级（`standard`/`high`）
- `POST /api/export-consents/grant`、`POST /api/export-consents/withdraw`：出境同意（未成年人须监护人）登记与撤回
- `POST /api/export-reviews`：提交/补齐出境审查，支持 `expected_version`
- `GET /api/requests/{id}/export-gate`：发送入口状态与各目的地缺口清单
- `GET /api/export-reviews/{id}`：单份审查的历史判定依据（含已停用记录）
- `POST /api/requests/extend`、`POST /api/requests/prepare`
- `POST /api/requests/fulfill`、`POST /api/requests/reject`

## 数据出境审查规则

- 每条 `data_locations` 必须先登记目的地和境外接收方；未登记的位置会让该请求的发送入口保持关闭。
- 高风险目的地审查必须同时附 `standard_contract_no`（标准合同号）和 `impact_assessment_no`（影响评估号），缺任一编号即阻断并列出缺口。
- 任一拟出境位置存在法律保留、未成年人缺少监护人出境同意、或主体已撤回出境同意时，审查阻断，发送入口关闭。
- 同一请求 + 目的地 + 接收方只有一份有效（`approved`/`blocked`）审查；重复提交沿用原 `review_no` 并递增版本。撤回同意后已通过的审查转为 `inactive`，编号与判定依据仍可通过历史接口查询；再次提交会生成新编号。
- 职责分层：`build_export_materials` 只装配出境资料，`evaluate_export` 是纯判定函数，`ExportReviewStore` 只管 SQLite 持久化与审计，`ExportReviewService` 编排三者；`static/index.html` 只负责展示发送入口与缺口。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整查阅请求、第三方遮蔽、未成年人/代理限制、重复与幂等、延期上限、删除法律保留、权限拒绝和版本冲突；出境部分覆盖高风险双编号、缺口阻断与补齐、编号沿用、法律保留/监护人同意、撤回停用与历史可查、停用后重新编号和发送入口状态。

## 局限

身份依赖请求头，联系方式只存哈希；请求正文、证据文件和实际回复文件未实现加密存储；地区规则是可配置模板，不构成法律意见；删除是流程判定，不会自动调用外部业务系统执行清除。出境风险等级和标准合同/影响评估编号为人工登记，系统不核验编号真伪或联网备案，也不实际执行跨境传输。
