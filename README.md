# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

入库走票面两段式：`POST /api/inbound/precheck` 只读预检（返回票面 + 将落层），
`POST /api/inbound/confirm` 整单写入（票面不符/行非法 → 整单拒写、零落行）。
到期日已过的批落行即入可下架集合，`GET /api/removals`（下架名单）、顶条、全层同一代。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。
