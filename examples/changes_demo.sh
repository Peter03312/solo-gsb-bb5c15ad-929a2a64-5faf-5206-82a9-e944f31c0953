#!/usr/bin/env bash
# 考古地层关系核验服务 —— 正式修订变化追踪（GET /changes）端到端调用示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 说明：本脚本自洽运行（自行登记地层 C1..C5 并提交批次），修订号从 /revision 动态读取；
#      证据编号 EV-C* / EV-CP* 须未被占用（全新部署或首次运行本脚本时满足）。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
req() { curl -s -o /tmp/resp.json -w '%{http_code}' "$@"; }
show() { echo "   [HTTP $(req "$@")]"; pp < /tmp/resp.json; }
revision() { curl -s "$BASE/revision" | python3 -c 'import sys,json; print(json.load(sys.stdin)["revision"])'; }
# 只显示指定修订的变化记录
show_rev() {  # $1 = 修订号
  python3 -c "
import sys, json
rev = $1
data = json.load(sys.stdin)
out = [c for c in data['changes'] if c['revision'] == rev]
print(json.dumps({'revision': rev, 'changes': out}, ensure_ascii=False, indent=2))
"
}

echo "== 0. 健康检查与当前正式修订号 =="
curl -s "$BASE/health" | pp
R=$(revision)
echo "   当前正式修订号 R = $R（本脚本产生的修订为 R+1 .. R+3）"

echo; echo "== 1. 登记地层 C1..C5 =="
for s in C1 C2 C3 C4 C5; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"乙遗址\"}" >/dev/null
done

echo; echo "== 2. 批次 A（修订号 R -> R+1）：EV-C01 C1<C3（直接证据）、EV-C02 C1<C2、EV-C03 C2<C3、EV-C05 C4<C5 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $R,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"EV-C01\", \"stratum_a\": \"C1\", \"stratum_b\": \"C3\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"EV-C02\", \"stratum_a\": \"C1\", \"stratum_b\": \"C2\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"EV-C03\", \"stratum_a\": \"C2\", \"stratum_b\": \"C3\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"EV-C05\", \"stratum_a\": \"C4\", \"stratum_b\": \"C5\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 3. 修订 R+1 的变化记录：未定 -> 确定（同一地层对按编号固定顺序只列一次） =="
curl -s "$BASE/changes?limit=500" | show_rev $((R+1))

echo; echo "== 4. 批次 B（R+1 -> R+2）：撤回直接证据 EV-C01 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $((R+1)),
  \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"EV-C01\"}]}" | pp

echo; echo "== 5. 修订 R+2 的变化记录：C1-C3 关系不变（仍早于），最优支撑路径 [EV-C01] -> [EV-C02,EV-C03] =="
echo "   （旧侧 EV-C01 虽已撤回，仍按当时状态 active 解释）"
curl -s "$BASE/changes?limit=500" | show_rev $((R+2))

echo; echo "== 6. 失败批次不产生记录：断言 C3 早于 C1 成环（HTTP 409 CONFLICT），变化记录数不变 =="
BEFORE=$(curl -s "$BASE/changes?limit=500" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["changes"]))')
show -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $((R+2)),
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"EV-C06\", \"stratum_a\": \"C3\", \"stratum_b\": \"C1\", \"relation\": \"earlier_than\"}
  ]}"
AFTER=$(curl -s "$BASE/changes?limit=500" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["changes"]))')
echo "   变化记录数：失败前 $BEFORE，失败后 $AFTER（一致）"

echo; echo "== 7. 推演并入产生正式修订：创建推演，撤回 EV-C03、新增 EV-CP1（C3<C4），并入（R+2 -> R+3） =="
PID=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "变化追踪演示推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "   projection id = $PID"
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-C03"},
    {"op": "assert", "evidence_id": "EV-CP1", "stratum_a": "C3", "stratum_b": "C4", "relation": "earlier_than"}
  ]}' | pp
curl -s -X POST "$BASE/projections/$PID/merge" | pp

echo; echo "== 8. 修订 R+3 的变化记录（并入产生）：C1-C3 / C2-C3 变为未定（路径为空），C3-C4 / C3-C5 变早于 =="
curl -s "$BASE/changes?limit=500" | show_rev $((R+3))

echo; echo "== 9. 空净变更并入不产生记录：推演内新增后又撤回，并入只关闭推演、不递增修订号 =="
P2=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "空净变更推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/projections/$P2/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-CP9", "stratum_a": "C1", "stratum_b": "C4", "relation": "earlier_than"}
  ]}' >/dev/null
curl -s -X POST "$BASE/projections/$P2/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [{"op": "retract", "evidence_id": "EV-CP9"}]}' >/dev/null
curl -s -X POST "$BASE/projections/$P2/merge" | pp
echo "   当前正式修订号仍为 $(revision)（= R+3），无新记录"

echo; echo "== 10. 稳定游标分页：limit=2 逐页读取；同一轮固定正式修订上界 =="
PAGE=1
CURSOR=""
while :; do
  if [ -z "$CURSOR" ]; then URL="$BASE/changes?limit=2"; else URL="$BASE/changes?limit=2&cursor=$CURSOR"; fi
  RESP=$(curl -s "$URL")
  MAXREV=$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["max_revision"])')
  COUNT=$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["changes"]))')
  echo "   第 $PAGE 页：$COUNT 条（本轮上界 max_revision=$MAXREV）"
  printf '%s' "$RESP" | python3 -c '
import sys, json
for c in json.load(sys.stdin)["changes"]:
    o, n = c["old"], c["new"]
    print("     rev%d %s-%s: %s -> %s" % (c["revision"], c["stratum_a"], c["stratum_b"], o["relation"], n["relation"]))
'
  CURSOR=$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["next_cursor"] or "")')
  [ -z "$CURSOR" ] && break
  PAGE=$((PAGE+1))
done
echo "   next_cursor 为 null：本轮已读完；再次无游标请求即开启新一轮（含此后新修订）"

echo; echo "== 11. 错误示例：非法游标（HTTP 400 INVALID_CURSOR） =="
show "$BASE/changes?cursor=not-a-valid-cursor"

echo; echo "全部变化追踪示例执行完毕。"
