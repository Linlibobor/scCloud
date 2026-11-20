#!/bin/sh
# 首次启动：生成 ./secrets/* 与 .env（敏感密钥统一放 ./secrets/，不写入 .env 明文）。
# 已存在的 secrets / .env 不会被覆盖。
# 不使用 set -e；使用 set -u。

set -u

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

SECRETS_DIR="./secrets"
DEFAULT_ADMIN_USER="admin"
DEFAULT_ADMIN_PASSWORD="admin123"

CALLER_WEB_PORT="${WEB_PORT:-}"
CALLER_WEB_BIND="${WEB_BIND_ADDRESS:-}"

color() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info()  { printf '%s %s\n' "$(color '1;36' '•')" "$*"; }
ok()    { printf '%s %s\n' "$(color '1;32' '✓')" "$*"; }
warn()  { printf '%s %s\n' "$(color '1;33' '!')" "$*" >&2; }
die()   { printf '%s %s\n' "$(color '1;31' '✗')" "$*" >&2; exit 1; }

if ! command -v openssl >/dev/null 2>&1 && ! command -v head >/dev/null 2>&1; then
  die "需要 openssl 或 head 以生成随机密钥"
fi

gen_hex() {
  bytes=${1:-32}
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes" 2>/dev/null
  else
    head -c "$bytes" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

gen_alnum() {
  bytes=${1:-24}
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 "$bytes" 2>/dev/null | tr -d '\n/+=' | cut -c1-32
  else
    head -c "$bytes" /dev/urandom | base64 2>/dev/null | tr -d '\n/+=' | cut -c1-32
  fi
}

# compose file secrets：容器内非 root 需能读 → 0644
ensure_secret() {
  file=$1
  value=$2
  if [ -f "$file" ] && [ -s "$file" ]; then
    return 0
  fi
  if [ -z "$value" ]; then
    die "无法写入空 secret: $file"
  fi
  printf '%s' "$value" > "$file"
  chmod 644 "$file" 2>/dev/null || true
  ok "写入 $file"
}

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR" 2>/dev/null || true

LEGACY_DB_PASS=""
LEGACY_DB_ROOT=""
LEGACY_JWT=""
LEGACY_BOOT=""
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null || true
  set +a
  LEGACY_DB_PASS="${DB_PASS:-}"
  LEGACY_DB_ROOT="${DB_ROOT_PASS:-}"
  LEGACY_JWT="${JWT_SECRET:-}"
  LEGACY_BOOT="${BOOTSTRAP_ADMIN_PASSWORD:-}"
fi

info "生成 secrets（已存在则跳过）..."
if [ -n "$LEGACY_DB_ROOT" ]; then
  ensure_secret "$SECRETS_DIR/db-root-password" "$LEGACY_DB_ROOT"
else
  ensure_secret "$SECRETS_DIR/db-root-password" "$(gen_alnum 24)"
fi
if [ -n "$LEGACY_DB_PASS" ]; then
  ensure_secret "$SECRETS_DIR/db-password" "$LEGACY_DB_PASS"
else
  ensure_secret "$SECRETS_DIR/db-password" "$(gen_alnum 24)"
fi
if [ -n "$LEGACY_JWT" ] && [ "$LEGACY_JWT" != "CHANGE_ME" ] && [ "$LEGACY_JWT" != "CHANGE_ME_generate_with_openssl_rand_hex_32" ]; then
  ensure_secret "$SECRETS_DIR/jwt-secret" "$LEGACY_JWT"
else
  ensure_secret "$SECRETS_DIR/jwt-secret" "$(gen_hex 32)"
fi
ensure_secret "$SECRETS_DIR/redis-password" "$(gen_alnum 24)"
if [ -n "$LEGACY_BOOT" ]; then
  ensure_secret "$SECRETS_DIR/bootstrap-admin-password" "$LEGACY_BOOT"
else
  ensure_secret "$SECRETS_DIR/bootstrap-admin-password" "$DEFAULT_ADMIN_PASSWORD"
fi

REDIS_PASS=$(tr -d '\r\n' < "$SECRETS_DIR/redis-password")
R_REDIS_URL="redis://:${REDIS_PASS}@redis:6379/0"

sync_env_key() {
  key=$1
  val=$2
  (
    umask 077
    if grep -q "^${key}=" .env 2>/dev/null; then
      ENV_KEY="$key" ENV_VAL="$val" awk '
        index($0, ENVIRON["ENV_KEY"] "=") == 1 { print ENVIRON["ENV_KEY"] "=" ENVIRON["ENV_VAL"]; next }
        { print }
      ' .env > .env.tmp
    else
      cat .env > .env.tmp
      printf '%s=%s\n' "$key" "$val" >> .env.tmp
    fi
    chmod 600 .env.tmp 2>/dev/null || true
    mv .env.tmp .env
  )
}

migrate_legacy_ghcr_env() {
  if grep -qE '^(FRONTEND_IMAGE|BACKEND_IMAGE|R_ENGINE_IMAGE)=ghcr\.io/zyzhou-saffron/sccloud-' .env 2>/dev/null; then
    (
      umask 077
      sed \
        -e 's|^FRONTEND_IMAGE=ghcr\.io/zyzhou-saffron/sccloud-|FRONTEND_IMAGE=ghcr.io/cangiolo/sccloud-|' \
        -e 's|^BACKEND_IMAGE=ghcr\.io/zyzhou-saffron/sccloud-|BACKEND_IMAGE=ghcr.io/cangiolo/sccloud-|' \
        -e 's|^R_ENGINE_IMAGE=ghcr\.io/zyzhou-saffron/sccloud-|R_ENGINE_IMAGE=ghcr.io/cangiolo/sccloud-|' \
        .env > .env.tmp
      chmod 600 .env.tmp 2>/dev/null || true
      mv .env.tmp .env
    )
    warn "检测到旧版镜像地址 ghcr.io/zyzhou-saffron/sccloud-*，已自动迁移为 ghcr.io/cangiolo/sccloud-*"
  fi
}

if [ -f .env ]; then
  ok ".env 已存在，同步 R_REDIS_URL 并检查迁移项"
  migrate_legacy_ghcr_env
  sync_env_key R_REDIS_URL "$R_REDIS_URL"
  if ! grep -q '^BOOTSTRAP_ADMIN_USER=' .env 2>/dev/null; then
    sync_env_key BOOTSTRAP_ADMIN_USER "$DEFAULT_ADMIN_USER"
  fi
  if grep -qE '^(DB_PASS|DB_ROOT_PASS|JWT_SECRET|BOOTSTRAP_ADMIN_PASSWORD)=' .env 2>/dev/null; then
    (
      umask 077
      grep -vE '^(DB_PASS|DB_ROOT_PASS|JWT_SECRET|BOOTSTRAP_ADMIN_PASSWORD)=' .env > .env.tmp
      chmod 600 .env.tmp 2>/dev/null || true
      mv .env.tmp .env
    )
    warn "已从 .env 移除明文 DB/JWT/管理员密码（改由 ./secrets/ 提供）"
  fi
  chmod 600 .env 2>/dev/null || true
else
  if [ ! -f .env.example ]; then
    die "缺少 .env.example"
  fi
  (
    umask 077
    grep -vE '^(DB_PASS|DB_ROOT_PASS|JWT_SECRET|BOOTSTRAP_ADMIN_PASSWORD)=' .env.example > .env.tmp
    {
      echo ""
      echo "# ---- 由 setup-wizard 生成（密钥在 ./secrets/）----"
      echo "BOOTSTRAP_ADMIN_USER=${DEFAULT_ADMIN_USER}"
      echo "R_REDIS_URL=${R_REDIS_URL}"
    } >> .env.tmp
    chmod 600 .env.tmp 2>/dev/null || true
    mv .env.tmp .env
  )
  [ -n "$CALLER_WEB_PORT" ] && sync_env_key WEB_PORT "$CALLER_WEB_PORT"
  [ -n "$CALLER_WEB_BIND" ] && sync_env_key WEB_BIND_ADDRESS "$CALLER_WEB_BIND"
  ok "已从 .env.example 创建 .env（权限 600；密钥仅在 secrets/）"
fi

mkdir -p ./data/initdb.d ./data/projects 2>/dev/null || true

BOOT_SHOW=$(tr -d '\r\n' < "$SECRETS_DIR/bootstrap-admin-password" 2>/dev/null || echo "$DEFAULT_ADMIN_PASSWORD")

cat <<EOF

$(ok "初始化完成")

密钥目录：${SECRETS_DIR}/ （目录 700；文件 644 供 compose secrets 挂载）
  db-root-password  db-password  jwt-secret  redis-password  bootstrap-admin-password

默认管理员（空库首次启动由 backend bootstrap）：
  用户：${DEFAULT_ADMIN_USER}
  密码：${BOOT_SHOW}

$(warn "请尽快登录后修改密码；勿将 secrets/ 提交到 git")

下一步：
  sh ./start.sh

EOF
