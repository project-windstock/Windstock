#!/usr/bin/env bash
# One-time hardening for the Windstock VPS. Safe to re-run.
#   ssh root@2.29.24.78 'bash -s' < deploy/harden-vps.sh
#
# - firewall (ufw): only 22, 80, 443, 8888 in
# - SSH: keys only (root keeps key login, which deploys use)
# - fail2ban on SSH
# - automatic security updates
# - journald capped at 500 MB
# - nightly backup of player data + site accounts to /root/backups (14 kept)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null

# --- firewall ---------------------------------------------------------------
# SSH is allowed BEFORE enabling, so this session is never cut off.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # ssh
ufw allow 80/tcp    # website (Caddy)
ufw allow 443/tcp   # website HTTPS once the domain points here
ufw allow 8888/tcp  # Windstock Online (game)
ufw --force enable

# --- SSH: keys only ---------------------------------------------------------
# 00- sorts first; sshd keeps the first value it reads, so this beats
# cloud-init's 50-cloud-init.conf.
cat > /etc/ssh/sshd_config.d/00-windstock.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 4
EOF
sshd -t
systemctl reload ssh

# --- fail2ban ---------------------------------------------------------------
cat > /etc/fail2ban/jail.d/windstock.conf <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

# --- automatic security updates (no automatic reboots) ----------------------
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# --- journald size cap ------------------------------------------------------
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/size.conf
systemctl restart systemd-journald

# --- nightly backup ---------------------------------------------------------
cat > /usr/local/sbin/windstock-backup <<'EOF'
#!/usr/bin/env bash
# Player saves, research progress, token key, site accounts. Pull copies
# off the box with: rsync -a root@2.29.24.78:/root/backups/ ~/WindstockBackups/
set -euo pipefail
mkdir -p /root/backups
cd /
tar czf "/root/backups/windstock-$(date -u +%Y%m%d-%H%M).tgz" --ignore-failed-read \
  opt/pogo/data/saves* opt/pogo/data/research_progress* opt/pogo/data/secret.key \
  opt/pogo/data/gyms.json opt/pogo/data/nominations.json opt/pogo/data/move_picks.json \
  opt/windstock-site/data/accounts.json opt/windstock-site/data/windstock.sqlite* \
  opt/windstock-site/.env 2>/dev/null || true
ls -1t /root/backups/windstock-*.tgz | tail -n +15 | xargs -r rm --
EOF
chmod 700 /usr/local/sbin/windstock-backup
echo '17 4 * * * root /usr/local/sbin/windstock-backup' > /etc/cron.d/windstock-backup
/usr/local/sbin/windstock-backup

echo
ufw status verbose
sshd -T | grep -E '^(passwordauthentication|permitrootlogin) '
fail2ban-client status sshd | tail -2
ls -lh /root/backups | tail -2
