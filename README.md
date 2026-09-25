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
- `POST /api/requests/extend`、`POST /api/requests/prepare`
- `POST /api/requests/fulfill`、`POST /api/requests/reject`

## 数据出境审查

每条数据定位可登记出境目的地和接收方；高风险目的地需附标准合同编号与影响评估编号。存在法律保留、未成年人缺监护人同意或编号未补齐时，发送入口关闭并在 `gate.gaps` 中列出缺口。每个定位的每个目的地只保留一份有效审查，重复提交补齐资料并沿用原审查编号。撤回同意后，该主体已通过的审查全部停用，历史依据（合同号、评估号、判定）仍可查询。

- `POST /api/export-reviews`：登记或补齐出境审查（`location_id`、`destination`、`recipient`，可附 `standard_contract_no`、`impact_assessment_no`、`guardian_consent_ref`）
- `POST /api/export/send`：按审查发送出境数据，存在缺口时返回 409 和缺口清单
- `POST /api/subjects/withdraw-consent`：记录同意撤回并停用该主体的有效审查
- 审查和发送闸门随 `GET /api/state`、`GET /api/requests/{id}` 返回

出境资料（`export_reviews` 表）、判定（`evaluate_send_gate` 纯函数）、存储（`ExportReviewStore`）在 `export_review.py` 中各自负责，界面在 `static/index.html`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整查阅请求、第三方遮蔽、未成年人/代理限制、重复与幂等、延期上限、删除法律保留、权限拒绝和版本冲突，以及出境审查的高风险编号补齐、重复沿用编号、法律保留与未成年人闸门、撤回停用和权限。

## 局限

身份依赖请求头，联系方式只存哈希；请求正文、证据文件和实际回复文件未实现加密存储；地区规则和高风险目的地清单是可配置模板，不构成法律意见；删除是流程判定，不会自动调用外部业务系统执行清除；出境发送是流程记录，不触发真实跨境传输。
