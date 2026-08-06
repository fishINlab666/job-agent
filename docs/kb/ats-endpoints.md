---
来源: 全量翻页实测，12 个门户逐个翻到底（脚本 /tmp/fs_full/probe_full.py）；单条复现命令见文末
版本: v2
生效时间: 2026-08-05
权限范围: 公开
更新负责人: wujingyu
审核状态: 已审核
---

# 已实测通的招聘接口

**只收实测通过的。** Moka / 北森 / 大易的租户页格式没实测，不在本文件里 ——
需要它们的口径时去 `docs/WIKI.md §3.3`（待查），别当结论用。

## 飞书招聘（`<tenant>.jobs.feishu.cn`，也支持自定义域名）

```
POST https://<host>/api/v1/search/job/posts
     User-Agent: <必须是下面那个 Mac 串>
     Referer:    https://<host>/
     website-path: <门户路径>        ← 决定返回哪一批岗位；不带 = 第三个池
     Content-Type: application/json
     {"keyword":"","limit":200,"offset":0}
  → {"code":0,"data":{"count":627,"job_post_list":[...]}}
```

**免鉴权**：不需要 cookie、不需要 `_signature`、不需要登录。

### 三条承重口径

1. **UA 是承重的。** 只有这个串能过，其余返回 HTTP 405 + 空 body：
   ```
   Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36
   ```
   版本号可以改（`Chrome/999.0` 也是 200），**平台段不能改**（Windows/Linux/iPhone
   全是 405）。**不要简化成 `Mozilla/5.0`** —— 那是 405。
   （按 UA 的哪一部分判，未定位。这里只声明「这个串能过」。）

2. **`website-path` 是选门户的唯一开关。** `portal_type` / `portal_entrance` /
   `process_type` / `website_id` 这些参数**全是陪跑的**，改了 count 不变。
   真浏览器请求里确实带 `portal_type=6`，但删掉结果一样。

3. **翻页**：`limit` 最大可用 500，实测按 200 步长翻到 `len(rows) >= count` 为止。
   带 `website-path` 时翻页照样准：12 个门户全部 `rows == count`、id 全 unique。

### 响应码

| 返回 | 含义 | 该怎么处理 |
|---|---|---|
| `code=0` + `count>0` | 正常 | 翻页取全量 |
| `code=0` + `count=0` | **真租户，当下没在招** | 是事实不是故障（`empty_is_authoritative`）|
| `code=-9000003` | **这个租户没有这个门户** | 配置错，必须抛，**不许当空** |
| HTTP 400 + 非 JSON | 这个租户不存在 | 抛 |
| HTTP 405 + 空 body | UA 不对 | 修 UA |

`count=0` 和 `-9000003` 长得像但相反：前者是「没岗位」，后者是「打错门户」。
后者要是被当成空放过，门户改名会导致整批岗位被静默判为关闭。

## 腾讯招聘（`join.qq.com`，自建）

列表接口已接（`adapters/tencent_join.py`），UA 与飞书同一个串。
`grad_year` 100% 有值（来自站点项目配置），是目前唯一有结构化届别的源。
详情要单独打 `jobDetails` 接口，当前未拉，所以 `description` 为空。

## 复现命令

```bash
cd "/Users/wujingyu/Desktop/AI/projects-jobs/job-agent" && uv run python -c "
import httpx
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
b='https://nio.jobs.feishu.cn'
for p in (None,'index','campus'):
    h={'User-Agent':UA,'Referer':b+'/','Content-Type':'application/json'}
    if p: h['website-path']=p
    d=httpx.post(b+'/api/v1/search/job/posts', json={'keyword':'','limit':1,'offset':0}, headers=h, timeout=25).json()
    print(p or '(none)', d['code'], d['data']['count'] if d['code']==0 else d.get('msg'))
"
```

2026-08-05 全量翻页时：`(none) 2250` / `index 2077` / `campus 627`。
**十几分钟后重跑，`(none)` 已经变成 2249** —— 同一天、同一条命令。

**count 会动，门户结构不会。** 所以这份文件里的数字只能当「这一刻的观测」，
判定「接口是否正常」要看的是 `code` 和门户是否存在，不是数字对不对得上。

## 变更记录

| 版本 | 日期 | 变化 |
|---|---|---|
| v1 | 2026-08 | 飞书 + 腾讯列表接口 |
| v2 | 2026-08-05 | 加 `website-path` 门户开关、`-9000003` 语义；删掉「这个口子没有校招」（已证伪，见 WIKI §3.2）|
