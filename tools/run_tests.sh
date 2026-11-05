#!/bin/sh
# tools/run_tests.sh —— keepalive.sh / worker.js 本地回归（不用真 key，全打本地假 API）
#
#   sh tools/run_tests.sh              # 全部
#   sh tools/run_tests.sh shell        # 只测 keepalive.sh
#   sh tools/run_tests.sh worker       # 只测 worker.js
#   sh tools/run_tests.sh controller   # 只测控制器
#   sh tools/run_tests.sh bridge       # 只测实际 Codex Bridge
#
# 依赖：sh、python3、curl、node（测 worker 时）。CI 里跑的是同一份脚本。
set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/.." && pwd)
SEL=${1:-all}

PASS=0
FAIL=0
FAILED=""

ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); FAILED="$FAILED
  - $1"; printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
sec()  { printf '\n== %s ==\n' "$1"; }

# 断言 <名字> <期望子串> <实际输出>
has() {
	case "$3" in
		*"$2"*) ok "$1" ;;
		*) bad "$1" "期望包含: $2
实际: $(printf '%s' "$3" | tr '\n' '|' | cut -c1-400)" ;;
	esac
}
nhas() {
	case "$3" in
		*"$2"*) bad "$1" "不该包含: $2" ;;
		*) ok "$1" ;;
	esac
}

