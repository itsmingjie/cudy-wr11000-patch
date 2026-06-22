#!/bin/sh
# Manage the nft-qos stack already shipped on Cudy WR11000 firmware.
#
# This avoids SQM/kmod packages from public OpenWrt feeds. The Cudy firmware
# already includes nft-qos, luci-app-nft-qos, nftables, and matching kernel
# nft modules. Run this script on the router over SSH.

set -eu

usage() {
	cat <<'EOF'
Usage:
  nft-qos-helper.sh status
  nft-qos-helper.sh enable [lan|wan|wan6]
  nft-qos-helper.sh add-mac <mac> <download-mbps> <upload-mbps> [comment]
  nft-qos-helper.sh add-priority <tcp|udp|udplite|sctp|dccp> <ports> <priority> [comment]
  nft-qos-helper.sh clear-mac
  nft-qos-helper.sh clear-priority
  nft-qos-helper.sh reload

Examples:
  nft-qos-helper.sh enable lan
  nft-qos-helper.sh add-mac aa:bb:cc:dd:ee:ff 100 20 "work laptop"
  nft-qos-helper.sh add-priority tcp 22,80,443 -400 "interactive tcp"

Notes:
  add-mac accepts Mbps and stores KiB/s because nft-qos uses "kbytes/second".
  Smaller priority values are earlier in the generated nft chain. Values used
  by Cudy's LuCI page include -400, -300, -225, -200, -150, -100, 0, 50, 100,
  225, and 300.
EOF
}

need_nft_qos() {
	[ -x /etc/init.d/nft-qos ] || {
		echo "nft-qos init script not found" >&2
		exit 1
	}
	command -v uci >/dev/null 2>&1 || {
		echo "uci not found" >&2
		exit 1
	}
}

mbps_to_kib() {
	awk -v mbps="$1" 'BEGIN {
		if (mbps !~ /^[0-9]+([.][0-9]+)?$/ || mbps <= 0) exit 1
		printf "%d", (mbps * 1000 / 8) + 0.999999
	}'
}

valid_mac() {
	echo "$1" | grep -Eiq '^([0-9a-f]{2}:){5}[0-9a-f]{2}$'
}

reload_qos() {
	/etc/init.d/nft-qos enable >/dev/null 2>&1 || true
	/etc/init.d/nft-qos restart
}

status() {
	echo "== nft-qos UCI =="
	uci -q show nft-qos || true
	echo
	echo "== nft-qos service =="
	/etc/init.d/nft-qos status 2>&1 || true
	echo
	echo "== nft rules =="
	nft list ruleset 2>/dev/null | sed -n '/nft-qos/,$p' | sed -n '1,180p' || true
	echo
	echo "== LuCI routes =="
	echo "http://192.168.10.1/cgi-bin/luci/admin/services/qos"
	echo "http://192.168.10.1/cgi-bin/luci/admin/services/qos/limit"
	echo "http://192.168.10.1/cgi-bin/luci/admin/services/qos/priority"
}

enable_qos() {
	netdev="${1:-lan}"
	case "$netdev" in
		lan|wan|wan6) ;;
		*) echo "expected netdev lan, wan, or wan6" >&2; exit 1 ;;
	esac

	uci -q set nft-qos.default.limit_mac_enable='1'
	uci -q set nft-qos.default.priority_enable='1'
	uci -q set nft-qos.default.priority_netdev="$netdev"
	uci -q commit nft-qos
	reload_qos
}

add_mac() {
	[ "$#" -ge 3 ] || { usage >&2; exit 1; }
	mac="$1"
	down_kib="$(mbps_to_kib "$2")" || { echo "invalid download Mbps: $2" >&2; exit 1; }
	up_kib="$(mbps_to_kib "$3")" || { echo "invalid upload Mbps: $3" >&2; exit 1; }
	comment="${4:-}"

	valid_mac "$mac" || { echo "invalid MAC address: $mac" >&2; exit 1; }

	section="$(uci add nft-qos client)"
	uci -q set "nft-qos.$section.macaddr=$mac"
	uci -q set "nft-qos.$section.drate=$down_kib"
	uci -q set "nft-qos.$section.urate=$up_kib"
	uci -q set "nft-qos.$section.drunit=kbytes"
	uci -q set "nft-qos.$section.urunit=kbytes"
	[ -z "$comment" ] || uci -q set "nft-qos.$section.comment=$comment"
	uci -q commit nft-qos
	reload_qos
}

add_priority() {
	[ "$#" -ge 3 ] || { usage >&2; exit 1; }
	proto="$1"
	ports="$2"
	priority="$3"
	comment="${4:-}"

	case "$proto" in
		tcp|udp|udplite|sctp|dccp) ;;
		*) echo "invalid protocol: $proto" >&2; exit 1 ;;
	esac
	echo "$ports" | grep -Eq '^[0-9][0-9, -]*$' || {
		echo "invalid ports list: $ports" >&2
		exit 1
	}
	case "$priority" in
		-400|-300|-225|-200|-150|-100|0|50|100|225|300) ;;
		*) echo "unexpected priority: $priority" >&2; exit 1 ;;
	esac

	section="$(uci add nft-qos priority)"
	uci -q set "nft-qos.$section.protocol=$proto"
	uci -q set "nft-qos.$section.service=$ports"
	uci -q set "nft-qos.$section.priority=$priority"
	[ -z "$comment" ] || uci -q set "nft-qos.$section.comment=$comment"
	uci -q commit nft-qos
	reload_qos
}

clear_type() {
	type="$1"
	tmp="/tmp/nft-qos-clear.$$"
	uci show nft-qos | sed -n "s/^nft-qos\\.\\([^.=]*\\)=$type$/\\1/p" > "$tmp"
	while IFS= read -r section; do
		[ -n "$section" ] && uci -q delete "nft-qos.$section" || true
	done < "$tmp"
	rm -f "$tmp"
	uci -q commit nft-qos
	reload_qos
}

need_nft_qos

cmd="${1:-}"
shift || true

case "$cmd" in
	status) status "$@" ;;
	enable) enable_qos "$@" ;;
	add-mac) add_mac "$@" ;;
	add-priority) add_priority "$@" ;;
	clear-mac) clear_type client ;;
	clear-priority) clear_type priority ;;
	reload) reload_qos ;;
	-h|--help|help|"") usage ;;
	*) echo "unknown command: $cmd" >&2; usage >&2; exit 1 ;;
esac
