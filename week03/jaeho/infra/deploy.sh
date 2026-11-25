#!/bin/bash
# 역할별 배포: WEB(nginx + 제어기 + k6) / WAS(ROLE=api) / WORKER(ROLE=worker)
# 사용: deploy.sh <linux 바이너리>     필요 env: TEST_DB_PASSWORD, ADMIN_TOKEN, ADMIN_PASSWORD, WEB_IP, WAS_IP, WORKER_IP
set -euo pipefail
cd "$(dirname "$0")"
BIN=$1
KEY=~/.ssh/fcfs_poc_ed25519
SSH="ssh -i $KEY -o BatchMode=yes"
WEB=${WEB_IP:?WEB_IP 필요}        # fcfs-k6 서버를 WEB 으로 사용 (c4-g3a, KR-1)
WAS=${WAS_IP:?WAS_IP 필요}        # fcfs-app-kr1 (c4-g3a, KR-1)
WORKER=${WORKER_IP:?WORKER_IP 필요}  # fcfs-app-kr2 (c2-g3a, KR-2)
WAS_PRIV=10.0.30.6
BROKERS=10.0.32.7:9092,10.0.32.8:9092,10.0.32.9:9092
DBHOST=db-4c46e7.vpc-cdb.ntruss.com
LB=http://fcfs-alb-146938399-469a0f84965e.kr.lb.naverncp.com

unit() { # $1=이름 $2=env 파일 내용
cat <<EOF
mkdir -p /etc/fcfs && umask 077 && cat > /etc/fcfs/$1.env <<'ENV'
$2
ENV
cat > /etc/systemd/system/$1.service <<'UNIT'
[Unit]
Description=$1
After=network-online.target
[Service]
EnvironmentFile=/etc/fcfs/$1.env
ExecStart=/usr/local/bin/fcfs
Restart=always
RestartSec=2
LimitNOFILE=200000
[Install]
WantedBy=multi-user.target
UNIT
systemctl disable --now fcfs 2>/dev/null || true
systemctl daemon-reload && systemctl enable -q $1 && systemctl restart $1
EOF
}
DSN="fcfs:${TEST_DB_PASSWORD}@tcp(${DBHOST}:3306)/fcfs?parseTime=true&timeout=3s"

for ip in $WAS $WORKER $WEB; do scp -q -i $KEY "$BIN" root@$ip:/usr/local/bin/fcfs.new; $SSH root@$ip 'mv /usr/local/bin/fcfs.new /usr/local/bin/fcfs && chmod +x /usr/local/bin/fcfs'; done

echo "== WAS"; $SSH root@$WAS "hostnamectl set-hostname fcfs-was; $(unit fcfs-was "ROLE=api
MYSQL_DSN=$DSN
KAFKA_BROKERS=$BROKERS
ADMIN_TOKEN=$ADMIN_TOKEN")"
echo "== WORKER"; $SSH root@$WORKER "hostnamectl set-hostname fcfs-worker; $(unit fcfs-worker "ROLE=worker
MYSQL_DSN=$DSN
KAFKA_BROKERS=$BROKERS")"

echo "== WEB"
scp -q -i $KEY ../k6/ticket.js ../app/index.html nginx-fcfs.conf root@$WEB:/tmp/
$SSH root@$WEB "set -e; hostnamectl set-hostname fcfs-web
command -v nginx >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx apache2-utils >/dev/null; }
mkdir -p /var/www/fcfs /opt/fcfs /var/cache/nginx/fcfs
mv /tmp/index.html /var/www/fcfs/index.html; mv /tmp/ticket.js /opt/fcfs/ticket.js
sed 's/__WAS_IP__/$WAS_PRIV/' /tmp/nginx-fcfs.conf > /etc/nginx/sites-available/fcfs
ln -sf /etc/nginx/sites-available/fcfs /etc/nginx/sites-enabled/fcfs; rm -f /etc/nginx/sites-enabled/default
sed -i 's/worker_connections [0-9]*;/worker_connections 65535;/' /etc/nginx/nginx.conf
grep -q worker_rlimit_nofile /etc/nginx/nginx.conf || sed -i '1i worker_rlimit_nofile 200000;' /etc/nginx/nginx.conf
htpasswd -bc /etc/nginx/fcfs.htpasswd admin '$ADMIN_PASSWORD' 2>/dev/null
nginx -t -q && systemctl reload nginx || systemctl restart nginx
$(unit fcfs-ctl "ROLE=ctl
WAS_URL=http://$WAS_PRIV:8080
LOAD_TARGET=$LB
ADMIN_TOKEN=$ADMIN_TOKEN
K6_DIR=/opt/fcfs")"

sleep 4
for h in "$WAS:fcfs-was" "$WORKER:fcfs-worker" "$WEB:fcfs-ctl"; do echo "${h#*:}: $($SSH root@${h%%:*} "systemctl is-active ${h#*:}")"; done
$SSH root@$WEB "curl -s localhost/health; echo; curl -s localhost/api/stats | head -c 200; echo"
