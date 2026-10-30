#!/usr/bin/env bash
# Run as root on the server, after the archive's SHA-256 has been verified.
set -euo pipefail
if [[ -z "${QWENNOTE_DOMAIN:-}" ]]; then
    echo '缺少 QWENNOTE_DOMAIN；请显式设置站点域名后再执行部署。' >&2
    exit 1
fi
# Accept a DNS hostname only; never echo caller input in validation errors.
if [[ ${#QWENNOTE_DOMAIN} -gt 253 || ! "$QWENNOTE_DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]; then
    echo 'QWENNOTE_DOMAIN 格式无效；请提供不含协议、端口或路径的域名。' >&2
    exit 1
fi
phase=${1:?prepare|certificate|activate}
release=${2:?absolute release directory}
[[ "$release" =~ ^/opt/qwennote/releases/[0-9]{8}T[0-9]{6}Z$ ]]
test "$(id -u)" = 0
exec 9>/run/lock/qwennote-deploy.lock
flock -n 9
cd "$release"

install_nginx_template() {
    local rendered
    rendered=$(mktemp)
    sed "s/<domain>/${QWENNOTE_DOMAIN}/g" "$1" > "$rendered"
    install -m 644 "$rendered" /etc/nginx/conf.d/qwennote.conf
    rm -f "$rendered"
}

case "$phase" in
prepare)
    test -x /usr/bin/python3.11
    if ! id qwennote >/dev/null 2>&1; then
        useradd --system --home-dir /opt/qwennote --shell /sbin/nologin qwennote
    fi
    install -d -o qwennote -g qwennote -m 750 /opt/qwennote/data /opt/qwennote/backups
    install -d -o root -g root -m 700 /etc/qwennote
    if [[ ! -e /etc/qwennote/qwennote.env ]]; then
        umask 077
        cat > /etc/qwennote/qwennote.env <<EOF
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_REDIRECT_URI=https://${QWENNOTE_DOMAIN}/auth/callback
SESSION_SECRET=$(openssl rand -hex 32)
DB_PATH=/opt/qwennote/data/qwennote.db
ADMIN_OPEN_ID=
QWENNOTE_MCP_TOKEN=
QWENNOTE_MCP_URL=https://minutes.qwennote.cn/mcp
QWENNOTE_TOKEN_FILE=/opt/qwennote/data/mcp_token.json
QWENNOTE_SOURCE=fixture
QWENNOTE_FIXTURE_DIR=/opt/qwennote/current/tests/fixtures/minutes
EOF
    fi
    chown root:root /etc/qwennote/qwennote.env
    chmod 600 /etc/qwennote/qwennote.env
    # Do not alter /usr/bin/python3 or install another interpreter.
    /usr/bin/python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
    .venv/bin/python -m scripts.prepare_model_secrets
    # Permit only the application user to traverse the root-owned config directory.
    # Other configuration files keep their existing owner and mode.
    command -v setfacl >/dev/null || dnf install -y acl
    setfacl -m u:qwennote:--x /etc/qwennote
    runuser -u qwennote -- test -r /etc/qwennote/secret.key
    bash scripts/build_sqlite.sh "$release"
    .venv/bin/python -m scripts.deployment_runtime "$release"
    command -v sqlite3 >/dev/null || dnf install -y sqlite
    chown -R qwennote:qwennote "$release"
    chmod -R u+rX,g+rX,o-rwx "$release"
    chmod 755 scripts/backup.sh scripts/rollback.sh
    ;;
certificate)
    install -d -m 755 /var/www/acme/.well-known/acme-challenge
    # Only this dedicated nginx file is ever written.
    if [[ ! -s /etc/letsencrypt/live/${QWENNOTE_DOMAIN}/fullchain.pem ]]; then
        install_nginx_template deploy/nginx/qwennote-http.conf
        nginx -t
        systemctl reload nginx
        if command -v certbot >/dev/null || dnf install -y certbot; then
            certbot certonly --webroot -w /var/www/acme -d "${QWENNOTE_DOMAIN}" \
                --non-interactive --agree-tos --register-unsafely-without-email
        else
            # Isolated acme home avoids altering any existing acme installation.
            curl --fail --silent --show-error https://get.acme.sh -o /tmp/qwennote-acme-install.sh
            sh /tmp/qwennote-acme-install.sh --home /opt/qwennote/acme --nocron
            acme=/opt/qwennote/acme/acme.sh
            "$acme" --register-account --server letsencrypt
            "$acme" --issue --server letsencrypt --webroot /var/www/acme -d "${QWENNOTE_DOMAIN}"
            install -d -m 700 "/etc/letsencrypt/live/${QWENNOTE_DOMAIN}"
            "$acme" --install-cert -d "${QWENNOTE_DOMAIN}" \
                --key-file "/etc/letsencrypt/live/${QWENNOTE_DOMAIN}/privkey.pem" \
                --fullchain-file "/etc/letsencrypt/live/${QWENNOTE_DOMAIN}/fullchain.pem" \
                --reloadcmd 'nginx -t && systemctl reload nginx'
            cat > /etc/systemd/system/qwennote-acme.service <<'EOF'
[Unit]
Description=Renew QwenNote certificate using isolated acme.sh
[Service]
Type=oneshot
ExecStart=/opt/qwennote/acme/acme.sh --cron --home /opt/qwennote/acme
EOF
            cat > /etc/systemd/system/qwennote-acme.timer <<'EOF'
[Unit]
Description=Daily QwenNote ACME renewal
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
EOF
            systemctl daemon-reload
            systemctl enable --now qwennote-acme.timer
        fi
    fi
    test -s "/etc/letsencrypt/live/${QWENNOTE_DOMAIN}/fullchain.pem"
    if command -v certbot >/dev/null && [[ -f /etc/letsencrypt/renewal/${QWENNOTE_DOMAIN}.conf ]]; then
        install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
        cat > /etc/letsencrypt/renewal-hooks/deploy/qwennote-reload.sh <<EOF
#!/bin/sh
if [ "\$RENEWED_LINEAGE" = /etc/letsencrypt/live/${QWENNOTE_DOMAIN} ]; then
    nginx -t && systemctl reload nginx
fi
EOF
        chmod 755 /etc/letsencrypt/renewal-hooks/deploy/qwennote-reload.sh
        systemctl enable --now certbot-renew.timer
    fi
    ;;
activate)
    test -s "/etc/letsencrypt/live/${QWENNOTE_DOMAIN}/fullchain.pem"
    old=$(readlink -e /opt/qwennote/current || true)
    # readlink -f on a nonexistent link can return the link's own path.
    # Accept only a real, complete release, never current itself or a symlink loop.
    if [[ "$old" != /opt/qwennote/releases/* || ! -d "$old/.venv" ]]; then old=''; fi
    if [[ -d "$old" && "$old" != "$release" ]]; then
        ln -s "$old" /opt/qwennote/.previous-deploy-$$
        mv -Tf /opt/qwennote/.previous-deploy-$$ /opt/qwennote/previous
    fi
    ln -s "$release" /opt/qwennote/.current-deploy-$$
    mv -Tf /opt/qwennote/.current-deploy-$$ /opt/qwennote/current
    if [[ "${QWENNOTE_APP_ONLY:-0}" != 1 ]]; then
        install -m 644 deploy/systemd/qwennote* /etc/systemd/system/
        systemd-analyze verify /etc/systemd/system/qwennote*.service /etc/systemd/system/qwennote*.timer
        systemctl daemon-reload
        systemctl enable --now qwennote.service
    fi
    systemctl restart qwennote.service
    healthy=false
    for attempt in {1..20}; do
        if [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/ || true)" = 302 ]]; then
            healthy=true
            break
        fi
        sleep 1
    done
    if [[ "$healthy" != true ]]; then
        if [[ -d "$old" && "$old" != "$release" ]]; then
            ln -s "$old" /opt/qwennote/.current-deploy-$$
            mv -Tf /opt/qwennote/.current-deploy-$$ /opt/qwennote/current
            systemctl restart qwennote.service
        else
            systemctl stop qwennote.service
        fi
        echo 'Application health check failed; stopped before nginx activation.' >&2
        exit 1
    fi
    if [[ "${QWENNOTE_APP_ONLY:-0}" != 1 ]]; then
        install_nginx_template deploy/nginx/qwennote.conf
        nginx -t
        systemctl reload nginx
    fi
    # nginx reload returns before new workers necessarily accept TLS connections.
    tls_healthy=false
    for attempt in {1..20}; do
        if [[ "$(curl --max-time 10 -sS "https://${QWENNOTE_DOMAIN}/" -o /dev/null -w '%{http_code} %{ssl_verify_result}' || true)" = '302 0' ]]; then
            tls_healthy=true
            break
        fi
        sleep 1
    done
    test "$tls_healthy" = true
    if [[ "${QWENNOTE_APP_ONLY:-0}" != 1 ]]; then
        systemctl disable --now qwennote-sync.timer qwennote-tag-maintenance.timer
        systemctl enable --now qwennote-tick.timer qwennote-backup.timer
    fi
    printf 'CURRENT=%s\n' "$(readlink -f /opt/qwennote/current)"
    ;;
*) echo 'Unknown phase' >&2; exit 2 ;;
esac