TMP=$(mktemp -d "${TMPDIR:-/tmp}/ibk-test.XXXXXX")
MOCK_PID=""
MOCK_PORT=""
WK_SCEN="running"
cleanup() {
	[ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null
	rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

# start_mock <scenario> [额外参数]
# 端口一律让内核挑（--port 0），从 mock 打印的 "MOCK listening on 127.0.0.1:<p>" 里读，
# 彻底避免多实例并发/残留进程撞固定端口。结果放全局 MOCK_PORT。
# POSIX sh 没有 local，变量一律 _sm_ 前缀，别踩调用方的变量。
start_mock() {
	stop_mock
	_sm_scen=$1
	rm -f "$TMP/req.log" "$TMP/mock.log"
	python3 "$HERE/mock_api.py" --scenario "$_sm_scen" --port 0 --log "$TMP/req.log" ${2:-} > "$TMP/mock.log" 2>&1 &
	MOCK_PID=$!
	_sm_i=0
	while [ "$_sm_i" -lt 100 ]; do
		_sm_line=$(grep -o 'MOCK listening on [^ ]*' "$TMP/mock.log" 2>/dev/null | head -n 1)
		if [ -n "$_sm_line" ]; then
			_sm_p=${_sm_line##*:}
			case "$_sm_p" in ''|*[!0-9]*) break ;; esac
			MOCK_PORT=$_sm_p
			return 0
		fi
		kill -0 "$MOCK_PID" 2>/dev/null || break
		sleep 0.1
		_sm_i=$((_sm_i + 1))
	done
	echo "假 API 起不来" >&2
	cat "$TMP/mock.log" >&2
	return 1
}
# reqlog —— 本次用例里假 API 收到的请求（判断脚本到底打了哪些接口）
reqlog() { cat "$TMP/req.log" 2>/dev/null; }
stop_mock() {
	if [ -n "$MOCK_PID" ]; then
		kill "$MOCK_PID" 2>/dev/null
		wait "$MOCK_PID" 2>/dev/null
		MOCK_PID=""
	fi
}

# run_ka <scenario> <keepalive.sh 参数...>
# 用 env -i 起干净环境：本机 shell 里导出的 INSTABOX_* 会串进被测脚本，测试就不再可信
run_ka() {
	scen=$1
	shift
	start_mock "$scen" "--ttl 900" || return 1
	env -i PATH="$PATH" HOME="${HOME:-/tmp}" \
		INSTABOX_API_URL="http://127.0.0.1:$MOCK_PORT" \
		INSTABOX_API_KEY=ibk_testkey0001 \
		INSTABOX_ENV_FILE=/dev/null \
		INSTABOX_WAKE_GAP=0 \
		INSTABOX_WAKE_TRIES=2 \
        INSTABOX_AUTO_START="${TEST_AUTO_START:-0}" \
		sh "$REPO/keepalive.sh" "$@" 2>&1
}

# run_wk <path> [额外 env...] —— 起假 API 后跑 worker.js 的 fetch handler
run_wk() {
	wpath=$1
	shift
	start_mock "${WK_SCEN:-running}" || return 1
	env -i PATH="$PATH" HOME="${HOME:-/tmp}" \
		INSTABOX_API_URL="http://127.0.0.1:$MOCK_PORT" \
		"$@" node "$HERE/worker_harness.mjs" "$wpath" 2>&1
}

# ------------------------------------------------------------------ shell
test_shell() {
	sec "keepalive.sh 语法"
	sh -n "$REPO/keepalive.sh" && ok "sh -n" || bad "sh -n 失败"

	sec "keepalive.sh --status（running）"
	out=$(run_ka running --status)
	has "读到 state=running" "state=running" "$out"
	has "显示机器名" "testbox" "$out"

	sec "keepalive.sh --once（running，正常续租）"
	out=$(run_ka running --once)
	has "续租成功" "续租 ok" "$out"
	has "带回 expiresAt" "到期 20" "$out"

	sec "keepalive.sh --once（suspended → resume 自愈）"
	out=$(run_ka suspended --once)
	log=$(reqlog)
	has "应先收到 409" "activity-leases/keepalive 409" "$log"
	has "打过 POST /resume" "POST /v1/machines/m_test/resume 202" "$log"
	has "唤醒后续租成功" "续租 ok（唤醒后）" "$out"
	nhas "不该走 start 兜底" "/start" "$log"

	sec "keepalive.sh --once（stopped → resume 失败用 start 兜底）"
	TEST_AUTO_START=1
	out=$(run_ka stopped --once)
	TEST_AUTO_START=0
	log=$(reqlog)
	nhas "stopped 不调用 resume" "POST /v1/machines/m_test/resume" "$log"
	has "打过后备 start" "POST /v1/machines/m_test/start 202" "$log"
	has "兜底唤醒后续租成功" "续租 ok（唤醒后）" "$out"

	sec "keepalive.sh --once（dead → 唤醒不了要如实失败）"
	out=$(run_ka dead --once)
	log=$(reqlog)
	has "只尝试 resume" "POST /v1/machines/m_test/resume 409" "$log"
	nhas "恢复失败不调用 start" "POST /v1/machines/m_test/start" "$log"
	has "报续租失败" "续租失败" "$out"
	has "汇总失败台数" "1/1 台失败" "$out"

	sec "keepalive.sh --once（nolist → 没有机器）"
	out=$(run_ka nolist --once)
	has "提示名下没有机器" "名下没有机器" "$out"

	sec "keepalive.sh --once（API 500 → 如实报错，不吞）"
	out=$(run_ka error --once)
	has "列机器失败" "列机器失败" "$out"
	has "带 HTTP 码" "HTTP 500" "$out"

	sec "keepalive.sh --release"
	out=$(run_ka running --release)
	has "释放成功" "已释放租约" "$out"

	sec "keepalive.sh 手动暂停 / 冷却 / Stopped 默认保护"
	for protected in manual cooldown stopped; do
		out=$(run_ka "$protected" --once)
		rc=$?
		log=$(reqlog)
		nhas "$protected 不恢复" "POST /v1/machines/m_test/resume" "$log"
		nhas "$protected 不 Start" "POST /v1/machines/m_test/start" "$log"
		[ "$rc" != 0 ] && ok "$protected 返回非零" || bad "$protected 返回非零" "$out"
	done

	sec "keepalive.sh 缺 key 时拒绝运行"
	out=$(env -i PATH="$PATH" INSTABOX_ENV_FILE=/dev/null sh "$REPO/keepalive.sh" --once 2>&1)
	has "报缺 key" "INSTABOX_API_KEY" "$out"
	[ "$(env -i PATH="$PATH" INSTABOX_ENV_FILE=/dev/null sh "$REPO/keepalive.sh" --once >/dev/null 2>&1; echo $?)" = "1" ] \
		&& ok "缺 key 退出码非 0" || bad "缺 key 退出码非 0" "退出码应为 1"

	sec "keepalive.sh 多 key（逗号分隔逐账号跑）"
	start_mock running "--ttl 900" || return 0
	out=$(env -i PATH="$PATH" \
		INSTABOX_API_URL="http://127.0.0.1:$MOCK_PORT" \
		INSTABOX_API_KEY="ibk_testkey0001,ibk_testkey0002" \
		INSTABOX_ENV_FILE=/dev/null \
		sh "$REPO/keepalive.sh" --once 2>&1)
	has "账号 1/2" "账号 1/2" "$out"
	has "账号 2/2" "账号 2/2" "$out"
	[ "$(printf '%s' "$out" | grep -c '续租 ok')" = "2" ] && ok "两把 key 各续一次" || bad "两把 key 各续一次" "$out"

	sec "keepalive.sh TTL 越界要拒绝"
	out=$(env -i PATH="$PATH" INSTABOX_API_KEY=ibk_x INSTABOX_ENV_FILE=/dev/null INSTABOX_LEASE_TTL=5 \
		sh "$REPO/keepalive.sh" --once 2>&1)
	has "TTL 越界报错" "TTL 必须在 30-3600" "$out"

	sec "keepalive.sh RENEW 间隔非法要拒绝"
	out=$(env -i PATH="$PATH" INSTABOX_API_KEY=ibk_x INSTABOX_ENV_FILE=/dev/null INSTABOX_LEASE_TTL=300 INSTABOX_RENEW_EVERY=300 \
		sh "$REPO/keepalive.sh" --once 2>&1)
	has "RENEW 越界报错" "续租间隔必须满足" "$out"

	sec "keepalive.sh 编号式多 key（INSTABOX_API_KEY_2）"
	start_mock running "--ttl 900" || return 0
	out=$(env -i PATH="$PATH" \
		INSTABOX_API_URL="http://127.0.0.1:$MOCK_PORT" \
		INSTABOX_API_KEY=ibk_testkey0001 \
		INSTABOX_API_KEY_2=ibk_testkey0002 \
		INSTABOX_ENV_FILE=/dev/null \
		sh "$REPO/keepalive.sh" --once 2>&1)
	has "识别账号 1/2" "账号 1/2" "$out"
	has "识别账号 2/2" "账号 2/2" "$out"
	[ "$(printf '%s' "$out" | grep -c '续租 ok')" = "2" ] && ok "编号两把 key 各续一次" || bad "编号两把 key 各续一次" "$out"

	sec "keepalive.sh 失败必须返回非 0 退出码"
	start_mock error || return 0
	env -i PATH="$PATH" \
		INSTABOX_API_URL="http://127.0.0.1:$MOCK_PORT" \
		INSTABOX_API_KEY=ibk_testkey0001 \
		INSTABOX_ENV_FILE=/dev/null \
		sh "$REPO/keepalive.sh" --once >/dev/null 2>&1
	[ "$?" != "0" ] && ok "API 500 时退出码非 0" || bad "API 500 时退出码非 0" "失败时不应返回 0"
}

# ----------------------------------------------------------------- controller
test_controller() {
	sec "Python VPS Controller 自动化测试"
	if ! command -v python3 >/dev/null 2>&1; then
		bad "python3 不存在，跳过 controller 测试" ""
		return 0
	fi
	c_out=$(cd "$REPO" && python3 -m unittest discover -s tests -v 2>&1)
	if [ "$?" = "0" ]; then
		ok "Controller 单元测试全量通过"
		printf '%s\n' "$c_out" | tail -n 4
	else
		bad "Controller 单元测试失败" "$c_out"
	fi
}

# -------------------------------------------------------------------- bridge
test_bridge() {
	sec "Codex Bridge 网关及原功能回归"
	if [ ! -d "$REPO/bridge/codex" ]; then
		printf '  SKIP: 完整 Codex Bridge 暂不随发布包分发\n'
		return 0
	fi
	b_out=$(cd "$REPO/bridge/codex" && python3 -m unittest discover -p 'test_*.py' -v 2>&1)
	if [ "$?" = "0" ]; then
		ok "Bridge 单元测试全量通过"
		printf '%s\n' "$b_out" | tail -n 4
	else
		bad "Bridge 单元测试失败" "$b_out"
	fi
}

# ----------------------------------------------------------------- worker
test_worker() {
	if ! command -v node >/dev/null 2>&1; then
		bad "node 不存在，跳过 worker 测试" ""
		return 0
	fi
	sec "worker.js 语法"
	node --input-type=module -e "
		import('file://$REPO/worker/worker.js').then(m => {
			const ok = typeof m.default?.fetch === 'function' && typeof m.default?.scheduled === 'function';
			console.log(ok ? 'WORKER-EXPORTS-OK' : 'WORKER-EXPORTS-BAD');
		}).catch(e => { console.log('WORKER-IMPORT-ERR ' + e.message); process.exit(1); });
	" > "$TMP/w1.out" 2>&1
	has "模块可解析且导出 fetch/scheduled" "WORKER-EXPORTS-OK" "$(cat "$TMP/w1.out")"

	sec "worker.js /accounts（多账号识别 + key 打码）"
	WK_SCEN=running
	out=$(run_wk /accounts INSTABOX_API_KEY=ibk_testkey0001 INSTABOX_API_KEY_2=ibk_testkey0002)
	has "认出 2 个账号" '"account": 2' "$out"
	has "key 打码不是明文" '"key": "ibk_…0001"' "$out"
	nhas "不泄漏完整 key" "ibk_testkey0001" "$out"

	sec "worker.js TOKEN 校验"
	WK_SCEN=running
	out=$(run_wk "/?token=wrong" INSTABOX_API_KEY=ibk_testkey0001 TOKEN=s3cret)
	has "错 token 401" "STATUS 401" "$out"
	out=$(run_wk "/?token=s3cret" INSTABOX_API_KEY=ibk_testkey0001 TOKEN=s3cret)
	has "对 token 200" "STATUS 200" "$out"

	sec "worker.js / 正常续租"
	WK_SCEN=running
	out=$(run_wk / INSTABOX_API_KEY=ibk_testkey0001)
	has "续租 200" "STATUS 200" "$out"
	has "带回 expiresAt" "expiresAt" "$out"
	has "命中机器 testbox" "testbox" "$out"

	sec "worker.js / suspended → resume 自愈"
	WK_SCEN=suspended
	out=$(run_wk / INSTABOX_API_KEY=ibk_testkey0001)
	has "标记 resumed" '"resumed": true' "$out"
	has "记录 wake" "resume:202" "$out"
	has "唤醒后续租成功" "expiresAt" "$out"

	sec "worker.js /dead → 唤醒不了如实失败"
	WK_SCEN=dead
	out=$(run_wk / INSTABOX_API_KEY=ibk_testkey0001)
	has "失败返回 502" "STATUS 502" "$out"
	has "说明恢复失败" "恢复请求失败" "$out"

	sec "worker.js 手动暂停 / 冷却 / Stopped 默认保护"
	for WK_SCEN in manual cooldown stopped; do
		out=$(run_wk / INSTABOX_API_KEY=ibk_testkey0001)
		log=$(reqlog)
		nhas "$WK_SCEN 不恢复" "POST /v1/machines/m_test/resume" "$log"
		nhas "$WK_SCEN 不 Start" "POST /v1/machines/m_test/start" "$log"
		has "$WK_SCEN 如实失败" "STATUS 502" "$out"
	done

	sec "worker.js /status"
	WK_SCEN=running
	out=$(run_wk /status INSTABOX_API_KEY=ibk_testkey0001)
	has "status 200" "STATUS 200" "$out"
	has "带机器名" "testbox" "$out"

	sec "worker.js 没配 key 时拒绝"
	WK_SCEN=running
	out=$(run_wk /)
	has "报没配账号" "没配账号" "$out"
}

case "$SEL" in
	all|shell|worker|controller|bridge) ;;
	*) printf 'Unknown test suite: %s\n' "$SEL" >&2; exit 1 ;;
esac
[ "$SEL" = "all" ] || [ "$SEL" = "shell" ] && test_shell
[ "$SEL" = "all" ] || [ "$SEL" = "worker" ] && test_worker
[ "$SEL" = "all" ] || [ "$SEL" = "controller" ] && test_controller
[ "$SEL" = "all" ] || [ "$SEL" = "bridge" ] && test_bridge

stop_mock
printf '\n----------------------------------------\n'
printf '通过 %s  失败 %s\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
	printf '失败项:%s\n' "$FAILED"
	exit 1
fi
exit 0
