#!/bin/bash
# What a mail cluster in a real region must look like, from Cargo and from the Internet.
#
#   tools/mail-smoke/check.sh <cluster> [--phase 3|6|7]
#
# Runs on the Cargo host (or any bench with SITE) and asks public resolvers and the cluster's
# own ports; nothing is changed. Phase 3 is the first real region; 6 adds several nodes and an
# upgrade; 7 adds customer domains through grants. The three checks that need a human, one
# external message arriving with spf, dkim and dmarc all passing, an unknown local part being
# rejected, and one plain site sending from its platform address, are listed at the end.
set -uo pipefail

CLUSTER="${1:?cluster name, e.g. mx.mail.blr.frappe.cloud}"
PHASE=3
[ "${2:-}" = "--phase" ] && PHASE="${3:-3}"
SITE="${SITE:-cargo.localhost}"
RESOLVER="${RESOLVER:-1.1.1.1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_PATH="${BENCH_PATH:-$(cd "$REPO_ROOT/../.." && pwd)}"

execute() {
	(cd "$BENCH_PATH" && bench --site "$SITE" execute "cargo.cloud_mail.smoke.$1" --kwargs "$2")
}
value() { grep -m1 "^$2=" <<<"$1" | cut -d= -f2-; }

passed=0
failed=0
check() {
	local label="$1"
	shift
	if "$@" > /tmp/mail-smoke.out 2>&1; then
		echo "  ok    $label"
		passed=$((passed + 1))
	else
		echo "  FAIL  $label"
		sed 's/^/          /' /tmp/mail-smoke.out | tail -4
		failed=$((failed + 1))
	fi
}
resolves() { dig +short "@$RESOLVER" "$2" "$1" | grep -q .; }
resolves_to() { dig +short "@$RESOLVER" "$2" "$1" | grep -qF -- "$3"; }
tls_presents() {
	local port="$1" extra="${2:-}"
	echo | openssl s_client -connect "$HOSTNAME_UNDER_TEST:$port" -servername "$HOSTNAME_UNDER_TEST" $extra -verify_return_error 2>/dev/null \
		| openssl x509 -noout -ext subjectAltName 2>/dev/null | grep -qE "DNS:(\*\.$ZONE|$HOSTNAME_UNDER_TEST)"
}

echo "==> Cargo's view of $CLUSTER (phase $PHASE)"
report="$(execute checks "{\"cluster\": \"$CLUSTER\", \"phase\": $PHASE}")" || { echo "$report" | tail -3; exit 2; }
sed 's/^/  /' <<<"$report" | grep -v "^  failed="
failed=$((failed + $(value "$report" failed)))

facts="$(execute facts "{\"cluster\": \"$CLUSTER\"}")"
HOSTNAME_UNDER_TEST="$(value "$facts" hostname)"
ZONE="$(value "$facts" zone)"
SPF_INCLUDE="$(value "$facts" spf_include)"

echo "==> What a public resolver answers ($RESOLVER)"
for node in $(value "$facts" nodes); do
	check "${node%%=*} A is ${node#*=}" resolves_to "${node%%=*}" A "${node#*=}"
done
check "$HOSTNAME_UNDER_TEST has an A record" resolves "$HOSTNAME_UNDER_TEST" A
check "$ZONE MX points at $HOSTNAME_UNDER_TEST" resolves_to "$ZONE" MX "$HOSTNAME_UNDER_TEST"
check "$ZONE SPF includes $SPF_INCLUDE" resolves_to "$ZONE" TXT "include:$SPF_INCLUDE"
check "$SPF_INCLUDE lists the sending addresses" resolves_to "$SPF_INCLUDE" TXT "v=spf1"
check "_dmarc.$ZONE rejects" resolves_to "_dmarc.$ZONE" TXT "p=reject"
for host in $(value "$facts" dkim_hosts); do
	check "$host publishes a key" resolves_to "$host" TXT "v=DKIM1"
done

echo "==> What the cluster presents"
for port in 443 465 993; do
	check "tls on $port presents the cluster certificate" tls_presents "$port"
done
check "tls on 587 presents the cluster certificate" tls_presents 587 "-starttls smtp"
check "tls on 25 presents the cluster certificate" tls_presents 25 "-starttls smtp"
check "jmap answers 401 without a token" test "$(curl -s -o /dev/null -w '%{http_code}' "https://$HOSTNAME_UNDER_TEST/.well-known/jmap")" = 401

echo
echo "$passed passed, $failed failed"
echo "Still to do by hand: send one message from a platform address to an external mailbox and read"
echo "spf=pass dkim=pass dmarc=pass in its Authentication-Results; send to an unknown local part on $ZONE"
echo "and see it rejected; configure one plain site from its platform credential and send from it."
exit $((failed > 0))
